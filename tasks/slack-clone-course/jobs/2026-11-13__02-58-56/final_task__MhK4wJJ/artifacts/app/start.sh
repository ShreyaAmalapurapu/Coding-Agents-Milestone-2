#!/bin/sh
set -eu
echo $$ >/tmp/start.pid
pids=""
trap 'for p in $pids; do kill "$p" 2>/dev/null || true; done; exit 0' INT TERM EXIT
while :; do
  if ! ss -ltn "sport = :6379" 2>/dev/null | grep -q LISTEN; then
    redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no >/tmp/huddle-redis.log 2>&1 &
    pids="$pids $!"
  fi
  if ! ss -ltn "sport = :6667" 2>/dev/null | grep -q LISTEN; then
    python3 /app/irc.py >/tmp/huddle-irc.log 2>&1 &
    pids="$pids $!"
  fi
  for n in 0 1 2; do
    port=$((8000+n))
    if ! ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; then
      PORT=$port NODE_ID=$n python3 /app/server.py >/tmp/huddle-$n.log 2>&1 &
      pids="$pids $!"
    fi
  done
  sleep 1
done
