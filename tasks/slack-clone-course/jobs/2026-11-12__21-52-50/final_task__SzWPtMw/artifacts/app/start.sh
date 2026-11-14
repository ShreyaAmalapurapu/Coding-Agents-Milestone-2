#!/bin/sh
mkdir -p /app/data
echo $$ >/tmp/start.pid
trap 'kill "$rp" "$p0" "$p1" "$p2" 2>/dev/null || true; exit 0' TERM INT
start_redis() {
  redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/redis.log 2>&1 &
  rp=$!
}
start_node() {
  n="$1"
  NODE_ID="$n" python3 /app/server.py >/tmp/node-$n.log 2>&1 &
  eval "p$n=$!"
}
start_redis
start_node 0
start_node 1
start_node 2
while :; do
  sleep 1
  if ! kill -0 "$rp" 2>/dev/null; then start_redis; fi
  if ! kill -0 "$p0" 2>/dev/null; then start_node 0; fi
  if ! kill -0 "$p1" 2>/dev/null; then start_node 1; fi
  if ! kill -0 "$p2" 2>/dev/null; then start_node 2; fi
done
