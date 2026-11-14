#!/bin/sh
set -u
mkdir -p /app/data
redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/redis.log 2>&1 &
R=$!
P0=0; P1=0; P2=0; P3=0
trap 'kill "$R" "$P0" "$P1" "$P2" "$P3" 2>/dev/null || true; exit 0' TERM INT
while :; do
  if [ "$P0" -eq 0 ] || ! kill -0 "$P0" 2>/dev/null; then NODE_ID=0 PORT=8000 python3 /app/app.py >/tmp/node0.log 2>&1 & P0=$!; fi
  if [ "$P1" -eq 0 ] || ! kill -0 "$P1" 2>/dev/null; then NODE_ID=1 PORT=8001 python3 /app/app.py >/tmp/node1.log 2>&1 & P1=$!; fi
  if [ "$P2" -eq 0 ] || ! kill -0 "$P2" 2>/dev/null; then NODE_ID=2 PORT=8002 python3 /app/app.py >/tmp/node2.log 2>&1 & P2=$!; fi
  if [ "$P3" -eq 0 ] || ! kill -0 "$P3" 2>/dev/null; then python3 /app/irc.py >/tmp/irc.log 2>&1 & P3=$!; fi
  if [ "$R" -eq 0 ] || ! kill -0 "$R" 2>/dev/null; then redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/redis.log 2>&1 & R=$!; fi
  sleep 1
done
