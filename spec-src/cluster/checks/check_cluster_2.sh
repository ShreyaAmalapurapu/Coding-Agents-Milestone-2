#!/usr/bin/env bash
# Runs rules 6-12 of the cluster check (see cluster-2-node-failure.md).
PY=/opt/test-venv/bin/python
[ -x "$PY" ] || PY=python3
CHECK_STAGE=2 exec "$PY" "$(dirname "${BASH_SOURCE[0]}")/check_cluster_core.py"
