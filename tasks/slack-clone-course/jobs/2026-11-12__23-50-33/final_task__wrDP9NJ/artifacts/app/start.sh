#!/bin/sh
echo $$ >/tmp/start.pid
mkdir -p /app/data
redis-server --bind 127.0.0.1 --port 6379 --daemonize yes >/tmp/redis.log 2>&1
while :; do
 for p in 8000 8001 8002; do if ! ss -ltn "sport = :$p" | grep -q LISTEN; then python3 /app/server.py "$p" >/tmp/node-$p.log 2>&1 & echo $! >/tmp/node-$p.pid; fi; done
 if ! ss -ltn "sport = :6667" | grep -q LISTEN; then python3 /app/server.py irc >/tmp/irc.log 2>&1 & echo $! >/tmp/irc.pid; fi
 if ! (printf 'PING\r\n' | nc -w 1 127.0.0.1 6379 2>/dev/null | grep -q PONG); then redis-server --bind 127.0.0.1 --port 6379 --daemonize yes >/tmp/redis.log 2>&1; fi
 sleep 1
done
