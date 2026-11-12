#!/bin/sh
set -eu
echo $$ >/tmp/start.pid
trap 'kill 0 2>/dev/null || true; exit 0' TERM INT
while :; do
  if ! ss -ltn "sport = :6379" | grep -q LISTEN; then
    redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no >>/tmp/redis.log 2>&1 &
  fi
  if ! ss -ltn "sport = :6667" | grep -q LISTEN; then
    python3 /app/irc.py >>/tmp/irc.log 2>&1 &
  fi
  for n in 0 1 2; do
    p=$((8000+n))
    if ! ss -ltn "sport = :$p" | grep -q LISTEN; then NODE_ID=$n PORT=$p python3 /app/server.py >>/tmp/node$n.log 2>&1 &
    fi
  done
  sleep 1
done
