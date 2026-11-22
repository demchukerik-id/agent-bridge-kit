// Media fade, state part. Runs in the extension's own context, which the
// page cannot change. It learns from background.js whether media fade is on
// and whether Chrome is in the background, and passes both to media_fade.js
// through attributes on <html>.
const show = ({ on, bg }) => {
  const html = document.documentElement;
  if (!html) return;
  html.setAttribute("data-media-fade-on", on ? "1" : "0");
  html.setAttribute("data-media-fade-bg", bg ? "1" : "0");
};

// Instant update when Chrome gains or loses focus, or FADE is switched.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "media-fade-state") show(msg);
});

// Check every second as well, in case a message was missed. Once the
// extension is reloaded, this copy can no longer reach it and stops.
let timer = 0;
const check = () => {
  if (!chrome.runtime?.id) return clearInterval(timer);
  try {
    chrome.runtime.sendMessage({ type: "media-fade-state?" })
      .then((r) => r && show(r))
      .catch(() => {});
  } catch {
    clearInterval(timer);
  }
};
check();
timer = setInterval(check, 1000);
