"""Verify an untouched upstream tree and reject private add-on artifacts."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
FILENAME_IPV4 = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
FILENAME_MASKED = re.compile(r"(?<!\d)(?:\d{1,3}|[xX]{1,3})(?:\.(?:\d{1,3}|[xX]{1,3})){3}(?![\dxX])")
MASKED = re.compile(r"(?<![\d.])(?:\d{1,3}|[xX]{1,3})(?:\.(?:\d{1,3}|[xX]{1,3})){3}(?![\d.xX])")
EXAMPLES = ipaddress.ip_network("192.168.1.0/24")
PRIVATE_DIRS = {"analysis", "outputs", "backups", "validation", "__pycache__"}
PRIVATE_SUFFIXES = (".local.env", ".local.json", ".log", ".bundle", ".tar", ".tar.gz", ".zip", ".db", ".sse")
PRIVATE_NAMES = {"validation_report.md", "aisbench_result.csv", "temp_api.py", "picked_ids.txt", "publication-private-map.json"}
SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{24,})")
UPSTREAM_REPOSITORY = "https://github.com/rayn-zzz/aisbench_auto_tools_prefix"
UPSTREAM_COMMIT = "ef2b6d8653d57487b622b0dc9db458f8a1e8dec6"
TRUSTED_MANIFEST_SHA256 = "f958ad0bc6b78518e7d5bd53af73d9ab94a405d02ac73a45314328c015635cad"


def public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        # A version string such as 3.1.20260912 is not an IPv4 address.
        return True
    return address.is_loopback or address.is_unspecified or address in EXAMPLES


def private_path(path: Path) -> bool:
    return any(part.casefold() in PRIVATE_DIRS for part in path.parts) or path.name.casefold().endswith(PRIVATE_SUFFIXES) or path.name.casefold() in PRIVATE_NAMES


def files_to_check(root: Path) -> tuple[list[Path], bool]:
    try:
        probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    except OSError:
        probe = None
    if probe is not None and probe.returncode == 0 and Path(probe.stdout.strip()).resolve() == root:
        tracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
        return [Path(name.decode()) for name in tracked.split(b"\0") if name], True
    # Deployment archives intentionally contain no .git directory. Local
    # runtime files may coexist with source, so exclude them in archive mode.
    return [path.relative_to(root) for path in root.rglob("*") if path.is_file() and ".git" not in path.parts and not private_path(path.relative_to(root))], False


def verify_upstream(root: Path, paths: list[Path], tracked: bool) -> tuple[list[dict], set[str]]:
    """Only exact, pinned upstream bytes may retain upstream's public examples.

    A local edit to the JSON cannot approve a new root file: its byte checksum,
    repository and revision are pinned here and reviewed with the add-on.
    """
    findings = []
    def fail(path: str, reason: str) -> None:
        findings.append({'path': path, 'line': 0, 'reason': reason})
    manifest_path = root / 'add' / 'upstream_manifest.json'
    try:
        raw = manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != TRUSTED_MANIFEST_SHA256:
            raise ValueError('untrusted manifest')
        manifest = json.loads(raw)
        if manifest['repository'] != UPSTREAM_REPOSITORY or manifest['commit'] != UPSTREAM_COMMIT:
            raise ValueError('untrusted repository or commit')
        expected = manifest['files']
        if not isinstance(expected, dict) or not expected:
            raise ValueError('empty upstream tree')
        for name, entry in expected.items():
            if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
                    or name.startswith('add/') or not isinstance(entry, dict)
                    or not re.fullmatch(r'[a-f0-9]{64}', entry.get('sha256', ''))
                    or entry.get('mode') not in {'100644', '100755'}):
                raise ValueError('invalid upstream entry')
    except (OSError, ValueError, KeyError, TypeError):
        fail('add/upstream_manifest.json', 'missing or untrusted upstream manifest')
        return findings, set()
    actual = {path.as_posix() for path in paths if path.parts[0] != 'add'}
    for name in sorted(set(expected) - actual):
        fail(name, 'upstream file is missing')
    for name in sorted(actual - set(expected)):
        # Unknown paths can themselves contain private data; do not echo them.
        fail('[non-add-file]', 'file outside add/ is not in the upstream tree')
    modes = {}
    if tracked:
        index = subprocess.check_output(['git', '-C', str(root), 'ls-files', '--stage', '-z'])
        for item in index.split(b'\0'):
            if item:
                metadata, name = item.split(b'\t', 1)
                modes[name.decode()] = metadata.split()[0].decode()
    for name in sorted(actual & set(expected)):
        path = root / name
        try:
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]['sha256']:
                fail(name, 'upstream file differs from the pinned original bytes')
            # Windows archives have no Git index or POSIX executable mode.
            mode = modes.get(name) if tracked else (None if os.name == 'nt' else ('100755' if path.stat().st_mode & 0o111 else '100644'))
            if mode is not None and mode != expected[name]['mode']:
                fail(name, 'upstream file mode differs from the pinned original')
        except OSError:
            fail(name, 'cannot read upstream file')
    return findings, set(expected) if not findings else set()


def audit(root: Path) -> tuple[list[dict], int, bool]:
    paths, tracked = files_to_check(root)
    findings, verified_upstream = verify_upstream(root, paths, tracked)
    for relative in paths:
        path = root / relative
        display_path = FILENAME_IPV4.sub(lambda match: match.group() if public_address(match.group()) else "[redacted-address]", str(relative))
        display_path = FILENAME_MASKED.sub(
            lambda match: match.group() if "x" not in match.group().lower() or match.group().lower().startswith("192.168.1.") else "[redacted-address]",
            display_path,
        )
        if display_path != str(relative):
            findings.append({"path": display_path, "line": 0, "reason": "network address in filename"})
        if tracked and private_path(relative):
            findings.append({"path": display_path, "line": 0, "reason": "local artifact is tracked"})
        if path.is_symlink():
            findings.append({"path": display_path, "line": 0, "reason": "review symlink before public release"})
            continue
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            findings.append({"path": display_path, "line": 0, "reason": "unreadable or binary tracked file"})
            continue
        for line_no, line in enumerate(content.splitlines(), 1):
            if relative.as_posix() not in verified_upstream and any(not public_address(match.group()) for match in IPV4.finditer(line)):
                findings.append({"path": display_path, "line": line_no, "reason": "network address outside public example range"})
            for match in MASKED.finditer(line):
                value = match.group()
                if relative.as_posix() not in verified_upstream and "x" in value.lower() and not value.lower().startswith("192.168.1."):
                    findings.append({"path": display_path, "line": line_no, "reason": "use the documented example subnet"})
            if SECRET.search(line):
                findings.append({"path": display_path, "line": line_no, "reason": "credential-shaped value"})
    return findings, len(paths), tracked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error("--root must name an existing directory")
    findings, count, tracked = audit(args.root.resolve())
    if count == 0:
        print("FILES=0 FAIL=1: no public source files found")
        return 1
    print(f"FILES={count} MODE={'tracked' if tracked else 'archive'} FAIL={len(findings)}")
    for finding in findings:
        # Never echo the offending address or credential in a public CI log.
        print("{path}:{line}: {reason}".format(**finding))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
