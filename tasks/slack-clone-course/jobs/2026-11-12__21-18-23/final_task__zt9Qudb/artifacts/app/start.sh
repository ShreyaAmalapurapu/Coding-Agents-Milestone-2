#!/bin/sh
mkdir -p /app/data
echo $$ >/tmp/start.pid
trap 'kill $P1 $P2 $P3 2>/dev/null; exit 0' TERM INT
NODE_ID=0 python3 /app/app.py >/tmp/node0.log 2>&1 & P1=$!
NODE_ID=1 python3 /app/app.py >/tmp/node1.log 2>&1 & P2=$!
NODE_ID=2 python3 /app/app.py >/tmp/node2.log 2>&1 & P3=$!
while kill -0 $P1 2>/dev/null || kill -0 $P2 2>/dev/null || kill -0 $P3 2>/dev/null; do
 for x in "$P1:0" "$P2:1" "$P3:2"; do pid=${x%:*}; n=${x#*:}; if ! kill -0 "$pid" 2>/dev/null; then NODE_ID=$n python3 /app/app.py >/tmp/node$n.log 2>&1 & eval P$((n+1))=$!; fi; done
 sleep 1
done
