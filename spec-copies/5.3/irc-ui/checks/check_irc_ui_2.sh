#!/usr/bin/env bash
# Runs rules 10-17 of the irc-ui check (see irc-ui-2-irc-basics.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=2 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_irc_ui_core.py"
