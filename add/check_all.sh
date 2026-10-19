#!/usr/bin/env bash
# Run from any directory. All custom validation stays inside add/.
set -euo pipefail
ADD_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ADD_DIR/.."
python3 -B add/validate_publication.py
python3 -B add/validate_addon.py
python3 -B -m unittest discover -s add -p 'test_*.py' -v
