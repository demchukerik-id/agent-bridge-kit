// Injected into the Claude side panel to capture the exact context size.
// Every turn the panel streams back an SSE frame whose usage.input_tokens is
// what the model actually read: system prompt, tool results, page reads, all
// of it. The hook clones the response, so the panel keeps its own stream.
// Canonical copy — daemon/ctx_agent.js reads this file and evaluates it.
(() => {
  if (!window.__bridgeCtx) {
    const state = { input_tokens: null, output_tokens: null, at: 0 };
    const take = (o) => {
      const u = (o && (o.usage || (o.message && o.message.usage))) || null;
      if (!u) return;
      const inp = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) +
                  (u.cache_creation_input_tokens || 0);
      if (inp > 0) { state.input_tokens = inp; state.at = Date.now(); }
      if (typeof u.output_tokens === "number") state.output_tokens = u.output_tokens;
    };
    const scan = (chunk) => {
      const lines = chunk.split("\n");
      for (let i = 0; i < lines.length; i++) {
        const s = lines[i].trim();
        if (s.indexOf("data:") !== 0) continue;
        const body = s.slice(5).trim();
        if (!body || body === "[DONE]") continue;
        try { take(JSON.parse(body)); } catch (e) {}
      }
    };
    const pump = async (res) => {
      try {
        const rd = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const r = await rd.read();
          if (r.done) break;
          buf += dec.decode(r.value, { stream: true });
          const i = buf.lastIndexOf("\n");
          if (i >= 0) { scan(buf.slice(0, i)); buf = buf.slice(i + 1); }
        }
        if (buf) scan(buf);
      } catch (e) {}
    };
    // Same capture for XHR, in case the panel does not use fetch for the turn.
    const XHR = window.XMLHttpRequest;
    if (XHR && XHR.prototype && !XHR.prototype.__bridgeHooked) {
      const origSend = XHR.prototype.send;
      XHR.prototype.send = function () {
        try {
          let seen = 0, buf = "";
          const drain = (final) => {
            try {
              const t = this.responseText || "";
              if (t.length > seen) { buf += t.slice(seen); seen = t.length; }
              const i = buf.lastIndexOf("\n");
              if (i >= 0) { scan(buf.slice(0, i)); buf = buf.slice(i + 1); }
              if (final && buf) { scan(buf); buf = ""; }
            } catch (e) {}
          };
          this.addEventListener("progress", () => drain(false));
          this.addEventListener("loadend", () => drain(true));
        } catch (e) {}
        return origSend.apply(this, arguments);
      };
      XHR.prototype.__bridgeHooked = true;
    }
    const origFetch = window.fetch;
    window.fetch = function () {
      return origFetch.apply(this, arguments).then((res) => {
        try {
          const ct = (res.headers && res.headers.get("content-type")) || "";
          if (ct.indexOf("event-stream") !== -1 && res.body) pump(res.clone());
        } catch (e) {}
        return res;
      });
    };
    window.__bridgeCtx = state;
  }
  const s = window.__bridgeCtx;
  return { input_tokens: s.input_tokens, output_tokens: s.output_tokens, at: s.at };
})()
