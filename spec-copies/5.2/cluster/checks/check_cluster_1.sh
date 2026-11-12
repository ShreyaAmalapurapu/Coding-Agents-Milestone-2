#!/usr/bin/env bash
# Runs rules 1-5 of the cluster check (see cluster-1-redis-fanout.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=1 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_cluster_core.py"
