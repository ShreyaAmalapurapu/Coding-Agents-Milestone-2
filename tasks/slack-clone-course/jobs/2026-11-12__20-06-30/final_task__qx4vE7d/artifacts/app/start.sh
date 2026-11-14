#!/bin/sh
set -u
echo $$ >/tmp/start.pid
pids=""
trap 'for p in $pids; do kill "$p" 2>/dev/null || true; done; exit 0' TERM INT EXIT
start_redis(){ redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/redis.log 2>&1 & rp=$!; pids="$pids $rp"; }
start_node(){ 
  port="$1"
  old="$(eval "printf '%s' \\$p$port" 2>/dev/null || true)"
  python3 /app/server.py "$port" >/tmp/node-$port.log 2>&1 &
  new=$!
  eval "p$port=$new"
  pids="$pids $new"
}
start_redis
start_node 8000; start_node 8001; start_node 8002
while :; do
  sleep 1
  if ! ss -ltn 2>/dev/null | grep -q ":8000 "; then start_node 8000; fi
  if ! ss -ltn 2>/dev/null | grep -q ":8001 "; then start_node 8001; fi
  if ! ss -ltn 2>/dev/null | grep -q ":8002 "; then start_node 8002; fi
  if ! ss -ltn 2>/dev/null | grep -q ":6379 "; then start_redis; fi
done
