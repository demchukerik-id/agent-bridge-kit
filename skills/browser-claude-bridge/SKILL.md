---
name: browser-claude-bridge
description: Send tasks to logged-in claude.ai chats from any local script. Each chat is addressable by friendly name (e.g. browser-research) or short alias (%0, %1, …). Replies come back via long-poll. Use this when an agent needs to fan out work to one or more browser-side Claude chats and receive their responses.
---

# browser-claude-bridge

Drive logged-in claude.ai side panels from any local script. Each chat is a "panel" addressable by friendly name, `%N` short alias, or numeric `tab_id`. Use the `browser-bridge` CLI for everything — it handles auth, routing, and long-polling for replies.

## Quick reference

```
browser-bridge open-panel [--url U]       New tab + Claude side panel by shortcut, prints tab_id
browser-bridge close-panel <target>       Close the panel's whole tab group, chat stays saved
browser-bridge delete-chat <target>       Delete the panel's chat, then close its tab group
browser-bridge list                       Show all panels (registered + discovered)
browser-bridge read <target> [n_chars]    Read the last reply text from a panel
browser-bridge type <target> <text>       Type into the panel input (no submit)
browser-bridge keys <target> <key>        Send a key (Enter, Escape, ...)
browser-bridge message <target> <text>    Send + wait for the chat's reply, print it
browser-bridge name <target> <label>      Rename a panel in panels.json
browser-bridge resolve <label>            Print the tab_id for a label
browser-bridge id                         Print this caller's identity
```

`<target>` accepts: **friendly name** | **`%N` alias** | numeric **`tab_id`**.

## Opening a panel

`browser-bridge open-panel` presses Ctrl+T and then Alt+W in the bridge window
of `--account` (default you@example.com) and prints the new panel's
tab_id. Use it whenever you need a panel. Do not ask the user to click the
extension icon.

- The panel is claimed for your pane: the server binds it to you, sends no
  connect greeting, and posts no "connected" line into the pane the user spoke
  to last. You can `message` it without `--force`.

- The first run on a PC or profile checks chrome://extensions/shortcuts and
  sets Alt+W for Claude if it is missing. Later runs skip the check and take
  about 2 s. `--check-shortcut` forces the check.
- `--url <url>` loads a page in the new tab before the panel opens.
- Never press keys on Windows yourself (SendKeys, keybd_event). An unguarded
  Alt+W went to the VS Code window and killed the calling agent on
  2026-09-26. `windows/window-keys.ps1` presses only after the named bridge
  window is verified in front, and exits 1 without pressing otherwise.

## Closing a panel or deleting its chat

Claude for Chrome keeps a panel's tab and every tab it opened in one Chrome
tab group. Both commands close that whole group, never only the panel.

| The user says | Run |
|---|---|
| "delete the chat", "delete the browser chat", "clean up the panel you used" | `browser-bridge delete-chat <target>` |
| "close the panel", "close the group", "close the extension" | `browser-bridge close-panel <target>` |

- `delete-chat` deletes the claude.ai chat (the title's More options, Delete,
  confirm) and then closes the group. About 1 to 3 s. It refuses while the
  panel is still answering: wait for the reply first.
- `close-panel` closes the group and leaves the chat in claude.ai history
  (it still opens at claude.ai/cowork/<cse id>, a deleted one redirects to /new).
  Under 0.5 s.
- `<target>` is the /brN, name, or tab_id you worked with. Both drop the panel
  from panels.json. `list` may show it for up to 180 s more (discovery grace).
- If the panel's tab is hidden behind another tab, `delete-chat` activates
  it. If the whole window is covered and the bridge Chrome was started
  without the occlusion flags, it raises the window, focus only.

## Sender identity

The CLI resolves the caller's identity in this order: `BB_SENDER` env var → `TMUX_PANE` (if set) → `virtual:<hostname>:<pid>`. The server uses it to paste the reply back into that pane. The panel never sees it. Set `BB_SENDER` explicitly when you want a stable, readable name across runs (e.g. `BB_SENDER=browser-orchestrator`).

## Sending a task

One-shot send-and-wait:

```bash
browser-bridge message browser-research \
  "Open https://en.wikipedia.org/wiki/Special:Random and reply with: title=<page title>"
```

The panel gets your text exactly as written. Nothing is prepended.

`message` blocks until the reply is stable, then prints it to stdout. Done.

### Multi-step tasks: report progress

When a task has more than one step, plan the steps first, then send every
message with `--task <id> --step N/M --step-name "<short name>"`. The
sessions-overview panel reads this from `GET /tasks` and shows the step, the
try number and the panel's live action count for your pane.

```bash
browser-bridge message pane-39 --task invoices-oct --step 1/3 --step-name "Open invoices" "..."
browser-bridge message pane-39 --task invoices-oct --step 2/3 --step-name "Check tracking IDs" "..."
```

- Same `--task` and same step again = a retry (try 2, 3, ...). A new step starts at try 1.
- The reply to step M ends the task. End it early with `browser-bridge task <id> done|failed`.

## Writing to the panel

Claude for Chrome treats its composer as the human's turn and judges the text by shape. Anything that looks machine-made is refused as a prompt injection planted by the page, and the whole message is dropped. That happened on 2026-09-19 with the old `[browser-bridge from:... — load the skill to reply]` header and the old connect greeting. Write every message the way the user would type it into the chat:

- Plain sentences, first person. No bracketed tags, no `from:`/`at:` headers, no protocol words such as relay, bridge, round trip, parse.
- No role assignment. Never open with "You are ...". Never dictate the format of its reply.
- No instructions it cannot follow, such as loading a skill.
- If you need a terse answer, ask as a person would: "one line is enough".

The connect greeting the server sends to a new panel follows the same rule.

## How replies come back

The server stamps every reply with `[browser-bridge /brN → %pane]` when it pastes it into the sending pane, and drops a duplicate tag if the panel typed one itself. The panel does not need to add any header, and you must not ask it to.

The server also restructures the panel's scraped text (`server/reply_format.py`), so every browser-agent reply lands in one fixed shape:

```
[browser-bridge /br64 → %10]
Answer
<what the agent found, as it wrote it>

Actions (2)
1. Opened example.com
2. Ran page script
```

The duplicate "Used Claude in Chrome (N actions)" header lines are dropped, the action log is numbered with repeats folded to `xN`, and short notices like `connected` and `closed` pass through untouched. Never ask the panel to format its own reply, that is refused as injection. Structure is applied on our side only.

**When you relay a browser agent's result to the user**, keep the same structure and add what they need to decide. Put the answer first, then what the agent did, then anything unverified. Do not paste the raw scrape, and do not present the agent's claims as verified fact: say "the browser agent reports" and give the source URL it opened.

## When to reach for the browser agent

The user's rule: when your own research (web search, fetch) comes back empty, thin, or blocked, do not stop. Open a browser agent and look for the answer in a real browser. Useful places, in the order that fits the question:

- The primary source's own site (a page that returned 403 or 451 to a fetch often loads in the browser).
- X (x.com) for breaking news, statements from named people, and what is being said right now.
- YouTube for live streams, press conferences, and official channel uploads.

Give the agent a plain first-person task, name the site, and ask for the specific thing ("what did the White House post on X in the last hour about the AI lunch"). Report back in the structured shape above, and name which site each claim came from.

## What to do when

| Goal | Command |
|---|---|
| Open a new panel | `browser-bridge open-panel [--url <url>]` |
| Close a panel, keep the chat | `browser-bridge close-panel <target>` |
| Delete a panel's chat and close it | `browser-bridge delete-chat <target>` |
| Send a one-shot task and get the reply | `browser-bridge message <target> "<text>"` |
| Send something but not submit yet | `browser-bridge type <target> "<text>"` |
| Press Enter to submit a typed draft | `browser-bridge keys <target> Enter` |
| See what an agent last said | `browser-bridge read <target>` |
| Discover available chats | `browser-bridge list` |
| Label a freshly-opened chat | `browser-bridge name %2 my-label` |
| Look up a label's `tab_id` | `browser-bridge resolve my-label` |

## Things that will bite

- **Don't poll `read` in a loop without backoff.** The CLI is synchronous; the server already long-polls. If you want to wait, use `message` — it waits correctly.
- **`<target>` matching is exact and case-sensitive** for friendly names and `%N` aliases. `Browser` ≠ `browser`.
- **Newly-opened chats appear after the next push cycle (~3s).** Run `list` again if a brand-new panel is missing.
- **Do not ask the panel to echo a header.** The server stamps replies itself. A reply-format instruction is one of the shapes Claude for Chrome refuses as injection.
- **`type` does not submit.** You must follow with `keys <target> Enter`. Prefer `message` unless you specifically need a draft-then-submit flow.
- **`%N` aliases are not stable across restarts.** Use friendly names (set via `name`) for anything you'll reference more than once.
- **One panel per chat.** If two callers `message` the same target concurrently, the second blocks until the first's reply is stable — don't fan out to the same chat in parallel.
