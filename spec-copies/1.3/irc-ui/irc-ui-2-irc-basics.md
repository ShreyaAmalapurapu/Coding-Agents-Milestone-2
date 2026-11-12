# IrcUi.2: the IRC gateway, registration and commands

Covers: the IRC server on port `6667`: PASS login with a bearer token, NICK/USER registration,
PING, QUIT, and the accepted and unknown commands.
Check: `bash /spec/irc-ui/checks/check_irc_ui_2.sh`. The rule numbers below match its output.

Do not start this spec until `check_irc_ui_1.sh` prints `RESULT: 9 passed, 0 failed`.

The conventions in `irc-ui-1-edits.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| `user_from_token` | `PASS <token>` is checked against the same bearer tokens as the web |

## Adds

| Function | What it does |
|---|---|
| IRC gateway process | Listens on `0.0.0.0:6667`. **Its own process**, started and restarted by `/app/start.sh` like the HTTP nodes, using the same shared database |
| `irc_register(conn)` | Waits for `PASS`, `NICK` and `USER`, then sends the welcome numerics |
| `irc_dispatch(conn, line)` | One handler per command; anything else gets `421` |

## Rules (10-17)

Lines end with `\r\n`. Server lines start with `:<servername> <numeric> <nick> ...`.

10. **Registration.** A client that sends `PASS <valid bearer token>`, then `NICK <nick>`, then
    `USER <nick> 0 * :<real name>` receives the numerics `001`, `002`, `003`, `004`, `005`, then
    `422` (`ERR_NOMOTD`), in that order. The user is the token's owner.

11. **Bad token.** A `PASS` with an invalid token gets `464` (`ERR_PASSWDMISMATCH`) and never
    `001`.

12. **Nick in use.** A `NICK` already used by a connected client gets `433`
    (`ERR_NICKNAMEINUSE`). The connection stays open, and sending a free `NICK` then completes
    registration.

13. **PING.** `PING :<x>` is answered with `PONG` and the same `<x>`, for example
    `:<server> PONG <server> :<x>`.

14. **Unknown commands.** Any command not listed here gets `421` (`ERR_UNKNOWNCOMMAND`) and the
    connection stays open.

15. **Accepted commands.** `NAMES`, `WHO`, `LIST`, `TOPIC`, `MODE` and `PONG` are accepted: they
    never get `421`. A short, valid reply (or no reply, for `PONG`) is enough.

16. **QUIT.** `QUIT` (with or without a message) closes the connection, with no error numeric and
    no `ERROR` line.

17. **PASS is required.** A client that registers with `NICK` and `USER` but no `PASS` gets `464`
    and never `001`.

## Notes

- The nick is the user's choice. Use the web `username` as the IRC user for prefixes, so the
  prefix is `<nick>!<username>@<host>`.
- Read lines with a buffer and split on `\n` (strip a trailing `\r`); a client may send several
  commands in one packet.
