#!/usr/bin/env bash
# Runs rules 13-18 of the cluster check (see cluster-3-redis-failure.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=3 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_cluster_core.py"
