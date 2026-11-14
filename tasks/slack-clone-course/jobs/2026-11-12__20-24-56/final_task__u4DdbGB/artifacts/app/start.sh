#!/bin/sh
mkdir -p /app/data
python3 /app/app.py 8000 & p1=$!
python3 /app/app.py 8001 & p2=$!
python3 /app/app.py 8002 & p3=$!
python3 /app/app.py irc & p4=$!
echo $$ >/tmp/start.pid
trap 'kill $p1 $p2 $p3 $p4 2>/dev/null; exit' TERM INT
while :; do sleep 1; kill -0 $p1 2>/dev/null || python3 /app/app.py 8000 & p1=$!; kill -0 $p2 2>/dev/null || python3 /app/app.py 8001 & p2=$!; kill -0 $p3 2>/dev/null || python3 /app/app.py 8002 & p3=$!; kill -0 $p4 2>/dev/null || python3 /app/app.py irc & p4=$!; done
