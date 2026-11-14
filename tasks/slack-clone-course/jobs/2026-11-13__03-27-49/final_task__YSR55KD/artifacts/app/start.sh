#!/bin/sh
set -eu
echo $$ >/tmp/start.pid
redis_pid=""
irc_pid=""
pids=""
trap 'for p in $pids $redis_pid $irc_pid; do kill "$p" 2>/dev/null || true; done; exit 0' TERM INT
while :; do
  if ! ss -ltn "sport = :6379" 2>/dev/null | grep -q LISTEN; then
    redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
    redis_pid=$!
  fi
  if ! ss -ltn "sport = :6667" 2>/dev/null | grep -q LISTEN; then
    python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 &
    irc_pid=$!
  fi
  for i in 0 1 2; do
    port=$((8000+i))
    if ! ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; then
      NODE_ID=$i python3 /app/server.py "$port" >/tmp/huddle-$i.log 2>&1 &
      pids="$pids $!"
    fi
  done
  sleep 1
done
