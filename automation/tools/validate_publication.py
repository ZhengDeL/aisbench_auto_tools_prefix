"""Audit native working files and the actual Git index or release tree."""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path

NATIVE_DIRECTORY = 'aisbench_auto_tools_prefix'
AUTOMATION_DIRECTORY = 'automation'
IPV4 = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
FILENAME_IPV4 = IPV4
MASKED = re.compile(r'(?<![\d.])(?:\d{1,3}|[xX]{1,3})(?:\.(?:\d{1,3}|[xX]{1,3})){3}(?![\d.xX])')
FILENAME_MASKED = MASKED
IPV6 = re.compile(r'(?<![\w:])(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:.]*(?:%[\w.-]+)?')
EXAMPLES = ipaddress.ip_network('192.168.1.0/24')
EXAMPLES_V6 = ipaddress.ip_network('2001:db8::/32')
PRIVATE_DIRS = {'analysis', 'outputs', 'backups', 'validation', '__pycache__'}
PRIVATE_SUFFIXES = ('.local.env', '.local.json', '.log', '.bundle', '.tar', '.tar.gz', '.zip', '.db', '.sse')
PRIVATE_NAMES = {'validation_report.md', 'aisbench_result.csv', 'temp_api.py', 'picked_ids.txt', 'publication-private-map.json'}
SECRET = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{24,})')
UPSTREAM_REPOSITORY = 'https://github.com/rayn-zzz/aisbench_auto_tools_prefix'
UPSTREAM_COMMIT = 'ac06bd619726663ac8c7b0c84c9b3cdbb041b062'
TRUSTED_MANIFEST_SHA256 = 'ee904c87de693a8a129b98fa41c73df42f06b1f9acc1c99caabe48b87da63950'


def public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split('%', 1)[0])
    except ValueError:
        return True
    return address.is_loopback or address.is_unspecified or (address in EXAMPLES if address.version == 4 else address in EXAMPLES_V6)


def private_path(path: Path) -> bool:
    return any(p.casefold() in PRIVATE_DIRS for p in path.parts) or path.name.casefold().endswith(PRIVATE_SUFFIXES) or path.name.casefold() in PRIVATE_NAMES


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(root), *arguments], stderr=subprocess.PIPE)


def _is_checkout(root: Path) -> bool:
    try:
        return Path(_git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve() == root.resolve()
    except (OSError, subprocess.CalledProcessError):
        return False


def git_snapshot(root: Path, ref: str | None = None) -> dict[str, tuple[str, bytes]]:
    records = _git(root, 'ls-tree', '-r', '-z', ref) if ref else _git(root, 'ls-files', '--stage', '-z')
    entries = []
    for record in records.split(b'\0'):
        if not record:
            continue
        metadata, name = record.split(b'\t', 1)
        parts = metadata.split()
        mode, blob = parts[0].decode(), parts[2 if ref else 1].decode()
        if not ref and parts[2] != b'0':
            raise ValueError('unmerged Git index')
        if mode not in {'100644', '100755', '120000'}:
            raise ValueError('unsupported tracked object')
        entries.append((name.decode('utf-8'), mode, blob))
    result = subprocess.run(['git', '-C', str(root), 'cat-file', '--batch'],
                            input=(''.join(blob + '\n' for _, _, blob in entries)).encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    data, offset, snapshot = result.stdout, 0, {}
    for name, mode, blob in entries:
        end = data.index(b'\n', offset)
        header = data[offset:end].split()
        if len(header) != 3 or header[0].decode() != blob or header[1] != b'blob':
            raise ValueError('invalid Git blob stream')
        size = int(header[2])
        snapshot[name] = (mode, data[end + 1:end + 1 + size])
        offset = end + size + 2
    return snapshot


def files_to_check(root: Path) -> tuple[list[Path], bool]:
    if _is_checkout(root):
        return [Path(n.decode('utf-8')) for n in _git(root, 'ls-files', '-z').split(b'\0') if n], True
    return [p.relative_to(root) for p in root.rglob('*') if p.is_file() and '.git' not in p.parts and not private_path(p.relative_to(root))], False


def _manifest(raw: bytes) -> dict:
    if hashlib.sha256(raw).hexdigest() != TRUSTED_MANIFEST_SHA256:
        raise ValueError('untrusted manifest')
    value = json.loads(raw)
    if value['repository'] != UPSTREAM_REPOSITORY or value['commit'] != UPSTREAM_COMMIT:
        raise ValueError('untrusted provenance')
    files = value['files']
    if not isinstance(files, dict) or not files:
        raise ValueError('empty manifest')
    for name, entry in files.items():
        if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
                or not isinstance(entry, dict) or not re.fullmatch('[a-f0-9]{64}', entry.get('sha256', ''))
                or entry.get('mode') not in {'100644', '100755'}):
            raise ValueError('invalid manifest entry')
    return files


def _verify_snapshot(snapshot: dict[str, tuple[str, bytes]], context: str) -> tuple[list[dict], set[str]]:
    findings = []
    def fail(path, reason):
        findings.append({'path': path, 'line': 0, 'reason': context + ': ' + reason})
    manifest_name = AUTOMATION_DIRECTORY + '/metadata/upstream_manifest.json'
    if manifest_name not in snapshot:
        manifest_name = AUTOMATION_DIRECTORY + '/upstream_manifest.json'
    try:
        expected = _manifest(snapshot[manifest_name][1])
    except (KeyError, ValueError, TypeError):
        fail(manifest_name, 'missing or untrusted upstream manifest')
        return findings, set()
    prefix = NATIVE_DIRECTORY + '/'
    actual = {name[len(prefix):] for name in snapshot if name.startswith(prefix)}
    for name in set(expected) - actual:
        fail(prefix + name, 'upstream file is missing')
    for name in actual - set(expected):
        fail('[non-native-file]', 'file in native subtree is not in upstream')
    for name in actual & set(expected):
        mode, data = snapshot[prefix + name]
        if hashlib.sha256(data).hexdigest() != expected[name]['sha256']:
            fail(prefix + name, 'upstream file differs from pinned original bytes')
        if mode and mode != expected[name]['mode']:
            fail(prefix + name, 'upstream file mode differs')
    return findings, {prefix + name for name in expected} if not findings else set()


def _scan(snapshot: dict[str, tuple[str, bytes]], native: set[str], context: str) -> list[dict]:
    findings = []
    for name, (mode, data) in snapshot.items():
        display = FILENAME_IPV4.sub(lambda m: m.group() if public_address(m.group()) else '[redacted-address]', name)
        display = FILENAME_MASKED.sub(lambda m: m.group() if 'x' not in m.group().lower() or m.group().startswith('192.168.1.') else '[redacted-address]', display)
        display = IPV6.sub(lambda m: m.group() if public_address(m.group()) else '[redacted-address]', display)
        def fail(line, reason):
            findings.append({'path': display, 'line': line, 'reason': context + ': ' + reason})
        if display != name:
            fail(0, 'network address in filename')
        if private_path(Path(name)):
            fail(0, 'local artifact is tracked')
        if mode == '120000':
            fail(0, 'tracked symlink requires explicit review')
            continue
        try:
            text = data.decode('utf-8-sig')
        except UnicodeError:
            fail(0, 'unreadable or binary tracked file')
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if name not in native:
                if any(not public_address(m.group()) for pattern in (IPV4, IPV6) for m in pattern.finditer(line)):
                    fail(number, 'network address outside public example range')
                if any('x' in m.group().lower() and not m.group().startswith('192.168.1.') for m in MASKED.finditer(line)):
                    fail(number, 'use the documented example subnet')
            if SECRET.search(line):
                fail(number, 'credential-shaped value')
    return findings


def audit(root: Path, ref: str | None = None) -> tuple[list[dict], int, bool]:
    root = root.resolve()
    paths, tracked = files_to_check(root)
    snapshot, findings = {}, []
    modes = git_snapshot(root) if tracked else {}
    native_root = root / NATIVE_DIRECTORY
    try:
        manifest_file = root / AUTOMATION_DIRECTORY / 'metadata/upstream_manifest.json'
        if not manifest_file.exists():
            manifest_file = root / AUTOMATION_DIRECTORY / 'upstream_manifest.json'
        expected = _manifest(manifest_file.read_bytes())
    except (OSError, ValueError, KeyError, TypeError):
        expected = {}
    known = {p.as_posix() for p in paths}
    for path in native_root.rglob('*') if native_root.is_dir() else []:
        if path.is_dir() and not path.is_symlink():
            relative = path.relative_to(native_root).as_posix() + '/'
            if expected and not any(name.startswith(relative) for name in expected):
                findings.append({'path': '[non-native-directory]', 'line': 0, 'reason': 'workspace: extra native directory'})
        elif path.relative_to(root).as_posix() not in known:
            paths.append(path.relative_to(root))
    for relative in paths:
        path, name = root / relative, relative.as_posix()
        try:
            mode = '120000' if path.is_symlink() else (modes.get(name, ('', b''))[0] if tracked else ('' if os.name == 'nt' else ('100755' if path.stat().st_mode & 0o111 else '100644')))
            snapshot[name] = (mode, os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
        except OSError:
            findings.append({'path': '[unreadable-source]', 'line': 0, 'reason': 'workspace: missing source file'})
    native_findings, verified = _verify_snapshot(snapshot, 'workspace')
    findings.extend(native_findings)
    findings.extend(_scan(snapshot, verified, 'workspace'))
    if tracked:
        published = git_snapshot(root, ref)
        native_findings, verified = _verify_snapshot(published, 'Git tree' if ref else 'Git index')
        findings.extend(native_findings)
        findings.extend(_scan(published, verified, 'Git tree' if ref else 'Git index'))
    return findings, len(snapshot), tracked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    location = Path(__file__).resolve().parent
    if location.name == 'tools':
        location = location.parent
    parser.add_argument('--root', type=Path, default=location.parent)
    parser.add_argument('--report-file', default='')
    parser.add_argument('--ref', help='also audit this actual commit or tree')
    parser.add_argument('--require-clean', action='store_true')
    args = parser.parse_args()
    findings, count, tracked = audit(args.root, args.ref)
    if args.require_clean and tracked:
        for arguments in (('diff', '--quiet'), ('diff', '--cached', '--quiet')):
            if subprocess.run(['git', '-C', str(args.root), *arguments], capture_output=True).returncode:
                findings.append({'path': '[working-tree]', 'line': 0, 'reason': 'uncommitted tracked source'})
    if args.report_file:
        Path(args.report_file).write_text(json.dumps({'findings': findings, 'files': count}, indent=2), encoding='utf-8')
    print(f"FILES={count} MODE={'tracked' if tracked else 'archive'} FAIL={len(findings)}")
    for finding in findings:
        print(json.dumps(finding, ensure_ascii=False))
    return int(bool(findings))


if __name__ == '__main__':
    raise SystemExit(main())
