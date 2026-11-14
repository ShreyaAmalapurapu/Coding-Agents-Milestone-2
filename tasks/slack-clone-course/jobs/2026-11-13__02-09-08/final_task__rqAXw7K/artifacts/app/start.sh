#!/bin/sh
set -eu
echo $$ > /tmp/start.pid
mkdir -p /app/data
pr= pi= p0= p1= p2=
trap 'kill "$pr" "$pi" "$p0" "$p1" "$p2" 2>/dev/null || true; exit 0' TERM INT
while :; do
  if [ -z "$pr" ] || ! kill -0 "$pr" 2>/dev/null; then
    redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
    pr=$!
  fi
  if [ -z "$pi" ] || ! kill -0 "$pi" 2>/dev/null; then
    python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 &
    pi=$!
  fi
  if [ -z "$p0" ] || ! kill -0 "$p0" 2>/dev/null; then
    NODE_ID=0 PORT=8000 python3 /app/server.py >/tmp/huddle-0.log 2>&1 &
    p0=$!
  fi
  if [ -z "$p1" ] || ! kill -0 "$p1" 2>/dev/null; then
    NODE_ID=1 PORT=8001 python3 /app/server.py >/tmp/huddle-1.log 2>&1 &
    p1=$!
  fi
  if [ -z "$p2" ] || ! kill -0 "$p2" 2>/dev/null; then
    NODE_ID=2 PORT=8002 python3 /app/server.py >/tmp/huddle-2.log 2>&1 &
    p2=$!
  fi
  sleep 1
done
