#!/bin/sh
set -u
echo $$ >/tmp/start.pid
mkdir -p /app/data
redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/redis.log 2>&1 &
RPID=$!
P0=0; P1=0; P2=0
trap 'kill "$RPID" "$P0" "$P1" "$P2" 2>/dev/null || true; exit 0' TERM INT EXIT
start_node() {
  port="$1"; var="$2"
  PORT="$port" python3 /app/server.py >/tmp/node-"$port".log 2>&1 &
  eval "$var=\$!"
}
start_node 8000 P0
start_node 8001 P1
start_node 8002 P2
while :; do
  if ! kill -0 "$RPID" 2>/dev/null; then
    redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/redis.log 2>&1 &
    RPID=$!
  fi
  kill -0 "$P0" 2>/dev/null || start_node 8000 P0
  kill -0 "$P1" 2>/dev/null || start_node 8001 P1
  kill -0 "$P2" 2>/dev/null || start_node 8002 P2
  sleep 1
done
