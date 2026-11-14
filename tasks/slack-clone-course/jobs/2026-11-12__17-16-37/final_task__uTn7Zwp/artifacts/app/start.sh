#!/bin/sh
mkdir -p /app/data
echo $$ >/tmp/start.pid
pids=""
trap 'kill $pids 2>/dev/null; exit 0' TERM INT
for i in 0 1 2; do PORT=$((8000+i)) NODE_ID=$i python3 /app/server.py >/tmp/huddle-$i.log 2>&1 & pids="$pids $!"; done
wait
