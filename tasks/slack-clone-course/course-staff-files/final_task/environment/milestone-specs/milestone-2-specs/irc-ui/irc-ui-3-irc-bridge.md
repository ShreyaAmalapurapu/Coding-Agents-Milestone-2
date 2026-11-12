# IrcUi.3: IRC channels and bridging with the web

Covers: IRC channel names, `JOIN`, two-way bridging between IRC and web messages in one dense
`event_id` stream, and bridging that keeps working while an HTTP node is dead.
Check: `bash /spec/irc-ui/checks/check_irc_ui_3.sh`. The rule numbers below match its output.
The check kills the HTTP node on port `8001` (by the PID that owns the port), so run it as a
command of its own.

Do not start this spec until `check_irc_ui_2.sh` prints `RESULT: 8 passed, 0 failed`.

The conventions in `irc-ui-1-edits.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| `post_message` | An IRC `PRIVMSG` is posted through the **same** function as the web route, so it gets the next `event_id` and goes to web sockets |
| `broadcast` (and the fallback fan-out, if Cluster is in `/spec`) | How the gateway learns about web messages to send to IRC |
| `get_workspace`, `get_member` | Mapping `#slug/channel` to a channel, and membership |

## Adds

| Function | What it does |
|---|---|
| `irc_channel(name)` | `#{workspace-slug}/{channel-name}` → the channel, or `None` |
| `irc_deliver(event)` | Sends a web `message.created` to every IRC client that joined that channel as `:<nick>!<user>@<host> PRIVMSG #slug/name :<body>`, except the client the message came from |

## Rules (18-25)

A web channel maps to the IRC name `#{workspace-slug}/{channel-name}`. Workspace `acme`, channel
`general` → `#acme/general`. The separator is a literal `/`.

18. **JOIN.** `JOIN #slug/general` by a member of the workspace sends the joiner's own
    `:<nick>!<user>@<host> JOIN #slug/general` line, then `353` (`RPL_NAMREPLY`, the nicks in the
    channel), then `366` (`RPL_ENDOFNAMES`). `332` (topic) or `331` (no topic) may come between
    the `JOIN` line and `353`. Joining over IRC also makes the user a channel member.

19. **Unknown channels.** `JOIN #slug-general` (a hyphen instead of `/`) and
    `JOIN #slug/<channel that does not exist>` both get `403` (`ERR_NOSUCHCHANNEL`).

20. **Web to IRC.** A message posted on the web reaches every IRC client in that channel within 3
    seconds as exactly `:<author username>!<author username>@<host> PRIVMSG #slug/general :<body>`
    (the author's web username is the nick in the prefix).

21. **IRC to web.** `PRIVMSG #slug/general :<text>` from an IRC client is stored as a message
    whose author is the IRC user and whose body is `<text>`. It is listed by
    `GET /api/channels/{id}/messages` on every node.

22. **One stream.** That message reaches web sockets as `message.created` with the channel's next
    `event_id`, exactly like a web post.

23. **IRC to IRC.** Other IRC clients in the channel receive it as
    `:<sender nick>!<user>@<host> PRIVMSG #slug/general :<text>`. The sender does not get it back.

24. **Mixed writers stay dense.** Web posts and IRC `PRIVMSG`s alternating in one channel give
    consecutive `event_id`s, no gaps or repeats.

25. **Survives a dead node.** The gateway is not inside any HTTP node's process. After the node
    on `8001` is killed, a web post on `8000` still reaches IRC clients, and an IRC `PRIVMSG`
    is still stored and listed on `8002`.

## Notes

- The gateway reads and writes the same shared database as the HTTP nodes, and subscribes to
  the same event fan-out, so it does not depend on any one HTTP node being alive.
- Long lines: send each message body as one `PRIVMSG` line, replacing any newlines with spaces.
