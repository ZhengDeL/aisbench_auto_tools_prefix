#!/usr/bin/env bash
# Offline gate. Real image/model and numerical acceptance run separately.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AUTOMATION_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RELEASE_ROOT="$(cd -- "$AUTOMATION_ROOT/.." && pwd)"
TESTS="$AUTOMATION_ROOT/tests"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$AUTOMATION_ROOT/core:$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd "$RELEASE_ROOT"
python3 -B "$SCRIPT_DIR/validate_publication.py" --root "$RELEASE_ROOT"
bash -n "$RELEASE_ROOT/run_perf_scenarios.sh"
python3 -B - "$TESTS" "$AUTOMATION_ROOT" <<'PY'
import pathlib, sys, unittest
for path in pathlib.Path(sys.argv[2]).rglob('*.py'):
    compile(path.read_text(encoding='utf-8-sig'), str(path), 'exec')
suite = unittest.defaultTestLoader.discover(sys.argv[1], pattern='test_*.py')
if not suite.countTestCases():
    raise SystemExit('Required test suite is empty; this is not a complete release gate.')
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.skipped:
    print('Required checks were skipped; this is not a complete release gate.', file=sys.stderr)
raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
PY
printf '%s\n' '离线门禁通过；实际镜像、模型、原始数值和功能矩阵须以本版本实机证据验收。'
