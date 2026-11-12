#!/usr/bin/env bash
# Runs rules 1-13 of the features check (see features-1-*.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=1 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_features_core.py"
