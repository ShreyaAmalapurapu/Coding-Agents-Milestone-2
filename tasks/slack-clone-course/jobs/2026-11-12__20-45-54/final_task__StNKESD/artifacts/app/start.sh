#!/bin/sh
echo $$ >/tmp/start.pid
mkdir -p /app/data
python3 -u /app/irc.py >/tmp/irc.log 2>&1 &
ip=$!
pids="$ip"
i=0
while :; do
 for n in 0 1 2; do
  port=$((8000+n))
  if ! ss -ltn "sport = :$port" | grep -q LISTEN; then NODE=$n python3 -u /app/app.py >/tmp/node$n.log 2>&1 & pids="$pids $!"; fi
 done
 kill -0 "$ip" 2>/dev/null || { python3 -u /app/irc.py >/tmp/irc.log 2>&1 & ip=$!; }
 sleep 1
done
