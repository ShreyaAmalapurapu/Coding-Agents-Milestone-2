#!/usr/bin/env bash
# Runs rules 18-25 of the irc-ui check (see irc-ui-3-irc-bridge.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=3 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_irc_ui_core.py"
