#!/usr/bin/env bash
# All 56 features rules (C.1 to C.5), one total.
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_features_core.py"
