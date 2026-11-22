// content.js — runs on https://claude.ai/*. Exposes window.__claudeBridge.send(item)
// so the service worker can drive a conversation: type the prompt, click send,
// wait for the streamed reply, and post it back via chrome.runtime.sendMessage.
//
// Selectors are clustered at the top because claude.ai's DOM mutates often.
// If something breaks, patch SELECTORS first.

(() => {
  if (window.__claudeBridge) return; // idempotent re-injection

  const SELECTORS = {
    // The chat input is a ProseMirror contenteditable.
    editor: [
      '[data-testid="chat-input"]',                   // 2026-09-19 Claude for Chrome
      'div.ProseMirror[contenteditable="true"]',
      'div[contenteditable="true"][role="textbox"]',
      'div[contenteditable="true"]',
    ],
    // The send/submit button.
    send: [
      'button[data-testid="chat-input-send"]',        // 2026-09-19 Claude for Chrome
      'button[aria-label="Send message" i]',
      'button[aria-label*="Send" i]',
      'button[data-testid="send-button"]',
      'fieldset button[type="submit"]',
    ],
    // The stop-streaming button (visible while Claude is replying).
    stop: [
      'button[aria-label="Stop response" i]',
      'button[aria-label*="Stop" i]',
      'button[data-testid="stop-button"]',
    ],
    // Containers for individual assistant turns. Multiple fallbacks because
    // claude.ai renames classes/testids often.
    assistantTurn: [
      '[data-testid="assistant-message"]',            // 2026-09-19 Claude for Chrome
      '[data-testid="assistant-turn"]',
      'div[data-message-author-role="assistant"]',
      '[data-test-render-count]',                // any rendered turn
      'div.font-claude-message',                  // current claude.ai class (subject to change)
      'div.font-claude-response',
      'article[data-author="assistant"]',
      'div[class*="message-content"]',
    ],
    // Marker that streaming has finished — when present, reply is complete.
    finishedMarker: [
      'button[data-testid="action-bar-copy"]',    // copy button appears after streaming
      'button[aria-label*="Copy" i]',
      'div[data-test-render-count][data-is-streaming="false"]',
    ],
  };

  const log = (...args) => console.log("[claude-bridge]", ...args);
  const warn = (...args) => console.warn("[claude-bridge]", ...args);

  function $first(selectors) {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function $all(selectors) {
    const out = [];
    for (const sel of selectors) {
      out.push(...document.querySelectorAll(sel));
    }
    return out;
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function waitFor(predicate, { timeoutMs = 15000, intervalMs = 100 } = {}) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const v = predicate();
      if (v) return v;
      await sleep(intervalMs);
    }
    return null;
  }

  function focusEditor(editor) {
    editor.focus();
    // Move caret to end
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(editor);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function pasteIntoEditor(editor, text) {
    focusEditor(editor);
    // Strategy 1: synthetic paste event with clipboardData. ProseMirror handles paste cleanly.
    const dt = new DataTransfer();
    dt.setData("text/plain", text);
    const evt = new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: dt,
    });
    const handled = editor.dispatchEvent(evt);
    if (handled && editor.textContent.includes(text.slice(0, Math.min(8, text.length)))) {
      return true;
    }
    // Strategy 2: execCommand insertText (legacy but still works in contenteditables).
    const ok = document.execCommand && document.execCommand("insertText", false, text);
    if (ok) return true;
    // Strategy 3: manual textNode insertion + input event.
    const tn = document.createTextNode(text);
    editor.appendChild(tn);
    editor.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    return true;
  }

  function clickSend() {
    const btn = $first(SELECTORS.send);
    if (!btn) return false;
    if (btn.disabled || btn.getAttribute("aria-disabled") === "true") return false;
    btn.click();
    return true;
  }

  function listAssistantTurns() {
    // Try selectors in priority order, take the first that returns matches.
    for (const sel of SELECTORS.assistantTurn) {
      const els = Array.from(document.querySelectorAll(sel));
      if (els.length) return { selector: sel, els };
    }
    return { selector: null, els: [] };
  }

  function extractText(turnEl) {
    // Prefer markdown content if present
    const md = turnEl.querySelector('[class*="prose"]') || turnEl;
    return (md.innerText || md.textContent || "").trim();
  }

  async function waitForReply({ baseline, perTurnTimeoutMs = 180_000 }) {
    // 1. Wait for a new assistant turn to appear OR for the existing last turn to grow significantly.
    const newTurn = await waitFor(() => {
      const { els } = listAssistantTurns();
      if (els.length > baseline.count) return els[els.length - 1];
      const last = els[els.length - 1];
      if (last && extractText(last).length > baseline.lastTextLen + 8) return last;
      return null;
    }, { timeoutMs: perTurnTimeoutMs, intervalMs: 250 });

    if (!newTurn) {
      const { selector } = listAssistantTurns();
      return { ok: false,
        error: `no new assistant turn appeared (matched selector: ${selector || "none"}). DOM may have changed; patch SELECTORS.assistantTurn.` };
    }

    // 2. Wait for streaming to finish using multiple signals.
    await waitFor(() => {
      // Signal A: stop button disappears
      if ($first(SELECTORS.stop)) return false;
      // Signal B: a copy/finished marker appeared inside the turn
      for (const sel of SELECTORS.finishedMarker) {
        if (newTurn.querySelector(sel)) return true;
      }
      // Signal C: send button re-enables (no stop visible)
      const send = $first(SELECTORS.send);
      if (send && !send.disabled && send.getAttribute("aria-disabled") !== "true") return true;
      return false;
    }, { timeoutMs: perTurnTimeoutMs, intervalMs: 300 });

    // 3. Settle delay so the final tokens land before we capture.
    await sleep(500);
    return { ok: true, text: extractText(newTurn) };
  }

  async function send(item) {
    try {
      const editor = $first(SELECTORS.editor);
      if (!editor) return { ok: false, error: "chat editor not found" };

      const { els: baselineTurns } = listAssistantTurns();
      const baseline = {
        count: baselineTurns.length,
        lastTextLen: baselineTurns.length ? extractText(baselineTurns[baselineTurns.length - 1]).length : 0,
      };

      // Send the raw orchestrator text to claude.ai. The routing header is
      // applied ONLY on the reply path (watcher → tmux pane), not on the
      // inbound message — claude.ai treats system-styled prefixes like
      // "[browser-bridge from:...]" as a potential prompt injection and
      // refuses to act on the content. Matches tmux-bridge: type raw text
      // into the target pane and let the receiver tag who-said-what from
      // their own context.
      pasteIntoEditor(editor, item.text);
      await sleep(150);

      // Wait for send button to enable, then click it.
      const ready = await waitFor(() => {
        const btn = $first(SELECTORS.send);
        return btn && !btn.disabled && btn.getAttribute("aria-disabled") !== "true";
      }, { timeoutMs: 5000, intervalMs: 100 });
      if (!ready) return { ok: false, error: "send button never enabled" };

      if (!clickSend()) return { ok: false, error: "send click failed" };

      // Detach reply-watcher: returns immediately so the service-worker call doesn't block.
      (async () => {
        const result = await waitForReply({ baseline });
        const text = result.ok ? result.text : `[bridge error] ${result.error}`;
        try {
          await chrome.runtime.sendMessage({
            type: "reply",
            id: item.id,
            from_pane: item.from_pane,
            text,
          });
        } catch (e) {
          warn("sendMessage failed:", e);
        }
      })();

      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e && e.message || e) };
    }
  }

  window.__claudeBridge = { send, SELECTORS, log };
  log("ready on", location.href);
})();
