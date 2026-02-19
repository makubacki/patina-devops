"""
Wrapper for build_and_run_rust_binary.py that routes streams as follows:
  - stdout: log file only (not forwarded to the console)
  - stderr: log file and the console (stderr)

This provides clean CI output where only errors surface to the console while
the full execution trace is preserved in the log file.
"""

import argparse
import os
import pathlib
import signal
import subprocess
import sys
import threading
from typing import IO

# Maximum time in seconds to wait for build_and_run_rust_binary.py to complete.
SUBPROCESS_TIMEOUT_SECONDS = 300  # 5 minutes

# Maximum time in seconds to wait for stream reader threads to finish after
# the subprocess exits or is killed. This is to prevent indefinite hangs when
# grandchild processes (e.g. QEMU) keep pipe handles open.
THREAD_JOIN_TIMEOUT_SECONDS = 15


def _stream_reader(
    stream: IO[bytes],
    log_file_handle: IO[bytes],
    forward_to: "IO[bytes] | None",
) -> None:
    """Read all lines from stream, write to log file, and optionally forward."""
    for line in iter(stream.readline, b""):
        log_file_handle.write(line)
        log_file_handle.flush()
        if forward_to is not None:
            forward_to.write(line)
            forward_to.flush()
    stream.close()


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a process and all of its descendants.

    On Windows, ``process.kill()`` only terminates the immediate child. If
    that child spawned long-running grandchildren (e.g. QEMU), those keep
    running and hold inherited pipe handles open, which causes the stream
    reader threads to hang indefinitely. This helper uses ``taskkill /T``
    on Windows to tear down the entire tree. On POSIX systems it kills the
    process group when the subprocess was started in its own session.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Cross-platform wrapper for build_and_run_rust_binary.py with "
            "controlled stream routing: stdout to log file only, stderr to "
            "both log file and console."
        )
    )
    parser.add_argument(
        "--patina-qemu-repo",
        required=True,
        help="Path to the patina-qemu repository.",
    )
    parser.add_argument(
        "--patina-dxe-core-repo",
        required=True,
        help="Path to the patina-dxe-core-qemu repository.",
    )
    parser.add_argument(
        "--fw-patch-repo",
        required=True,
        help="Path to the patina-fw-patcher repository.",
    )
    parser.add_argument(
        "--pre-compiled-rom",
        required=True,
        help="Path to the pre-compiled firmware ROM file.",
    )
    parser.add_argument(
        "--build-target",
        required=True,
        help="Build target (e.g. DEBUG or RELEASE).",
    )
    parser.add_argument(
        "--toolchain",
        required=True,
        help="Rust toolchain tag.",
    )
    parser.add_argument(
        "--platform",
        required=True,
        help="QEMU platform name (e.g. Q35 or SBSA).",
    )
    parser.add_argument(
        "--log-file",
        required=True,
        help="Absolute path to the output log file.",
    )
    parser.add_argument(
        "--qemu-path",
        default="",
        help="Path to the QEMU executable directory (Windows only).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run QEMU headless.",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Skip building the firmware.",
    )
    parser.add_argument(
        "--shutdown-after-run",
        action="store_true",
        help="Shut down QEMU after running.",
    )

    args = parser.parse_args()

    script = pathlib.Path(args.patina_qemu_repo) / "build_and_run_rust_binary.py"
    log_path = pathlib.Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(script),
        "--patina-dxe-core-repo", args.patina_dxe_core_repo,
        "--fw-patch-repo", args.fw_patch_repo,
        "--pre-compiled-rom", args.pre_compiled_rom,
        "--build-target", args.build_target,
        "--toolchain", args.toolchain,
        "--platform", args.platform,
    ]

    if args.qemu_path:
        cmd += ["--qemu-path", args.qemu_path]
    if args.headless:
        cmd.append("--headless")
    if args.no_build:
        cmd.append("--no-build")
    if args.shutdown_after_run:
        cmd.append("--shutdown-after-run")

    with log_path.open("ab") as log_fh:
        # Start the subprocess in its own process group / session so that
        # the entire tree (including QEMU) can be killed on timeout.
        popen_kwargs: dict = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )

        # stdout: write to log file only, not forwarded to the console.
        stdout_thread = threading.Thread(
            target=_stream_reader,
            args=(process.stdout, log_fh, None),
        )
        # stderr: write to log file and forward to the console.
        stderr_thread = threading.Thread(
            target=_stream_reader,
            args=(process.stderr, log_fh, sys.stderr.buffer),
        )

        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            return_code = process.wait(timeout=SUBPROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(process)
            return_code = process.wait()
        finally:
            # Prevent orphaned grandchild processes from holding pipe handles
            # that cause threads to hang forever.
            stdout_thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
            stderr_thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

    if timed_out:
        timeout_msg = (
            f"ERROR: build_and_run_rust_binary.py timed out after "
            f"{SUBPROCESS_TIMEOUT_SECONDS} seconds.\n"
        ).encode()
        sys.stderr.buffer.write(timeout_msg)
        sys.stderr.buffer.flush()
        with log_path.open("ab") as log_fh:
            log_fh.write(timeout_msg)
        return 1

    if args.shutdown_after_run:
        shutdown_drive = pathlib.Path(args.patina_qemu_repo) / "Build" / "shutdown_drive"
        if shutdown_drive.exists():
            uefi_logs = shutdown_drive / "UefiLogs"
            if not uefi_logs.exists():
                failure_msg = (
                    f"ERROR: Boot did not succeed: UefiLogs directory not found in "
                    f"'{shutdown_drive}'.\n"
                ).encode()
                sys.stderr.buffer.write(failure_msg)
                sys.stderr.buffer.flush()
                with log_path.open("ab") as log_fh:
                    log_fh.write(failure_msg)
                return 1

    return return_code


if __name__ == "__main__":
    sys.exit(main())
