#!/usr/bin/env bash
# Runs rules 32-42 of the features check (see features-4-*.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=4 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_features_core.py"
