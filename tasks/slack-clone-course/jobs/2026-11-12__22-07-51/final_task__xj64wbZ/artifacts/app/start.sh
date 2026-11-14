#!/bin/sh
mkdir -p /app/data
trap 'kill $(cat /tmp/chat.pids 2>/dev/null) 2>/dev/null; exit 0' TERM INT
: > /tmp/chat.pids
for n in 0 1 2; do PORT=$((8000+n)) NODE_ID=$n python3 /app/server.py & echo $! >> /tmp/chat.pids; done
python3 /app/irc.py & echo $! >> /tmp/chat.pids
wait
