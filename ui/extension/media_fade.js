// Media fade, page part. background.js injects this file into the page's own
// JavaScript context before the site's scripts run, together with
// media_fade_state.js.
//
// While media fade is on and Chrome is in the background (another app has
// focus):
//   - the video keeps playing: the page always reports itself as visible and
//     focused, and pause() calls are ignored;
//   - the volume fades down to LOW, and back up when Chrome returns.
(() => {
  const LOW = 0.12;          // share of the normal volume in the background
  const FADE_DOWN_MS = 250;
  const FADE_UP_MS = 500;
  const CHECK_MS = 50;

  // Both attributes are set by media_fade_state.js. Switching FADE off in the
  // popup takes effect at once: every change below steps aside.
  const attr = (name) => document.documentElement?.getAttribute(name);
  const isOn = () => attr("data-media-fade-on") !== "0";
  const inBackground = () => attr("data-media-fade-bg") === "1";

  // Keep playing: while on, the page always looks visible and focused.
  const fixed = (prop, value) => {
    const real = Object.getOwnPropertyDescriptor(Document.prototype, prop).get;
    Object.defineProperty(Document.prototype, prop, {
      configurable: true,
      get() { return isOn() ? value : real.call(this); },
    });
  };
  fixed("hidden", false);
  fixed("webkitHidden", false);
  fixed("visibilityState", "visible");
  fixed("webkitVisibilityState", "visible");
  const realHasFocus = Document.prototype.hasFocus;
  Document.prototype.hasFocus = function () {
    return isOn() || realHasFocus.call(this);
  };

  const swallow = (e) => { if (isOn()) e.stopImmediatePropagation(); };
  for (const type of ["visibilitychange", "webkitvisibilitychange"]) {
    window.addEventListener(type, swallow, true);
    document.addEventListener(type, swallow, true);
  }
  // Only the window's own blur. Blur on fields inside the page still works.
  window.addEventListener("blur", (e) => {
    if (e.target === window || e.target === document) swallow(e);
  }, true);

  const realPause = HTMLMediaElement.prototype.pause;
  HTMLMediaElement.prototype.pause = function () {
    if (!(isOn() && inBackground())) return realPause.call(this);
  };

  // Volume: each element plays at (its own volume x factor). The site reads
  // back its own volume, so its volume slider is never changed.
  const realVolume = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "volume");
  const ownVolume = new WeakMap();
  let factor = 1;

  const volumeOf = (el) => (ownVolume.has(el) ? ownVolume.get(el) : realVolume.get.call(el));
  const play = (el, v) => {
    ownVolume.set(el, v);
    realVolume.set.call(el, v * factor);
  };

  Object.defineProperty(HTMLMediaElement.prototype, "volume", {
    configurable: true,
    get() { return volumeOf(this); },
    set(v) { play(this, v); },
  });

  const applyAll = () => {
    for (const el of document.querySelectorAll("video, audio")) play(el, volumeOf(el));
  };

  let target = 1;
  let timer = 0;
  const fadeTo = (to) => {
    if (to === target) return;
    target = to;
    clearInterval(timer);
    const from = factor;
    const ms = to < from ? FADE_DOWN_MS : FADE_UP_MS;
    const start = performance.now();
    // setInterval, because requestAnimationFrame stops while the window is covered.
    timer = setInterval(() => {
      let t = Math.min(1, (performance.now() - start) / ms);
      t = t * t * (3 - 2 * t);  // ease in and out
      factor = from + (to - from) * t;
      applyAll();
      if (t >= 1) clearInterval(timer);
    }, 20);
  };

  setInterval(() => fadeTo(isOn() && inBackground() ? LOW : 1), CHECK_MS);
  document.addEventListener("play", applyAll, true);
})();
