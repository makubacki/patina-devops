"""
Wrapper for build_and_run_rust_binary.py that routes streams as follows:
  - stdout: log file only (not forwarded to the console)
  - stderr: log file and the console (stderr)

This provides clean CI output where only errors surface to the console while
the full execution trace is preserved in the log file.
"""

import argparse
import pathlib
import subprocess
import sys
import threading
from typing import IO


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
        default="false",
        help="Run QEMU headless ('true' or 'false').",
    )
    parser.add_argument(
        "--no-build",
        default="false",
        help="Skip building the firmware ('true' or 'false').",
    )
    parser.add_argument(
        "--shutdown-after-run",
        default="false",
        help="Shut down QEMU after running ('true' or 'false').",
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
    if args.headless.lower() == "true":
        cmd.append("--headless")
    if args.no_build.lower() == "true":
        cmd.append("--no-build")
    if args.shutdown_after_run.lower() == "true":
        cmd.append("--shutdown-after-run")

    with log_path.open("ab") as log_fh:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
        stdout_thread.join()
        stderr_thread.join()

    return process.wait()


if __name__ == "__main__":
    sys.exit(main())
