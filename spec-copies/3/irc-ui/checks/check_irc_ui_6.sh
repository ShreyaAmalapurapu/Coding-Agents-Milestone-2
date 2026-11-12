#!/usr/bin/env bash
# Runs rules 38-45 of the irc-ui check (see irc-ui-6-ui-messages.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=6 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_irc_ui_core.py"
