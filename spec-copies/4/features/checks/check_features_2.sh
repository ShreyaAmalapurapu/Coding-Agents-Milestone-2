#!/usr/bin/env bash
# Runs rules 14-23 of the features check (see features-2-*.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=2 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_features_core.py"
