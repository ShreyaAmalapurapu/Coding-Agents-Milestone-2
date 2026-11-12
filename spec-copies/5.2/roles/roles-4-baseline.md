# Roles.4: the cluster baseline (health, node ids, WebSocket auth, a killed node)

Routes covered: `GET /api/health`, the WebSocket handshake on `/api/ws?token=...`, and how
`/app/start.sh` runs the three HTTP nodes.
Check: `bash /spec/roles/checks/check_roles_4.sh`, or `bash /spec/checks/regression.sh` in a run without it. The rule numbers below
match their output.

These are the four baseline conditions the grader tests first. Each must hold for the whole run,
including after every later spec is built. The check kills one HTTP node on purpose, so it
runs on its own and is not part of `check_roles_all.sh`.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | From |
|---|---|
| `user_from_token(header)` | Roles.1 (the WebSocket handshake uses the same token lookup) |
| `create_user(username, password, display_name)`, `issue_token(user_id)` | Roles.1 |
| the `GET /api/health` route | Roles.1, rule 63 |

## Adds

| Function | What it does |
|---|---|
| `/app/start.sh` supervisor | Stays in the foreground. Starts each HTTP node as **its own process** (one process per port), watches them, and restarts any node that exits or is killed |
| `ws_accept(token)` | Accepts a WebSocket on `/api/ws` only when `token` is a valid bearer token; otherwise rejects or closes it before sending any frame |

## Rules (71-76)

71. **Health on 8000.** `GET /api/health` on `127.0.0.1:8000` returns `200` with a JSON body
    whose `status` is `"ok"` and whose `node_id` is `0` (the number, not a string).

72. **Every node reports its own id.** Ports `8000`, `8001`, and `8002` each return `200` with
    `status` `"ok"` and `node_id` `0`, `1`, and `2`. Across the cluster the set of node ids is
    exactly `{0, 1, 2}`.

73. **WebSocket rejects a bad token.** A WebSocket to
    `ws://127.0.0.1:8000/api/ws?token=<made-up token>` is refused at the handshake, or is closed
    before any frame is sent. It must never stay open.

74. **WebSocket accepts a good token.** A WebSocket to `ws://127.0.0.1:8000/api/ws?token=<valid
    token>` opens and stays open. (Rule 73 is not met by rejecting everyone.)

75. **One node dies, the others keep serving.** When the process that listens on port `8001`
    is killed with `SIGKILL`, ports `8000` and `8002` still return `200` with `status` `"ok"`
    from `GET /api/health`. This only works if each node is a separate process: if one
    process listens on all three ports, killing it takes the whole cluster down.

76. **The killed node comes back by itself.** After rule 75, port `8001` answers
    `GET /api/health` with `200` and `node_id` `1` again within 15 seconds, with no command from
    you or the check. `/app/start.sh` must notice that the node died and start it again.

## Notes

- Find a node's process with `ss -ltnp` (the PID that owns the port) and stop it by that PID.
  Never stop processes by name (`pkill -f`, `killall`, `pgrep -f`): that can kill you.
- The restarted node must share the same storage as before, so everything written before the
  kill is still visible on all three nodes.
