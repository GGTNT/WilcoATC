/* How the window looks: the theme, the accent, the light behind the glass.
 *
 * Loaded in the head, before the body is drawn, so the first paint is already
 * in the pilot's colours rather than flashing the default ones. What it paints
 * from there is the last look this browser saw; the one that counts is in
 * config.yaml (ui.theme, ui.accent...), which app.js reads and hands to
 * apply() as soon as the settings arrive. Both, because the desktop window
 * runs in private mode and forgets its own storage when it closes, and a
 * tablet beside the yoke would otherwise open in the wrong theme every time.
 *
 * Everything here is a CSS custom property on the root element; the
 * stylesheet does the rest.
 */

const LOOK = (() => {

  const DEFAULT = {
    theme: "dark", accent: "#697f9b", scene: "accent",
    blur: 24, lights: true, grid: true, gradient: true,
  };

  // The accents offered as swatches. Slate first: it is the default, and the
  // quietest of them beside a cockpit.
  const SWATCHES = [
    "#697f9b", "#a78bfa", "#5eead4", "#fb7185", "#f59e0b",
    "#34d399", "#38bdf8", "#f472b6", "#e2e8f0", "#c4b5fd",
  ];

  // The two lights in the corners of the room, per scene. "accent" has none
  // of its own: the stylesheet derives them from the accent.
  const SCENES = {
    accent: null,
    dusk: ["rgba(105, 127, 155, 0.34)", "rgba(60, 110, 255, 0.26)"],
    night: ["rgba(80, 120, 255, 0.26)", "rgba(20, 200, 190, 0.18)"],
    aurora: ["rgba(60, 230, 170, 0.22)", "rgba(170, 90, 255, 0.26)"],
  };

  const KEY = "wilco.look";
  let current = { ...DEFAULT };

  /* Only what the server would also have accepted, so a stale or hand-edited
   * value in this browser's storage cannot put garbage in the style. */
  function clean(look) {
    const out = { ...DEFAULT };
    if (!look || typeof look !== "object") return out;
    if (look.theme === "dark" || look.theme === "light") out.theme = look.theme;
    if (typeof look.accent === "string" && /^#[0-9a-f]{6}$/i.test(look.accent)) {
      out.accent = look.accent.toLowerCase();
    }
    if (Object.prototype.hasOwnProperty.call(SCENES, look.scene)) out.scene = look.scene;
    const blur = Number(look.blur);
    if (Number.isFinite(blur)) out.blur = Math.max(0, Math.min(48, Math.round(blur)));
    if (typeof look.lights === "boolean") out.lights = look.lights;
    if (typeof look.grid === "boolean") out.grid = look.grid;
    if (typeof look.gradient === "boolean") out.gradient = look.gradient;
    return out;
  }

  /* Dark type on a light accent, white on a dark one. Every swatch offered
   * is light enough for dark type; a custom colour need not be. */
  function onAccent(hex) {
    const channel = (at) => {
      const c = parseInt(hex.slice(at, at + 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    };
    const luminance = 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
    return luminance > 0.18 ? "#0b0f1a" : "#ffffff";
  }

  function apply(look, { remember = true } = {}) {
    current = clean(look);
    const root = document.documentElement;
    root.dataset.theme = current.theme;
    root.style.setProperty("--accent", current.accent);
    root.style.setProperty("--on-accent", onAccent(current.accent));
    const scene = SCENES[current.scene];
    if (scene) {
      root.style.setProperty("--orb1", scene[0]);
      root.style.setProperty("--orb2", scene[1]);
    } else {
      root.style.removeProperty("--orb1");
      root.style.removeProperty("--orb2");
    }
    root.style.setProperty("--blur", `${current.blur}px`);
    root.style.setProperty("--grid", current.grid ? "0.35" : "0");
    root.classList.toggle("no-lights", !current.lights);
    root.classList.toggle("no-gradient", !current.gradient);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", current.theme === "light" ? "#e9edf4" : "#070a12");
    if (remember) {
      try { localStorage.setItem(KEY, JSON.stringify(current)); } catch (error) { /* fine */ }
    }
    document.dispatchEvent(new CustomEvent("look:changed", { detail: { ...current } }));
    return current;
  }

  // The last look this browser saw, until the real one arrives.
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (error) { stored = null; }
  apply(stored, { remember: false });

  return {
    DEFAULT, SWATCHES, SCENES, apply, clean,
    get current() { return { ...current }; },
  };
})();
