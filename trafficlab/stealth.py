"""Stealth shims: remove the *common browser automation tells* for local testing.

Scope: LOOPBACK ONLY. `Settings.validate()` refuses `--stealth` for any
non-loopback target, so this can only ever run against a local mirror of a
site you control (see `trafficlab.mirror`). It exists to answer one question
about your own detector:

    "does my detector do more than read the obvious automation tells?"

Each shim is labelled with the detector question it answers, so a run on the
mirror tells you *which* tells your protection actually checks.

Deliberately NOT here (and not coming): TLS/JA3 shaping, canvas/WebGL noise,
header spoofing, CDP-level patching, challenge solving. That would be an
evasion toolkit aimed at other people's systems; this file is a test fixture
for your own localhost.
"""

from __future__ import annotations

from playwright.async_api import BrowserContext

STEALTH_SCRIPT = """
(() => {
  const ua = navigator.userAgent || "";
  const isChromium = /Chrome|Chromium|Edg/.test(ua) && !/Firefox/.test(ua);

  // 1. navigator.webdriver - THE automation tell (true under Selenium,
  //    Playwright and Puppeteer). Detector question: does my rule read
  //    navigator.webdriver and nothing else?
  try {
    Object.defineProperty(navigator, "webdriver",
      { get: () => undefined, configurable: true });
  } catch (e) {}

  // 2. window.chrome - headless/minimal contexts often lack the chrome object
  //    that real Chrome pages have. Only added for Chromium UAs so a Firefox
  //    profile stays internally consistent. Detector question: does my rule
  //    assume window.chrome exists?
  try {
    if (isChromium) {
      if (!window.chrome) window.chrome = {};
      if (!window.chrome.runtime)
        window.chrome.runtime = { connect: () => ({}), sendMessage: () => ({}) };
      if (!window.chrome.loadTimes) window.chrome.loadTimes = () => ({});
      if (!window.chrome.csi) window.chrome.csi = () => ({});
    }
  } catch (e) {}

  // 3. navigator.plugins - automation contexts report an empty plugin list.
  //    Detector question: does my rule treat "0 plugins" as a bot?
  try {
    if (navigator.plugins && navigator.plugins.length === 0) {
      const mk = (name, filename) => ({
        name, filename, description: name, length: 1,
        item: () => null, namedItem: () => null,
      });
      const list = [
        mk("Chrome PDF Plugin", "internal-pdf-viewer"),
        mk("Chrome PDF Viewer", "mhjfbmdgcfjbbpaeojofohoefgiehjai"),
        mk("Native Client", "internal-nacl-plugin"),
      ];
      list.item = i => list[i] || null;
      list.namedItem = n => list.find(p => p.name === n) || null;
      Object.defineProperty(navigator, "plugins",
        { get: () => list, configurable: true });
    }
  } catch (e) {}
})();
"""


async def apply(context: BrowserContext) -> None:
    """Install the shims into every page of this context (init script)."""
    await context.add_init_script(STEALTH_SCRIPT)
