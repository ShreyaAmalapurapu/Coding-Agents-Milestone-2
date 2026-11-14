#!/bin/sh
set -eu
echo $$ >/tmp/start.pid
trap 'kill $p0 $p1 $p2 2>/dev/null || true; exit 0' TERM INT
python3 /app/server.py >/tmp/huddle0.log 2>&1 & p0=$!
NODE_ID=1 PORT=8001 python3 /app/server.py >/tmp/huddle1.log 2>&1 & p1=$!
NODE_ID=2 PORT=8002 python3 /app/server.py >/tmp/huddle2.log 2>&1 & p2=$!
while :; do
  kill -0 $p0 2>/dev/null || { NODE_ID=0 PORT=8000 python3 /app/server.py >/tmp/huddle0.log 2>&1 & p0=$!; }
  kill -0 $p1 2>/dev/null || { NODE_ID=1 PORT=8001 python3 /app/server.py >/tmp/huddle1.log 2>&1 & p1=$!; }
  kill -0 $p2 2>/dev/null || { NODE_ID=2 PORT=8002 python3 /app/server.py >/tmp/huddle2.log 2>&1 & p2=$!; }
  sleep 1
done
