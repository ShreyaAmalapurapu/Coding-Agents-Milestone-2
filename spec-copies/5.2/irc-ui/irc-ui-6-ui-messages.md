# IrcUi.6: message actions in the browser

Covers: the hover toolbar (edit, delete, thread, react), the thread panel, the emoji picker and
reaction chips, message rows, and the empty-channel state.
Check: `bash /spec/irc-ui/checks/check_irc_ui_6.sh`. The rule numbers below match its output.
It drives a headless Chromium at 1280x900; run it as a command of its own.

Do not start this spec until `check_irc_ui_5.sh` prints `RESULT: 7 passed, 0 failed`.

The conventions in `irc-ui-1-edits.md` apply to every rule.

## Uses

Call these functions from earlier specs. Do not write a second version of any of them.

| Function | Used here for |
|---|---|
| The page and its WebSocket from IrcUi.4 and IrcUi.5 | Every element below is inside a `message` row or the thread panel |
| `PATCH`/`DELETE /api/messages/{id}`, replies, reactions (see "The API the UI needs" in IrcUi.4) | What the toolbar calls |

## Adds

| Element (`data-testid`) | Where |
|---|---|
| `edit-message-btn`, `delete-message-btn` | In the hover toolbar of the user's own messages |
| `edit-message-input` | Replaces the body while editing; Enter saves, Escape cancels |
| `edited-indicator` | `(edited)` after the body of an edited message |
| `reply-count` | Under a parent message: `1 reply` / `N replies` (hidden when 0) |
| `message-avatar`, `message-author`, `message-time` | In every message row |

## Rules (38-45)

The check posts some messages through the API, sets `huddle.token`, opens `#general`, and then
uses only the mouse and keyboard. Each action is found **inside the message row**
(`data-testid="message"`) it applies to, and shows when that row is hovered.

38. **Edit.** Hovering your message and clicking `edit-message-btn` shows `edit-message-input`
    with the text. Typing a new text and pressing Enter saves it (`PATCH`). The row shows the new
    `message-body` and an `edited-indicator`, and both are still there after a hard reload.

39. **Delete.** Hovering your message and clicking `delete-message-btn` (a `confirm()` dialog is
    allowed) deletes it. The row disappears at once and stays gone after a reload.

40. **Thread.** Clicking `open-thread-btn` on a message opens `thread-panel` to the **right** of
    `message-list`, showing the parent and its replies. Typing in `thread-input` and clicking
    `thread-send` posts a reply (with `parent_id`) that appears in the panel. The parent's
    `reply-count` shows `1 reply`.

41. **Close.** `close-thread` hides `thread-panel`.

42. **React.** Clicking `reaction-button` opens `emoji-picker`, a visible grid of at least 6
    `emoji-option` buttons (never a `prompt()`). Clicking one saves the reaction and shows a
    `reaction-chip` under that message with the emoji and the count `1`.

43. **Un-react.** Clicking your own `reaction-chip` removes your reaction. The chip goes away when
    its count reaches 0.

44. **Message rows.** Every `message` shows a `message-avatar` (a coloured circle with an initial
    is enough), `message-author` (display name or username), `message-time` (a readable time
    such as `10:42 AM`), and `message-body`.

45. **Empty channel.** A channel with no messages shows a short sentence in `message-list` (for
    example `No messages yet. Say hello!`) and no `message` rows.

## Notes

- Apply WebSocket events to the open channel (`message.updated`, `message.deleted`,
  `message.reply`, `reaction.added`/`removed`) so other users' changes show live too.
- Keep the toolbar buttons in the DOM for each row and show them with CSS `:hover` or
  `mouseenter`. The check hovers the row before clicking.
