# Features.2: reactions

Routes covered: `POST /api/messages/{id}/reactions`, `DELETE /api/messages/{id}/reactions/{emoji}`,
and the `reactions` field of every message.
Check: `bash /spec/features/checks/check_features_2.sh`. The rule numbers below match its output.

Do not start this spec until `check_features_1.sh` prints `RESULT: 13 passed, 0 failed`.

The conventions in `features-1-profiles-dms.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| `user_from_token`, `get_member` | Who is reacting, and whether they may see the channel |
| `list_messages` | Every message it returns now carries its current `reactions` |
| `append_event`, `broadcast` | `reaction.added` and `reaction.removed` frames |

## Adds

| Function | What it does |
|---|---|
| `add_reaction(message, user, emoji)` | Stores one (message, user, emoji) row if it is not there yet; emits `reaction.added` only when a row was added |
| `remove_reaction(message, user, emoji)` | Deletes the caller's row if it exists; emits `reaction.removed` only when a row was deleted |
| `reactions_of(message)` | `[{emoji, count, user_ids}]`, one entry per emoji that has at least one user, in the order each emoji was first added |

## Rules (14-23)

`reactions` is `[{emoji, count, user_ids}]`: `count` is the number of users, `user_ids` their
ids. `emoji` is any non-empty string, for example `👍` or `eyes`. In the `DELETE` URL it is
percent-encoded (`👍` is `%F0%9F%91%8D`); decode it before comparing.

14. **React.** `POST /api/messages/{id}/reactions` with `{emoji}` returns `200` (not `201`) and
    `{reactions}`: the message's full reaction list, here
    `[{"emoji": "👍", "count": 1, "user_ids": [<caller id>]}]`.

15. **Idempotent.** The same user posting the same emoji again returns `200` and the count stays `1`.

16. **Two users.** A second user (on another node) reacting with the same emoji gives `count`
    `2` and both ids in `user_ids`.

17. **In listings.** `GET /api/channels/{id}/messages` (any node) shows the message with the same
    `reactions` list.

18. **Remove.** `DELETE /api/messages/{id}/reactions/{emoji}` removes only the caller's reaction
    and returns `200` and `{reactions}`. Removing a reaction the caller does not have is a no-op
    that still returns `200` and the list.

19. **Empty entries go.** When the last user removes an emoji, that entry leaves the list. A
    message with no reactions has `reactions: []`.

20. **Several emojis.** Different emojis on one message are separate entries, each with its own count.

21. **Errors.** An unknown message id returns `404` on both routes. A body without `emoji`, or
    with an empty `emoji`, returns `400`. No token returns `401`.

22. **Events.** A new reaction emits a durable `reaction.added` frame and a removal emits
    `reaction.removed`, each with the next `event_id` of the message's channel (frames carry
    `channel_id`, `event_id`, `message_id`, `emoji`, `user_id`). A duplicate add or a no-op
    remove emits **nothing**, so the sequence stays dense.

23. **Private channels.** Reacting to a message in a private channel the caller is not a member
    of returns `403`.
