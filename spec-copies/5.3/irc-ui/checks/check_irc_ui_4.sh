#!/usr/bin/env bash
# Runs rules 26-30 of the irc-ui check (see irc-ui-4-ui-markup.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=4 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_irc_ui_core.py"
