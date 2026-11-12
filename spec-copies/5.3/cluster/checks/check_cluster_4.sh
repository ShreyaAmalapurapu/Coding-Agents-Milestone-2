#!/usr/bin/env bash
# Runs rules 19-21 of the cluster check (see cluster-4-performance.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=4 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_cluster_core.py"
