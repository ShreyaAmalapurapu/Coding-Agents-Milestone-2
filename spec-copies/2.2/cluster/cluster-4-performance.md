# Cluster.4: delivery speed under load

Covers: WebSocket `message.created` latency and loss while all three nodes take writes at once.
Check: `bash /spec/cluster/checks/check_cluster_4.sh`. The rule numbers below match its output.

Do not start this spec until `check_cluster_3.sh` prints `RESULT: 6 passed, 0 failed`.

The conventions in `cluster-1-redis-fanout.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| `post_message`, `append_event`, `broadcast`, `fallback_fanout` | The write and delivery path being measured |

## Adds

Nothing new is required if the earlier specs are built well. Make the path fast:

| Function | What it does |
|---|---|
| `deliver(frame)` | Sends one frame to each local subscriber without waiting on slow sockets (one send queue or task per socket) |

## Rules (19-21)

The check opens 6 sockets subscribed to one channel (2 on each node). Then 3 writers, one per
node, each post 20 messages about 50 ms apart, all at the same time (60 messages, 360 expected
deliveries). Latency is the time from just before the `POST` is sent to the moment the socket
receives that message's `message.created`.

19. **No loss.** At most 1% of the expected deliveries are missing (every socket should receive
    every message).

20. **Median.** The median latency is at most 300 ms.

21. **p95.** The 95th percentile latency is at most 800 ms.

## Notes

- Do the database write and the publish in the request itself and return `201` right away;
  do not wait for delivery to finish before answering.
- Do not do heavy work per frame (for example reloading the whole channel or re-reading every
  event). The fallback loop from Cluster.3 should read only events newer than the last one
  delivered, using an index.
- Use SQLite in WAL mode with a busy timeout (for example 5 seconds), or a similar shared store,
  so concurrent writers from three processes wait instead of failing.
