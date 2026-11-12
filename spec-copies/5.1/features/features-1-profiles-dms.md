# Features.1: profiles and direct messages

Routes covered: `GET /api/users/{id}`, `PATCH /api/users/me`, the `user.updated` WebSocket frame,
`POST /api/dms`.
Check: `bash /spec/features/checks/check_features_1.sh`. The rule numbers below match its output.

Your service is launched with `/app/start.sh`. The three HTTP nodes listen on
`127.0.0.1:8000`, `:8001`, and `:8002`. State written through one node must be visible through
the other two.

Conventions for every rule (in all five Features specs): JSON in, JSON out; responses use one
top-level resource key (`user`, `message`, `file`, `group`, `groups`, `reactions`, ...);
`Authorization: Bearer <token>` on every route, and a missing or invalid token is `401`; an
unknown id is `404`; a bad body is `400`; an authenticated user who is not allowed is `403`;
timestamps are ISO 8601 UTC strings such as `2026-05-16T08:07:34.123Z`; every request must
answer within 5 seconds.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

If Specs Roles and Channels are in `/spec`, these come from there. If they are not, build only
these basics first (see `instruction.md` for each route), and nothing else from those specs:

| Function | The basic it provides |
|---|---|
| `create_user`, `issue_token`, `user_from_token` | `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me` |
| `create_workspace`, `get_member` | `POST /api/workspaces` (creates `#general`), workspace membership |
| `create_channel`, `join_channel`, `list_channel_members` | `POST /api/workspaces/{slug}/channels` (public and private), `POST /api/channels/{id}/join`, `GET /api/channels/{id}/members` |
| `post_message`, `list_messages` | `POST /api/channels/{id}/messages` returning the full `message`, `GET /api/channels/{id}/messages` newest first |
| `ws_endpoint`, `append_event`, `broadcast` | `ws://.../api/ws?token=`, `subscribe` answered by `subscribed` with `head_event_id`, durable `message.created` frames with a dense per-channel `event_id` |

## Adds

| Function | What it does |
|---|---|
| `get_user(user_id)` | The `UserObj` or `None` |
| `update_profile(user, fields)` | Validates and saves any subset of the profile fields, then calls `broadcast_global` |
| `broadcast_global(frame)` | Sends a frame to **every** open WebSocket on every node, subscribed or not |
| `open_dm(user, other_user_id)` | Finds or creates the one DM channel for this pair of users |

## Rules (1-13)

`UserObj` has at least `{id, username, display_name, timezone, avatar_url, status_text,
status_emoji}`. The profile fields are `display_name`, `timezone`, `avatar_url`, `status_text`,
`status_emoji`.

1. **Get a user.** `GET /api/users/{id}` returns `200` and `{user}` with every `UserObj` field.

2. **Errors.** An unknown id returns `404`. No token returns `401`.

3. **Partial update.** `PATCH /api/users/me` accepts any subset of the profile fields, changes
   only the fields sent, and returns `200` and `{user}` (the full updated user). Sending
   `{display_name}` after `{status_text, status_emoji}` keeps the status fields.

4. **Shared.** The change is visible right away in `GET /api/users/{id}` on `8002` and in
   `GET /api/auth/me` on `8001`.

5. **Timezone.** `timezone` must be an IANA zone name such as `America/New_York` or `UTC`
   (Python: valid if `zoneinfo.ZoneInfo(name)` works). An invalid one such as `Mars/Phobos`
   returns `400` and changes nothing.

6. **Length limits.** `display_name` is 1 to 80 characters, `status_text` 0 to 100,
   `status_emoji` 0 to 64, `avatar_url` 0 to 2048. Anything outside these returns `400`, and
   nothing in that request is saved.

7. **Validation.** No token returns `401`. A profile field that is not a string (for example
   `"display_name": 123`) returns `400`.

8. **`user.updated`.** After a profile update, every open WebSocket on every node (subscribed to
   any channel or not) receives `{"type": "user.updated", "user": <UserObj>}` within 3 seconds.
   This frame has **no** `channel_id` and **no** `event_id`, and does not use a channel's
   event sequence.

9. **Open a DM.** `POST /api/dms` with `{user_id}` returns `200` and `{channel_id}`. Calling it
   again, and calling it from the other user with the first user's id, returns the **same**
   `channel_id`.

10. **Use the DM.** The DM is a channel with `is_dm` true and exactly two members, the two users
    (shown by `GET /api/channels/{id}/members`). Both can post to it and list its messages
    through the usual message routes, on any node.

11. **Private to the pair.** Any other user gets `403` listing or posting messages in the DM.

12. **DM errors.** An unknown `user_id` returns `404`. A body without `user_id` returns `400`.
    No token returns `401`.

13. **Live.** A DM channel works with `subscribe` like any other channel: a message posted to it
    reaches the other user's socket as `message.created` with the next `event_id`.

## Notes

- A DM is not listed among a workspace's channels and is not created in any workspace's `#general`.
- Store the DM pair in shared storage (for example with a unique key on the two sorted user ids),
  so two nodes never create two DMs for the same pair.
