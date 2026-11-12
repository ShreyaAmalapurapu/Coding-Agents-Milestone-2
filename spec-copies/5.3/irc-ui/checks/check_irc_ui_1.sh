#!/usr/bin/env bash
# Runs rules 1-9 of the irc-ui check (see irc-ui-1-edits.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=1 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_irc_ui_core.py"
