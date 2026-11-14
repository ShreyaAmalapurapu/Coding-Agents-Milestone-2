#!/bin/sh
set -eu
echo $$ >/tmp/start.pid
mkdir -p /app/data
redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/redis.log 2>&1 &
r=$!
pids=""
PORT=0 python3 /app/irc.py >/tmp/irc.log 2>&1 & irc=$!
trap 'kill $r $irc $pids 2>/dev/null || true; exit 0' TERM INT EXIT
for port in 8000 8001 8002; do PORT=$port python3 /app/server.py >/tmp/node-$port.log 2>&1 & pids="$pids $!"; done
while :; do
 for port in 8000 8001 8002; do
  if ! ss -ltn "sport = :$port" | grep -q LISTEN; then PORT=$port python3 /app/server.py >/tmp/node-$port.log 2>&1 & pids="$pids $!"; fi
 done
 if ! kill -0 "$irc" 2>/dev/null; then PORT=0 python3 /app/irc.py >/tmp/irc.log 2>&1 & irc=$!; fi
 if ! kill -0 "$r" 2>/dev/null; then redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/redis.log 2>&1 & r=$!; fi
 sleep 1
done
