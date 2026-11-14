#!/bin/bash
set -u
echo $$ >/tmp/start.pid
mkdir -p /app/data
redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/redis.log 2>&1 & R=$!
pids=(); IRC=''
for i in 0 1 2; do NODE_ID=$i python3 /app/app.py $((8000+i)) >/tmp/node$i.log 2>&1 & pids+=($!); done
python3 /app/irc.py >/tmp/irc.log 2>&1 & IRC=$!
trap 'kill "$R" "$IRC" "${pids[@]}" 2>/dev/null; exit 0' TERM INT
while :; do
 kill -0 "$R" 2>/dev/null || { redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/redis.log 2>&1 & R=$!; }
 kill -0 "$IRC" 2>/dev/null || { python3 /app/irc.py >/tmp/irc.log 2>&1 & IRC=$!; }
 for i in 0 1 2; do kill -0 "${pids[$i]}" 2>/dev/null || { NODE_ID=$i python3 /app/app.py $((8000+i)) >/tmp/node$i.log 2>&1 & pids[$i]=$!; }; done
 sleep 1
done
