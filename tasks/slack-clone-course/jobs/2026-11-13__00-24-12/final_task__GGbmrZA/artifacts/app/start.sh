#!/bin/sh
set -eu
echo $$ >/tmp/start.pid
mkdir -p /app/data
redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/redis.log 2>&1 &
RP=$!
for p in 8000 8001 8002; do PORT=$p python3 /app/server.py >/tmp/node-$p.log 2>&1 & echo $! > /tmp/node-$p.pid; done
IRC_PID=''
IRC_PID=$(python3 /app/irc.py >/tmp/irc.log 2>&1 & echo $!)
echo $IRC_PID >/tmp/irc.pid
trap 'kill $RP $(cat /tmp/node-8000.pid) $(cat /tmp/node-8001.pid) $(cat /tmp/node-8002.pid) $(cat /tmp/irc.pid) 2>/dev/null || true' INT TERM EXIT
while :; do
 kill -0 $RP 2>/dev/null || { redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no >/tmp/redis.log 2>&1 & RP=$!; }
 for p in 8000 8001 8002; do pid=$(cat /tmp/node-$p.pid); kill -0 $pid 2>/dev/null || { PORT=$p python3 /app/server.py >/tmp/node-$p.log 2>&1 & echo $! > /tmp/node-$p.pid; }; done
 sleep 1
done
