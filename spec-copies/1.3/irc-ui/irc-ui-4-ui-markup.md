# IrcUi.4: the web UI page and its pinned hooks

Covers: the single-page UI served at `GET /` and the selectors, ids and button roles the graders
use to find things in it.
Check: `bash /spec/irc-ui/checks/check_irc_ui_4.sh`. The rule numbers below match its output.

Do not start this spec until `check_irc_ui_3.sh` prints `RESULT: 8 passed, 0 failed`.

The conventions in `irc-ui-1-edits.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| The REST routes and the WebSocket | The page only calls the same public API the checks use, with `Authorization: Bearer <token>` |

### The API the UI needs

The UI (here and in IrcUi.5 to IrcUi.7) uses the routes below. Where Specs Roles, Channels or
Features are in `/spec`, they come from there. Where they are not, build each missing route now,
before the page, exactly as `instruction.md` describes it (status codes, envelopes, events):

| Area | Routes |
|---|---|
| Workspaces | `GET /api/workspaces` (`{workspaces}`), `GET /api/workspaces/{slug}` (`{workspace, channels, read_state}`), `PATCH /api/workspaces/{slug}` (`name`, `join_mode`; admin or owner) |
| Members and roles | `GET /api/workspaces/{slug}/members` (`{members: [{user_id, role, ...}]}`), `PATCH /api/workspaces/{slug}/members/{user_id}` (`{role}`) |
| Invitations | `POST /api/workspaces/{slug}/invitations` (`201 {invitation: {code, ...}}`, admin or owner), `GET` on the same route, `POST /api/invitations/{code}/accept` (unknown or used code `404`) |
| Channels | `POST /api/workspaces/{slug}/channels` (duplicate name `409`), `PATCH /api/channels/{id}` (`{topic}`), `POST /api/channels/{id}/archive` and `/unarchive` (owner), `GET /api/channels/{id}/members`, `POST /api/dms` |
| Adding a member | the channel settings "add member" form posts `/invite @<username>` as a message to the channel (`201`, `{message: null, invited_user}`) |
| Threads | `POST /api/channels/{id}/messages` with `parent_id` (the parent's `reply_count` goes up), `GET /api/messages/{id}/replies` (oldest first), the `message.reply` event |
| Reactions | `POST /api/messages/{id}/reactions` (`{emoji}` → `200 {reactions}`), `DELETE /api/messages/{id}/reactions/{emoji}`, the `reaction.added` and `reaction.removed` events |

## Adds

| Function | What it does |
|---|---|
| `GET /` on every node | Returns one HTML page (for example `/app/static/index.html`) |
| `/static/app.js`, `/static/app.css` (names up to you) | The SPA's script and styles, served locally by every node. No CDN, no build step, no framework download |

## Rules (26-30)

26. **Served everywhere.** `GET /` on `8000`, `8001` and `8002` returns `200` with
    `Content-Type: text/html` and the same page.

27. **Pinned test ids.** Each of these is the `data-testid` of an element **in the HTML as
    served** (it may be hidden until needed): `auth-form`, `auth-submit`, `auth-toggle`,
    `workspace-create-form`, `empty-state-create-workspace`, `join-workspace-form`,
    `join-workspace-submit`, `channel-list`, `dms-list`, `new-channel-btn`, `current-user`,
    `logout-btn`, `workspace-header`, `workspace-settings-btn`, `workspace-settings-close`,
    `workspace-name-input`, `workspace-join-mode`, `workspace-general-submit`, `channel-title`,
    `channel-topic`, `channel-settings-btn`, `channel-settings-title`, `channel-settings-close`,
    `channel-members-list`, `channel-add-member-form`, `channel-add-member-input`,
    `channel-add-member-submit`, `channel-topic-input`, `channel-topic-submit`,
    `archive-channel-btn`, `unarchive-channel-btn`, `create-channel-form`,
    `create-channel-private`, `create-channel-submit`, `create-channel-cancel`, `message-list`,
    `message-input`, `send-btn`, `thread-panel`, `thread-input`, `thread-send`, `close-thread`,
    `settings-tab-members`, `settings-tab-invitations`, `settings-tab-general`,
    `settings-pane-members`, `settings-pane-invitations`, `settings-pane-general`,
    `create-invitation-btn`, `create-invitation-form`, `create-invitation-submit`,
    `invitations-list`.
    These are created by your script when their data renders: `join-workspace-error`,
    `channel-entry`, `channel-member-row`, `message`, `message-body`, `open-thread-btn`,
    `reaction-button`, `reaction-chip`, `emoji-picker`, `emoji-option`, `member-row`,
    `role-badge`, `role-select`, `invitation-code`.

28. **Pinned DOM ids.** The served HTML also has elements with the ids `auth-modal`,
    `create-channel-modal`, `channel-settings-modal`, `workspace-settings-modal`, and
    `create-invitation-form`. The `create-invitation-form` id goes on the same element as the
    `create-invitation-form` test id.

29. **Button roles.** Each button below has `data-button-role` on the same element as its
    `data-testid`, in the served HTML:
    - `primary`: `auth-submit`, `send-btn`, `thread-send`, `create-channel-submit`,
      `join-workspace-submit`, `workspace-general-submit`, `create-invitation-submit`,
      `channel-topic-submit`, `channel-add-member-submit`;
    - `danger`: `archive-channel-btn`, `logout-btn`;
    - `secondary`: `new-channel-btn`, `create-invitation-btn`, `unarchive-channel-btn`,
      `create-channel-cancel`, `close-thread`, `channel-settings-close`,
      `workspace-settings-close`, `channel-settings-btn`, `workspace-settings-btn`.

30. **Local assets only.** Every `<script src>` and stylesheet the page loads is a local path
    that returns `200` on every node. Nothing is loaded from the internet.

## Notes

- Write the page by hand as plain HTML, CSS and JavaScript (no React or bundler). Put every pinned
  element in `index.html` from the start and show or hide it with a class. Do not build it only
  from JavaScript.
- Channel sidebar items are `data-testid="channel-entry"` elements carrying
  `data-channel-id="<id>"` and `data-channel-name="<name>"` on the clickable element.
