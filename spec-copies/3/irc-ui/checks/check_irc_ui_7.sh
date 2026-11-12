#!/usr/bin/env bash
# Runs rules 46-53 of the irc-ui check (see irc-ui-7-ui-flows.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=7 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_irc_ui_core.py"
