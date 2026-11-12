#!/usr/bin/env bash
# Runs rules 43-56 of the features check (see features-5-*.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=5 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_features_core.py"
