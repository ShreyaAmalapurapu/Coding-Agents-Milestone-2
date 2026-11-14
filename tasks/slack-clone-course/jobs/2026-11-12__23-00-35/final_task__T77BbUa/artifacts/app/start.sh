#!/bin/sh
echo $$ >/tmp/start.pid
mkdir -p /app/data
start_redis() {
  redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/redis.log 2>&1 &
  RP=$!
}
start_node() {
  case "$1" in
    8000) PORT=8000 python3 /app/app.py >/tmp/node8000.log 2>&1 & P0=$!;;
    8001) PORT=8001 python3 /app/app.py >/tmp/node8001.log 2>&1 & P1=$!;;
    8002) PORT=8002 python3 /app/app.py >/tmp/node8002.log 2>&1 & P2=$!;;
  esac
}
start_redis
start_node 8000
start_node 8001
start_node 8002
PORT=6667 python3 /app/irc.py >/tmp/irc.log 2>&1 & PI=$!
trap 'kill "$RP" "$P0" "$P1" "$P2" "$PI" 2>/dev/null; exit 0' TERM INT
while :; do
  kill -0 "$RP" 2>/dev/null || start_redis
  kill -0 "$P0" 2>/dev/null || start_node 8000
  kill -0 "$P1" 2>/dev/null || start_node 8001
  kill -0 "$P2" 2>/dev/null || start_node 8002
  kill -0 "$PI" 2>/dev/null || { python3 /app/irc.py >/tmp/irc.log 2>&1 & PI=$!; }
PORT=6667 python3 /app/irc.py >/tmp/irc.log 2>&1 & PI=$!
  sleep 1
done
