# IrcUi.1: editing and deleting messages

Routes covered: `PATCH /api/messages/{id}`, `DELETE /api/messages/{id}`, and the
`message.updated` and `message.deleted` events.
Check: `bash /spec/irc-ui/checks/check_irc_ui_1.sh`. The rule numbers below match its output.

Your service is launched with `/app/start.sh`, which must stay in the foreground and keep the
cluster alive. The three HTTP nodes listen on `127.0.0.1:8000`, `:8001`, and `:8002`, the IRC
gateway on `0.0.0.0:6667`, and the web UI is served at `GET /` on every node. State written
through one node (or through IRC) must be visible everywhere.

Conventions for every rule (in all five IrcUi specs): JSON in, JSON out; responses use one
top-level resource key; `Authorization: Bearer <token>` on every route, and a missing or invalid
token is `401`; an unknown id is `404`; a bad body is `400`; an authenticated user who is not
allowed is `403`; timestamps are ISO 8601 UTC strings; every request must answer within 5 seconds.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

If Specs Roles and Channels are in `/spec`, these come from there. If they are not, build only
these basics first (see `instruction.md` for each route), and nothing else from those specs:

| Function | The basic it provides |
|---|---|
| `create_user`, `issue_token`, `user_from_token` | `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me` |
| `create_workspace`, `get_workspace`, `get_member` | `POST /api/workspaces` (creates `#general`; the creator is `owner`), `GET /api/workspaces`, `GET /api/workspaces/{slug}` |
| `create_channel`, `join_channel` | `POST /api/workspaces/{slug}/channels`, `POST /api/channels/{id}/join` |
| `post_message`, `list_messages` | `POST /api/channels/{id}/messages` (`201 {message}`, full `MessageObj`), `GET /api/channels/{id}/messages` newest first |
| `ws_endpoint`, `append_event`, `broadcast` | `ws://.../api/ws?token=`, `subscribe` → `subscribed` with `head_event_id`, durable frames with a dense per-channel `event_id` on every node |

## Adds

| Function | What it does |
|---|---|
| `edit_message(message, actor, body)` | Author only; sets `body` and `edited_at`; emits `message.updated` |
| `delete_message(message, actor)` | Author or workspace owner; marks it deleted once and emits `message.deleted` once; a repeat is a no-op |

## Rules (1-9)

1. **Edit.** `PATCH /api/messages/{id}` with `{body}` by the author returns `200` and
   `{message}` with the new `body` and `edited_at` set to the edit time (it is `null` until the
   first edit).

2. **Shared.** The listing on any node shows the new body and `edited_at`.

3. **Edit errors.** Anyone but the author gets `403`, even the workspace owner. An unknown id
   returns `404`. An empty or blank body returns `400`. No token returns `401`.

4. **`message.updated`.** An edit emits a durable `{"type": "message.updated", "event_id",
   "channel_id", "message": <the updated MessageObj>}` with the next `event_id`.

5. **Delete.** `DELETE /api/messages/{id}` by the author returns `200` and exactly
   `{"deleted": true}`. Deleting it again (any node) also returns `200` and `{"deleted": true}`.

6. **Gone from listings.** A deleted message no longer appears in
   `GET /api/channels/{id}/messages`, and its reactions no longer count anywhere.

7. **No editing the dead.** `PATCH` on a deleted message returns `409`.

8. **Who may delete.** The workspace owner may delete anyone's message (`200`). Other members
   get `403`. An unknown id returns `404`.

9. **`message.deleted`.** A delete emits one durable `{"type": "message.deleted", "event_id",
   "channel_id", "message_id"}` with the next `event_id`. A repeated delete emits nothing.
