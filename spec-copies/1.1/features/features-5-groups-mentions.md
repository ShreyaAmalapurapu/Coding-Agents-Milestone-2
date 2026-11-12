# Features.5: user groups, @group-handle and @here

Routes covered: `GET`/`POST /api/workspaces/{slug}/groups`,
`PATCH`/`DELETE /api/workspaces/{slug}/groups/{handle}`, and the `mentions` of every new message.
Check: `bash /spec/features/checks/check_features_5.sh`. The rule numbers below match its output.

Do not start this spec until `check_features_4.sh` prints `RESULT: 11 passed, 0 failed`.

The conventions in `features-1-profiles-dms.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| `get_member` | Who may see and change groups (any workspace member; the workspace owner) |
| `resolve_mentions(channel, author, body)` | Extended here, not replaced. If Channels is not in `/spec`, write it now: `@username` (not mid-word, not email-shaped, the user must be in the channel's workspace) and `@channel` |
| `get_read_state` | `GET /api/channels/{id}/read` returns `{read_state: {channel_id, last_read_event_id, unread_count, mention_count}}`. If Channels is not in `/spec`, write this one route now |
| `ws_endpoint` | Records who has an open socket, for `@here` |

## Adds

| Function | What it does |
|---|---|
| `create_group(workspace, actor, handle, name, user_ids)` | Validates and stores a group; remembers its creator |
| `update_group(workspace, actor, handle, fields)` / `delete_group(workspace, actor, handle)` | Creator or workspace owner only |
| `online_user_ids()` | Users with at least one open WebSocket on **any** node (shared, for example a Redis or database set with a per-connection entry removed on close) |

## Rules (43-56)

`GroupObj = {id, handle, name, member_user_ids}`. Create and `PATCH` bodies use `user_ids`, and
responses use `member_user_ids` (never `members`). A handle is 2 to 32 characters of lowercase
letters, digits, `-` and `_`, unique within the workspace.

43. **Create.** Any workspace member may `POST /api/workspaces/{slug}/groups` with
    `{handle, name, user_ids}`. It returns `201` and `{group: GroupObj}`.

44. **List.** `GET /api/workspaces/{slug}/groups` returns `200` and `{groups}` (any node).

45. **Handle rules.** A handle already used in the workspace returns `409`. A handle with a space
    or an uppercase letter, an empty one, or a 1-character one returns `400`.

46. **Members only.** A user who is not in the workspace gets `403` on `POST` and `GET`. No token
    returns `401`.

47. **Update.** The group's creator may `PATCH .../groups/{handle}` with any of `{name, user_ids}`.
    It returns `200` and `{group}`, and `user_ids` replaces the member list.

48. **Who may update.** Other members get `403`. The workspace owner may update (`200`). An
    unknown handle returns `404`.

49. **Delete.** `DELETE .../groups/{handle}` by the creator or the workspace owner returns `200`
    and exactly `{"deleted": true}`. Deleting it again also returns `200` and `{"deleted": true}`.
    Other members get `403`. A deleted group is no longer listed.

50. **`@group-handle`.** `@<handle>` in a message expands to the group members who are also
    members of the message's channel, excluding the author. A mention token is `@` followed by
    letters, digits, `_` or `-`, not preceded by a letter, digit or `_`.

51. **Deduplicated.** `mentions` holds each user id once, whatever mix of `@user`, `@group`,
    `@channel` and `@here` named them, and never the author.

52. **Mention count.** Each user in `mentions` gets their `mention_count` raised by 1 for that
    channel. The author's own `mention_count` never rises from their own message.

53. **`@here`.** `@here` expands to the channel's members, except the author, who have an open
    WebSocket at that moment, on any node. Being subscribed is not required. Users with an open
    socket who are not channel members are not included.

54. **Closing counts.** Within 1 second of a user closing their last socket, `@here` no longer
    includes them.

55. **Unknown handles.** An `@word` that is neither a user nor a group is ordinary text: the
    message posts (`201`) with no mention for it.

56. **Deleted groups.** After a group is deleted, its handle no longer mentions anyone.

## Notes

- Mentions are fixed when the message is posted; later group or socket changes do not alter old messages.
