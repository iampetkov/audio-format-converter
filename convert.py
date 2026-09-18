"""Batch audio converter CLI entry point."""

from __future__ import annotations

import os
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS  # type: ignore[attr-defined]
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FFMPEG_PATH = os.path.join(BASE_DIR, "bin", "ffmpeg")


def show_usage() -> None:
    print("Usage: python3 convert.py <input_folder>")


def load_config() -> Dict:
    config_path = Path(BASE_DIR) / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Missing config file: {config_path}")

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in config file: {config_path}") from exc

    if not isinstance(data, dict):
        raise ValueError("Config file must contain a JSON object")
    for key in ("general", "formats"):
        if key not in data:
            raise ValueError(f"Config is missing required key: {key}")

    if not isinstance(data["formats"], dict):
        raise ValueError("Config key 'formats' must be an object")
    if not isinstance(data["general"], dict):
        raise ValueError("Config key 'general' must be an object")

    return data


def validate_input_path(input_path: Path) -> List[Path]:
    if not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")
    if not input_path.is_dir():
        raise NotADirectoryError(f"Input path is not a directory: {input_path}")

    wav_files = [
        item for item in input_path.iterdir()
        if item.is_file() and item.suffix.lower() == ".wav"
    ]
    if not wav_files:
        raise ValueError(f"No .wav files found in: {input_path}")

    return wav_files


def ensure_ffmpeg_available() -> None:
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "ffmpeg is not available in PATH. Please install ffmpeg and retry."
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg is not usable in PATH. 'ffmpeg -version' did not succeed."
        )


def prepare_output_folders(input_dir: Path, formats: Dict) -> Dict[str, Path]:
    parent = input_dir.parent
    output_dirs: Dict[str, Path] = {}

    for fmt_name, fmt_cfg in formats.items():
        if not isinstance(fmt_cfg, dict) or not fmt_cfg.get("enabled", False):
            continue

        folder_name = fmt_cfg.get("folder_name", fmt_name)
        output_dir = parent / folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Output folder prepared: {output_dir}")
        output_dirs[fmt_name] = output_dir

    return output_dirs


def get_max_workers(config: Dict) -> int:
    default_workers = min(8, os.cpu_count() or 1)
    raw_value = config.get("general", {}).get("max_workers", default_workers)
    try:
        workers = int(raw_value)
    except (TypeError, ValueError):
        return default_workers
    return workers if workers >= 1 else default_workers


def build_ffmpeg_command(
    input_file: Path,
    output_targets: List[Tuple[str, Path, Dict]],
    overwrite: bool,
) -> List[str]:
    command = [
        FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-i",
        str(input_file),
    ]

    for index, (fmt_name, output_file, fmt_cfg) in enumerate(output_targets):
        command.extend(["-map", "0:a"])
        command.extend([f"-c:a:{index}", str(fmt_cfg["codec"])])
        command.extend([f"-ac:a:{index}", str(fmt_cfg["channels"])])
        command.extend([f"-ar:a:{index}", str(fmt_cfg["sample_rate"])])

        if fmt_name == "ogg":
            command.extend(["-strict", "experimental"])
            command.extend([f"-q:a:{index}", str(fmt_cfg["quality"])])
        elif fmt_name == "opus":
            command.extend([f"-b:a:{index}", str(fmt_cfg["bitrate"])])
            if "vbr" in fmt_cfg:
                command.extend([f"-vbr:a:{index}", str(fmt_cfg["vbr"])])
            if "application" in fmt_cfg:
                command.extend([f"-application:a:{index}", str(fmt_cfg["application"])])
        elif fmt_name == "m4a":
            if "quality" in fmt_cfg:
                command.extend([f"-q:a:{index}", str(fmt_cfg["quality"])])
            elif "bitrate" in fmt_cfg:
                command.extend([f"-b:a:{index}", str(fmt_cfg["bitrate"])])
            else:
                raise ValueError("m4a config must include either 'quality' or 'bitrate'")
        else:
            raise ValueError(f"Unsupported format configured: {fmt_name}")
        command.append(str(output_file))
    return command


def ffmpeg_error_summary(result: subprocess.CompletedProcess) -> str:
    stream = result.stderr if result.stderr else result.stdout
    if not stream:
        return "No readable ffmpeg error output was returned."

    raw_lines = [line.strip() for line in stream.splitlines() if line.strip()]
    if not raw_lines:
        return "No readable ffmpeg error output was returned."

    ignore_prefixes = (
        "ffmpeg version",
        "built with",
        "configuration:",
        "libavutil",
        "libavcodec",
        "libavformat",
        "libavfilter",
        "libswresample",
        "libswscale",
    )
    meaningful_lines = []
    for line in reversed(raw_lines):
        lowered = line.lower()
        if any(prefix in lowered for prefix in ignore_prefixes):
            continue
        meaningful_lines.append(line)
        if len(meaningful_lines) == 3:
            break

    if not meaningful_lines:
        return "No readable ffmpeg error output."
    return " | ".join(reversed(meaningful_lines))


def run_conversion(
    wav_file: Path,
    output_targets: List[Tuple[str, Path, Dict]],
    overwrite: bool,
) -> Tuple[bool, str]:
    output_files = [output_file for _, output_file, _ in output_targets]
    command = build_ffmpeg_command(wav_file, output_targets, overwrite)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return False, str(exc)

    if result.returncode == 0:
        return True, ""

    cleanup_failures = []
    for output_file in output_files:
        if output_file.exists() and output_file.stat().st_size == 0:
            try:
                output_file.unlink()
            except OSError as exc:
                cleanup_failures.append(str(exc))

    if cleanup_failures:
        return False, f"{ffmpeg_error_summary(result)} | cleanup failed: {'; '.join(cleanup_failures)}"
    return False, ffmpeg_error_summary(result)


def summary_to_console(
    wav_count: int,
    attempted: int,
    succeeded: int,
    failed: int,
    format_stats: Dict[str, Dict[str, int]],
    failures: List[Tuple[str, str]],
) -> None:
    print("Conversion complete.")
    print(f"WAV files found: {wav_count}")
    print(f"Conversions attempted: {attempted}")
    print(f"Succeeded: {succeeded}")
    print(f"Failed: {failed}\n")
    print("Per format:")
    for fmt_name in sorted(format_stats.keys()):
        stats = format_stats[fmt_name]
        print(f"- {fmt_name}: {stats['succeeded']} succeeded, {stats['failed']} failed")

    if failures:
        print("\nFailures:")
        for file_name, fmt_name in failures:
            print(f"- {file_name} -> {fmt_name}")
    else:
        print("\nFailures: none")


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        show_usage()
        return 1

    input_path = Path(argv[1]).expanduser().resolve()

    try:
        config = load_config()
        ensure_ffmpeg_available()
        wav_files = validate_input_path(input_path)
    except (FileNotFoundError, NotADirectoryError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Found {len(wav_files)} wav files")

    output_dirs = prepare_output_folders(input_path, config.get("formats", {}))
    if not output_dirs:
        print("Error: No enabled output formats found in config.json.")
        return 1

    general_cfg = config.get("general", {})
    overwrite = bool(general_cfg.get("overwrite", True))
    max_workers = get_max_workers(config)

    format_stats: Dict[str, Dict[str, int]] = {
        fmt_name: {"succeeded": 0, "failed": 0}
        for fmt_name in output_dirs.keys()
    }
    failures: List[Tuple[str, str]] = []

    format_count = len(output_dirs)
    attempted = len(wav_files) * format_count
    succeeded = 0
    failed = 0
    completed_jobs = 0
    total_wav_files = len(wav_files)

    futures: Dict = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for wav_file in wav_files:
            output_targets = []
            for fmt_name, output_dir in output_dirs.items():
                output_targets.append(
                    (
                        fmt_name,
                        output_dir / f"{wav_file.stem}.{fmt_name}",
                        config["formats"][fmt_name],
                    )
                )

            futures[
                executor.submit(
                    run_conversion,
                    wav_file=wav_file,
                    output_targets=output_targets,
                    overwrite=overwrite,
                )
            ] = wav_file

        for future in as_completed(futures):
            wav_file = futures[future]
            completed_jobs += 1
            try:
                success, message = future.result()
            except ValueError as exc:
                success = False
                message = str(exc)
            except Exception as exc:
                success = False
                message = str(exc)

            if success:
                succeeded += format_count
                for fmt_name in output_dirs.keys():
                    format_stats[fmt_name]["succeeded"] += 1
                print(f"[{completed_jobs} / {total_wav_files}] {wav_file.name} [success]")
            else:
                failed += format_count
                for fmt_name in output_dirs.keys():
                    format_stats[fmt_name]["failed"] += 1
                failures.append((wav_file.name, "all"))
                print(f"[{completed_jobs} / {total_wav_files}] {wav_file.name} [failed]")
                print(f"  error: {message}")

    summary_to_console(
        wav_count=len(wav_files),
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        format_stats=format_stats,
        failures=failures,
    )

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
