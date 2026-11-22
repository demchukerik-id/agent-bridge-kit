"""Structure a Claude-for-Chrome panel reply before it is pasted into a pane.

The panel's scraped text arrives as: the header "Used Claude in Chrome (N
actions)" (often twice, sometimes again mid-reply with "· N notes"), the
answer, then one short line per action ("Opened example.com", "Ran page
script", "Captured screenshot" ...). This turns that into

    Answer
    <answer text>

    Actions (3)
    1. Opened example.com
    2. Captured screenshot x12
    3. Ran page script

Anything that does not look like a browser-agent reply (the "connected" and
"closed" notices, a panel that printed no header) is returned unchanged.
"""
import re

_HEADER = re.compile(r"^Used Claude in Chrome\b.*$", re.I)
_ACTION = re.compile(
    r"^(Opened|Opening|Ran|Read|Captured|Zoomed|Pressed|Waited|Found|Checked|"
    r"Loaded|Searched|Scrolled|Uploaded|Clicked|Typed|Navigated|Took|Scanned|"
    r"Extracted|Filled|Selected|Switched|Closed|Dragged|Hovered|Downloaded|"
    r"Created|Resized|Saved|Focused|Expanded|Collapsed)\b")
_MAX_ACTION_LEN = 90


def structure_reply(text: str) -> tuple[str, bool]:
    """Return (text, structured). structured is False when text is untouched."""
    # The scrape puts an icon glyph (private-use characters such as U+E082,
    # U+E069) on its own line before each action, and str.strip() keeps them,
    # so a "blank" glyph line broke the trailing action run. Zero-width
    # characters are dropped for the same reason.
    text = re.sub(r"[​-‏⁠﻿-]", "", text)
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    if not any(_HEADER.match(ln.strip()) for ln in lines):
        return text.strip(), False

    kept = [ln for ln in lines if not _HEADER.match(ln.strip())]

    # Action log = the trailing run of short lines that start with an action
    # verb, blank lines between them ignored.
    actions: list[str] = []
    end = len(kept)
    while end > 0:
        ln = kept[end - 1].strip()
        if not ln:
            end -= 1
            continue
        if len(ln) <= _MAX_ACTION_LEN and _ACTION.match(ln):
            actions.append(ln)
            end -= 1
            continue
        break
    actions.reverse()
    answer_lines = kept[:end]

    while answer_lines and not answer_lines[0].strip():
        answer_lines.pop(0)
    answer = "\n".join(answer_lines).strip()
    answer = re.sub(r"\n{3,}", "\n\n", answer)

    # Fold identical consecutive actions into "line xN".
    folded: list[tuple[str, int]] = []
    for a in actions:
        if folded and folded[-1][0] == a:
            folded[-1] = (a, folded[-1][1] + 1)
        else:
            folded.append((a, 1))

    parts = ["Answer", answer or "(the agent returned no text)"]
    if folded:
        parts.append("")
        parts.append(f"Actions ({len(actions)})")
        for i, (a, n) in enumerate(folded, 1):
            parts.append(f"{i}. {a}" + (f" x{n}" if n > 1 else ""))
    return "\n".join(parts), True
