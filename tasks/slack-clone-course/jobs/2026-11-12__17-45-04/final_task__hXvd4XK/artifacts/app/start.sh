#!/bin/sh
set -eu
mkdir -p /app/data
echo $$ > /tmp/start.pid
python3 -c 'import sys;sys.path.insert(0,"/app");import server;server.init()'
pids=""
cleanup() {
  trap - TERM INT EXIT
  for p in $pids; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -f /tmp/start.pid
  exit 0
}
trap cleanup TERM INT EXIT
start_node() {
  port="$1"; node="$2"
  PORT="$port" NODE_ID="$node" python3 /app/server.py >>"/tmp/node${node}.log" 2>&1 &
  echo "$!"
}
p0=$(start_node 8000 0); p1=$(start_node 8001 1); p2=$(start_node 8002 2)
pids="$p0 $p1 $p2"
while :; do
  for spec in "8000 0 p0" "8001 1 p1" "8002 2 p2"; do
    set -- $spec
    port="$1"; node="$2"; var="$3"
    eval "pid=\${$var}"
    if ! kill -0 "$pid" 2>/dev/null; then
      newpid=$(start_node "$port" "$node")
      eval "$var=$newpid"
      pids="$pids $newpid"
    fi
  done
  sleep 1
done
