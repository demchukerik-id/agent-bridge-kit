// theme.js — light/dark switch for the bridge operator panel.
//
// Loaded BEFORE popup.js so the theme is applied on the first paint. The
// palette itself lives entirely in popup.html as CSS custom properties:
//   :root                    -> dark (default, the terminal-HUD look)
//   :root[data-theme=light]  -> light (paper terminal)
// popup.js writes var(--…) references into its inline styles, so both themes
// flow through without touching the render code.
//
// Preference persists in chrome.storage.local under "panelTheme".

(() => {
  const KEY = "panelTheme";
  const root = document.documentElement;

  function apply(theme) {
    if (theme === "light") root.setAttribute("data-theme", "light");
    else root.removeAttribute("data-theme");
    const btn = document.getElementById("theme-toggle");
    if (btn) {
      const isLight = theme === "light";
      btn.textContent = isLight ? "DRK" : "LGT";
      btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
    }
  }

  let current = "dark";

  // Read the stored preference as early as possible.
  try {
    chrome.storage.local.get([KEY], (res) => {
      current = res && res[KEY] === "light" ? "light" : "dark";
      apply(current);
    });
  } catch (_) {
    apply(current);
  }

  document.addEventListener("DOMContentLoaded", () => {
    apply(current);
    const btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      current = current === "light" ? "dark" : "light";
      apply(current);
      try { chrome.storage.local.set({ [KEY]: current }); } catch (_) {}
    });
  });
})();
