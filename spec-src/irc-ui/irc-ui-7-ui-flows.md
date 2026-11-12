# IrcUi.7: workspace, channel and account flows in the browser

Covers: the no-workspace empty state, creating and joining workspaces, creating channels
(including the duplicate-name error), channel and workspace settings, logout and login, and the
three-pane layout.
Check: `bash /spec/irc-ui/checks/check_irc_ui_7.sh`. The rule numbers below match its output.
It drives a headless Chromium at 1280x900; run it as a command of its own.

Do not start this spec until `check_irc_ui_6.sh` prints `RESULT: 8 passed, 0 failed`.

The conventions in `irc-ui-1-edits.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| The page from IrcUi.4 to IrcUi.6 | All the pinned elements below |
| Workspaces, members, invitations, channels (see "The API the UI needs" in IrcUi.4) | What these forms call |

## Adds

| Element | What it holds |
|---|---|
| `workspace-create-form` | Inputs `name="name"` and `name="slug"` and a `type="submit"` button |
| `join-workspace-form` | Input `name="code"` and `join-workspace-submit` |
| `create-channel-form` | Input `name="name"`, the `create-channel-private` checkbox, and a `role="alert"` element for errors |

## Rules (46-53)

The check signs up a brand-new user through `auth-form` and continues from there in one browser
session.

46. **Empty state, new workspace.** A user with no workspace sees `empty-state-create-workspace`,
    with `workspace-create-form` and `join-workspace-form` usable on it. Filling the name and
    slug and submitting creates the workspace: `workspace-header` shows its name and `#general`
    is in the sidebar.

47. **Bad invitation code.** Submitting `join-workspace-form` with a code that does not exist shows
    the error text in a visible `join-workspace-error`, and the user stays on the empty state.

48. **New channel.** `new-channel-btn` opens `#create-channel-modal`. Entering `planning` and
    clicking `create-channel-submit` creates it. It appears in the sidebar as a `channel-entry`
    with `data-channel-name="planning"`, and it opens (`channel-title` shows `planning`).

49. **Duplicate name.** Creating `planning` again shows the API's `409` as a visible
    `role="alert"` message inside `create-channel-form`, adds nothing to the sidebar, and keeps
    the modal open. `create-channel-cancel` closes it.

50. **Channel settings.** In an open channel, `channel-settings-btn` opens
    `#channel-settings-modal`. Typing in `channel-topic-input` and clicking `channel-topic-submit`
    saves the topic, which then shows in the header's `channel-topic`. `channel-settings-close`
    hides the modal.

51. **Workspace settings.** `workspace-settings-btn` opens `#workspace-settings-modal`.
    `settings-tab-members` shows `settings-pane-members` with a `member-row` per member, each
    with a `role-badge` (the owner's says `owner`). `settings-tab-invitations` shows
    `settings-pane-invitations`. There, `create-invitation-btn` then `create-invitation-submit`
    creates an invitation whose code is shown in an `invitation-code` element, and that code
    works with `POST /api/invitations/{code}/accept`. `workspace-settings-close` hides the modal.

52. **Logout and login.** `logout-btn` clears the token and shows `auth-form`. Switching to
    login with `auth-toggle` (the form's `data-mode` becomes `login`), then entering the same
    username and password, restores the same workspace and its channels.

53. **Layout.** At 1280x900 the page has three panes: a sidebar with a non-white background on
    the left (`workspace-header` at the top, then `channel-list` and `dms-list`), the message
    pane to its right (`channel-title` and `channel-topic` in a header, `message-list`, then the
    composer), and `thread-panel` on demand to the right.

## Notes

- Show API errors to the user as text in a visible `role="alert"` element next to the form;
  never silently ignore a failed request.
- `role-select` in each `member-row` (owner and admins only) changes a member's role through
  `PATCH /api/workspaces/{slug}/members/{user_id}`. `archive-channel-btn` and
  `unarchive-channel-btn` in channel settings call the archive routes. The graders may use these
  even though the check does not.
