import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


ADD_DIR = Path(__file__).resolve().parent
BASE_DIR = ADD_DIR.parent
ARCHIVE_MANIFEST_NAME = "archive_manifest.json"
DEFAULT_ROOT_ARTIFACTS = [
    "aisbench.log",
    "aisbench_all.log",
    "aisbench_result.csv",
    "temp_api.py",
    "peak_search.log",
    "peak_search_report.csv",
    "peak_search_report_all.csv",
    "peak_search_report.md",
    "peak_search_report.json",
]
DEFAULT_CAPTURE_SUFFIXES = {".log", ".csv", ".json", ".md", ".txt", ".html", ".doc"}


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sanitize_archive_name(value: str, fallback: str = "task") -> str:
    safe: List[str] = []
    for char in str(value or "").strip():
        if char.isalnum() or char in {"-", "_"}:
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe).strip("_") or fallback


def create_archive_dir(output_root: Path, task_kind: str, archive_name: str, run_tag: str = "") -> Path:
    ensure_dir(output_root)
    kind_name = sanitize_archive_name(task_kind, fallback="task")
    archive_base = sanitize_archive_name(archive_name, fallback=kind_name)
    suffix = f"_{sanitize_archive_name(run_tag)}" if str(run_tag or "").strip() else ""
    archive_dir = output_root / f"{kind_name}_{archive_base}_{timestamp()}{suffix}"
    ensure_dir(archive_dir)
    return archive_dir


def copy_if_exists(source: Path, target: Path) -> str:
    if not source.exists():
        return ""
    ensure_dir(target.parent)
    if source.is_dir():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)
    return str(target)


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def copy_root_artifacts(source_root: Path, archive_dir: Path, artifact_files: Optional[Sequence[str]] = None) -> List[str]:
    copied: List[str] = []
    for name in artifact_files or DEFAULT_ROOT_ARTIFACTS:
        copied_path = copy_if_exists(source_root / str(name), archive_dir / Path(str(name)).name)
        if copied_path:
            copied.append(copied_path)
    return copied


def snapshot_recent_output_files(
    output_root: Path,
    capture_root: Path,
    since_ts: Optional[float] = None,
    suffixes: Optional[Sequence[str]] = None,
) -> str:
    if not output_root.exists() or not output_root.is_dir():
        return ""
    resolved_capture_root = capture_root.resolve()
    allowed_suffixes = {item.lower() for item in (suffixes or DEFAULT_CAPTURE_SUFFIXES)}
    copied = False
    for source_path in output_root.rglob("*"):
        if not source_path.is_file() or source_path.suffix.lower() not in allowed_suffixes:
            continue
        try:
            top_level = source_path.relative_to(output_root).parts[0]
        except (ValueError, IndexError):
            top_level = ""
        if top_level == "scenario_results" or top_level.startswith(("performance_", "peak_")):
            continue
        resolved_source_path = source_path.resolve()
        if path_is_relative_to(resolved_source_path, resolved_capture_root):
            continue
        if since_ts is not None and source_path.stat().st_mtime + 1 < since_ts:
            continue
        relative = source_path.relative_to(output_root)
        target = capture_root / relative
        ensure_dir(target.parent)
        shutil.copy2(source_path, target)
        copied = True
    return str(capture_root) if copied else ""


def write_archive_manifest(
    archive_dir: Path,
    *,
    task_kind: str,
    archive_name: str,
    run_tag: str = "",
    output_root: Optional[Path] = None,
    source_root: Optional[Path] = None,
    captured_outputs_dir: str = "",
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    manifest_path = archive_dir / ARCHIVE_MANIFEST_NAME
    archived_files = sorted(
        str(path.relative_to(archive_dir)).replace("\\", "/")
        for path in archive_dir.rglob("*")
        if path.is_file() and path != manifest_path
    )
    manifest = {
        "task_kind": task_kind,
        "archive_name": archive_name,
        "run_tag": run_tag,
        "archive_dir": str(archive_dir),
        "output_root": str(output_root) if output_root else "",
        "source_root": str(source_root) if source_root else "",
        "captured_outputs_dir": captured_outputs_dir,
        "archived_files": archived_files,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "extra_metadata": extra_metadata or {},
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def archive_performance_run(
    *,
    archive_name: str,
    output_dir: str,
    run_tag: str = "",
    since_ts: Optional[float] = None,
    source_root: Optional[Path] = None,
    task_kind: str = "performance",
) -> Dict[str, Any]:
    source_root = source_root or BASE_DIR
    output_root = Path(output_dir)
    if not output_root.is_absolute():
        output_root = (source_root / output_root).resolve()
    archive_dir = create_archive_dir(output_root, task_kind, archive_name, run_tag=run_tag)
    copy_root_artifacts(source_root, archive_dir)
    captured_outputs_dir = snapshot_recent_output_files(output_root, archive_dir / "captured_outputs", since_ts=since_ts)
    return write_archive_manifest(
        archive_dir,
        task_kind=task_kind,
        archive_name=archive_name,
        run_tag=run_tag,
        output_root=output_root,
        source_root=source_root,
        captured_outputs_dir=captured_outputs_dir,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive aisbench runtime artifacts into a task-scoped directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    perf_parser = subparsers.add_parser("performance", help="archive a performance task run")
    perf_parser.add_argument("--archive-name", required=True, help="task name used in archive directory")
    perf_parser.add_argument("--output-dir", required=True, help="runtime OUTPUT_DIR root")
    perf_parser.add_argument("--run-tag", default="", help="optional task id or suffix")
    perf_parser.add_argument("--since-ts", type=float, default=None, help="only capture files modified after this timestamp")
    perf_parser.add_argument("--task-kind", default="performance", help="archive kind label")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "performance":
        manifest = archive_performance_run(
            archive_name=args.archive_name,
            output_dir=args.output_dir,
            run_tag=args.run_tag,
            since_ts=args.since_ts,
            task_kind=args.task_kind,
        )
        print("===AISBENCH_ARCHIVE===")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    raise ValueError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
