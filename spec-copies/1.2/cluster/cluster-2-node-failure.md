# Cluster.2: a killed HTTP node

Covers: reads, writes, WebSocket delivery and replay on the two surviving nodes while one HTTP
node is dead, and the dead node coming back by itself.
Check: `bash /spec/cluster/checks/check_cluster_2.sh`. The rule numbers below match its output.
The check kills the node on port `8001` itself (by the PID that owns the port), so run it as a
command of its own.

Do not start this spec until `check_cluster_1.sh` prints `RESULT: 5 passed, 0 failed`.

The conventions in `cluster-1-redis-fanout.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| `/app/start.sh` supervisor | Restarts the killed node |
| `post_message`, `list_messages`, `broadcast` | Keep working on the surviving nodes |
| `events_since` | Replays missed events when a client resumes on another node |

## Adds

| Function | What it does |
|---|---|
| `on_socket_closed(ws)` | Cleans up a socket whose node died, or that closed on its own, without affecting the other nodes |

Nothing else is new: this spec is about keeping every node independent. A node must not hold
anything the others need (no state only in one node's memory, no node acting as the leader).

## Rules (6-12)

The check subscribes one socket on `8001` and one on `8002`, posts one message, then sends
`SIGKILL` to the process listening on `8001`.

6. **Others keep answering.** Within half a second of the kill, `8000` and `8002` answer
   `GET /api/health` with `200` and `status` `"ok"`.

7. **Writes.** Posting a message through `8000` and through `8002` returns `201`.

8. **Reads.** `GET /api/channels/{id}/messages` on `8000` and on `8002` lists the message written
   before the kill and both written during it, newest first.

9. **Live delivery.** The socket on `8002` receives both new messages as `message.created` with
   the next two `event_id`s.

10. **Replay across a reconnect.** The client whose socket was on `8001` connects to `8000` and
    sends `{"type": "resume", "channel_id": <id>, "since_event_id": <last id it saw>}`. It gets
    the two missed events in order, then `{"type": "resumed", "channel_id": <id>,
    "head_event_id": <last id + 2>}`.

11. **The node comes back.** Within 15 seconds, with no command from anyone, `8001` answers
    `GET /api/health` with `200` and `node_id` `1` again.

12. **And catches up.** The restarted `8001` lists the messages written while it was down, and a
    message posted through it reaches the socket on `8002` with the next `event_id`.

## Notes

- IRC bridging while a node is dead is checked in the IRC component (it needs the IRC gateway).
- The supervisor in `/app/start.sh` should check its children about once a second
  (`kill -0 <pid>` or `wait -n`) and start a fresh one at once. A restarted node must not reset
  the shared database or the event counters.
