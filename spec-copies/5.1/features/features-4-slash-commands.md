# Features.4: slash commands

Routes covered: `POST /api/channels/{id}/messages` when `body` starts with `/`.
Check: `bash /spec/features/checks/check_features_4.sh`. The rule numbers below match its output.

Do not start this spec until `check_features_3.sh` prints `RESULT: 8 passed, 0 failed`.

The conventions in `features-1-profiles-dms.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| `post_message` | `/me`, `/shrug` and `//...` are normal messages |
| `update_channel_topic`, `set_archived`, `join_channel` | `/topic`, `/archive`, `/unarchive`, `/invite` call the same functions as the REST routes, with the same permission checks and the same events. If Channels is not in `/spec`, write them now: a topic is up to 250 characters and may be set by a channel member or the workspace owner; only the workspace owner may archive or unarchive (others get `403`); posting to an archived channel returns `423` |

## Adds

| Function | What it does |
|---|---|
| `run_slash_command(channel, user, body)` | Called by the message route before anything is stored when `body` starts with `/` (but not `//`). Returns the response for the command |

## Rules (32-42)

A command is the first word of `body` (up to the first space). The rest, with surrounding spaces
trimmed, is its text.

32. **`/me`.** `/me waves` posts a normal message (`201`, `{message}`) whose `body` is kept
    exactly as `"/me waves"`. It is not stripped or reformatted.

33. **`/me` needs text.** `/me` with no text (or only spaces) returns `400`.

34. **`/shrug`.** `/shrug oh well` posts a normal message whose `body` is
    `oh well ¯\_(ツ)_/¯` (one backslash; in JSON it is written `"oh well ¯\\_(ツ)_/¯"`).
    `/shrug` alone posts just `¯\_(ツ)_/¯`.

35. **`/topic`.** `/topic Sprint planning` sets the channel topic to `Sprint planning` and returns
    `201` with exactly `{"message": null, "channel": <ChannelObj>}`, where the channel has the new
    topic. A topic over 250 characters returns `400`.

36. **`/invite`.** `/invite @username` adds that workspace member to the channel and returns
    `201` with `{"message": null, "invited_user": <UserObj>}`. They are then listed in
    `GET /api/channels/{id}/members`.

37. **`/invite` errors.** A username that does not exist returns `404`.

38. **`/archive` permission.** Anyone but the workspace owner using `/archive` gets `403` (the same rule as
    `POST /api/channels/{id}/archive`).

39. **`/archive`, `/unarchive`.** The workspace owner's `/archive` returns `201` with
    `{"message": null, "channel": <ChannelObj with is_archived true>}`. Normal posts then return
    `423`, but `/unarchive` still works and returns `201` with `is_archived` false. Posting then
    works again.

40. **Unknown commands.** Any other command (for example `/foo bar`, or a lone `/`) returns `400`.

41. **Escaped slash.** A body starting with `//` is not a command. It is posted as a normal
    message with the body unchanged (`//literal text` stays `//literal text`).

42. **No message for side effects.** `/topic`, `/invite`, `/archive`, `/unarchive` store no
    message and emit no `message.created`. Each emits only its usual event (for `/topic`,
    `channel.updated` with the next `event_id`). The command text never appears in the listing.
