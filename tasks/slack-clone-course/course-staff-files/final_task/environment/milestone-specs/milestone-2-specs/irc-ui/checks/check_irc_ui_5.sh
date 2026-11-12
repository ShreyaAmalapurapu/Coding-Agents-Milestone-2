#!/usr/bin/env bash
# Runs rules 31-37 of the irc-ui check (see irc-ui-5-ui-browser.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=5 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_irc_ui_core.py"
