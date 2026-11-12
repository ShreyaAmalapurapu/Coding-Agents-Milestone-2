#!/bin/sh
echo $$ >/tmp/start.pid
mkdir -p /app/data
pids=""
trap 'kill $pids 2>/dev/null; exit 0' TERM INT
while :; do
 for i in 0 1 2; do p=$(cat /tmp/huddle_$i.pid 2>/dev/null); if ! kill -0 "$p" 2>/dev/null; then NODE_ID=$i nohup python3 /app/app.py --node "$i" >/tmp/huddle_$i.log 2>&1 & echo $! >/tmp/huddle_$i.pid; pids="$pids $!"; fi; done
 p=$(cat /tmp/huddle_irc.pid 2>/dev/null); if ! kill -0 "$p" 2>/dev/null; then nohup python3 /app/irc.py >/tmp/huddle_irc.log 2>&1 & echo $! >/tmp/huddle_irc.pid; pids="$pids $!"; fi
 sleep 1
done
