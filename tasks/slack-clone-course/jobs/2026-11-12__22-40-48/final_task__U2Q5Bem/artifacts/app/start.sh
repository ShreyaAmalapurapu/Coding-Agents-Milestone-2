#!/bin/sh
mkdir -p /app/data
echo $$ >/tmp/start.pid
python3 -c 'import sys;sys.path.insert(0,"/app");import app' 
python3 /app/irc.py &
IP=$!
trap 'kill $IP 2>/dev/null; exit 0' TERM INT
while :; do
 for p in 8000 8001 8002; do
  if ! (echo >/dev/tcp/127.0.0.1/$p) 2>/dev/null; then PORT=$p python3 -c 'import os,sys;sys.path.insert(0,"/app");from app import S,H; S(("127.0.0.1",int(os.environ["PORT"])),H).serve_forever()' >/tmp/node-$p.log 2>&1 & echo $! >/tmp/node-$p.pid; fi
 done
 kill -0 $IP 2>/dev/null || { python3 /app/irc.py & IP=$!; }
 sleep 1
done
