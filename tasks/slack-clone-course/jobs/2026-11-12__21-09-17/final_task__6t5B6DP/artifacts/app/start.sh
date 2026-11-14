#!/bin/sh
mkdir -p /app/data
echo $$ >/tmp/start.pid
pids=""
trap 'kill $pids 2>/dev/null; exit 0' TERM INT
for p in 8000 8001 8002; do python3 /app/app.py $p >>/tmp/huddle-$p.log 2>&1 & pids="$pids $!"; done
wait
