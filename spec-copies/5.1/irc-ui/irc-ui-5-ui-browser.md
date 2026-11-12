# IrcUi.5: the web UI in a browser

Covers: button colours, sign-up and login, the channel sidebar, posting, and live updates in a
real browser.
Check: `bash /spec/irc-ui/checks/check_irc_ui_5.sh`. The rule numbers below match its output.
It drives a headless Chromium at 1280x900, so it takes a few seconds; run it as a command of its own.

Do not start this spec until `check_irc_ui_4.sh` prints `RESULT: 5 passed, 0 failed`.

The conventions in `irc-ui-1-edits.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| The page from IrcUi.4 | Every element below is one of its pinned test ids |
| `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/workspaces`, `GET /api/workspaces/{slug}`, the message routes, `PATCH`/`DELETE /api/messages/{id}`, reactions, the WebSocket | What the page calls |

## Adds

| Function | What it does |
|---|---|
| `render_*` functions in the page script | Draw the sidebar, the channel, messages and the thread panel from API data |
| a page-level WebSocket | `subscribe` to the open channel; apply `message.created`, `message.updated`, `message.deleted`, `reaction.added`/`removed` as they arrive |

## Rules (31-37)

31. **Exact button colours.** At rest (not hovered, focused or disabled), every button with
    `data-button-role` has computed `color` `rgb(255, 255, 255)` and `background-color`
    `rgb(0, 122, 90)` for `primary`, `rgb(224, 30, 90)` for `danger`, `rgb(18, 100, 163)` for
    `secondary`. Use plain solid colours (no gradient, image or transparency), for example
    `[data-button-role="primary"] { background-color: #007a5a; color: #fff; }`, and make sure no
    later rule overrides them.

32. **Sign-up error.** The page opens with `auth-form` in **sign-up** mode (`auth-toggle` switches
    to log in and back). It has inputs `name="username"` and `name="password"` (and optional
    `name="display_name"`). Submitting an invalid sign-up (for example a username with a space or
    a short password) shows the error in a visible `role="alert"` element inside `auth-form`, and
    stores no token.

33. **Sign-up logs in.** A valid sign-up logs the user in at once: the bearer token is saved in
    `localStorage` under `huddle.token`, and `current-user` shows the username. On load, a token
    already in `huddle.token` (or in a `?token=` query string) logs the user in without the form.

34. **Sidebar.** After a reload, the user's workspace is shown (`workspace-header` has its name)
    and `channel-list` holds one `channel-entry` per channel, each with `data-channel-id`,
    `data-channel-name` and a visible name prefixed with `#` (for example `#general`). Clicking
    an entry opens the channel: `channel-title` shows its name and `channel-topic` its topic.

35. **Post.** Typing in `message-input` and clicking `send-btn` (or pressing Enter) posts the
    message. It appears in `message-list` as a `message` element whose `message-body` holds the
    text, and it is still there after a hard reload.

36. **No blank posts.** Sending an empty or whitespace-only message does not call the API and
    shows nothing new.

37. **Live.** A message posted by someone else (on any node) appears in the open channel within
    3 seconds, without a reload.

## Notes

- The auth form carries `data-mode="register"` or `data-mode="login"` for its current mode, and
  `auth-toggle` flips it. After a logout it opens in `register` mode again.
- Message actions (edit, delete, threads, reactions) are IrcUi.6. Workspace, channel, settings
  and logout flows are IrcUi.7.
