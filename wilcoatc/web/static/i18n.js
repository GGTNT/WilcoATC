/* The interface, in whatever language the person reading it uses.
 *
 * This is the panel's own language and has nothing to do with what is spoken
 * on the radio. The radio follows the aeroplane -- English everywhere plus the
 * local language over a state that works one -- and no setting changes that.
 * What this changes is the words around it: the menu, the labels, the switches.
 *
 * English is not a catalogue. It is what the markup already says, so a missing
 * key or a missing file degrades to readable English rather than to a blank
 * screen or a raw key name.
 */

const I18N = (() => {

  // Every language the panel is translated into, written in its own language,
  // because a person looking for their language is looking for its own name.
  const LANGUAGES = [
    ["en", "English"],
    ["ar", "العربية"],
    ["bg", "Български"],
    ["ca", "Català"],
    ["cs", "Čeština"],
    ["da", "Dansk"],
    ["de", "Deutsch"],
    ["el", "Ελληνικά"],
    ["es", "Español"],
    ["et", "Eesti"],
    ["fa", "فارسی"],
    ["fi", "Suomi"],
    ["fr", "Français"],
    ["gl", "Galego"],
    ["he", "עברית"],
    ["hi", "हिन्दी"],
    ["hr", "Hrvatski"],
    ["hu", "Magyar"],
    ["id", "Indonesia"],
    ["it", "Italiano"],
    ["ja", "日本語"],
    ["ko", "한국어"],
    ["lt", "Lietuvių"],
    ["lv", "Latviešu"],
    ["ms", "Melayu"],
    ["nb", "Norsk"],
    ["nl", "Nederlands"],
    ["pl", "Polski"],
    ["pt", "Português"],
    ["ro", "Română"],
    ["ru", "Русский"],
    ["sk", "Slovenčina"],
    ["sl", "Slovenščina"],
    ["sr", "Српски"],
    ["sv", "Svenska"],
    ["th", "ไทย"],
    ["tr", "Türkçe"],
    ["uk", "Українська"],
    ["vi", "Tiếng Việt"],
    ["zh-Hans", "简体中文"],
    ["zh-Hant", "繁體中文"],
  ];

  // Scripts written right to left. The whole document flips, which is what
  // these readers expect and what the CSS logical properties already handle.
  const RTL = new Set(["ar", "he", "fa"]);

  const cache = new Map();
  let current = "en";
  let strings = {};

  const codes = () => LANGUAGES.map(([code]) => code);

  /* The English the markup already carries, captured once so switching back
   * to English -- or to a language with a key missing -- restores it rather
   * than leaving the last language's word in place. */
  const english = new Map();
  let captured = false;

  function capture() {
    if (captured) return;
    captured = true;
    for (const node of document.querySelectorAll("[data-i18n]")) {
      english.set(node, node.textContent.trim());
    }
    for (const [attribute, key] of ATTRIBUTES) {
      for (const node of document.querySelectorAll(`[${key}]`)) {
        english.set(node.getAttribute(key), node.getAttribute(attribute));
      }
    }
  }

  const ATTRIBUTES = [
    ["placeholder", "data-i18n-placeholder"],
    ["title", "data-i18n-title"],
    ["aria-label", "data-i18n-aria"],
  ];

  /* What the browser is set to, narrowed to a language we actually have.
   * "fr-CA" becomes "fr"; Chinese keeps its script, because simplified and
   * traditional are not interchangeable. */
  function detect() {
    const have = new Set(codes());
    for (const tag of navigator.languages || [navigator.language || "en"]) {
      if (!tag) continue;
      if (have.has(tag)) return tag;
      const lower = tag.toLowerCase();
      if (lower.startsWith("zh")) {
        return (lower.includes("hant") || lower.includes("tw")
                || lower.includes("hk") || lower.includes("mo"))
          ? "zh-Hant" : "zh-Hans";
      }
      const base = lower.split("-")[0];
      if (have.has(base)) return base;
      if (base === "no" || base === "nn") return "nb";
      if (base === "in") return "id";       // the old code for Indonesian
      if (base === "iw") return "he";       // the old code for Hebrew
    }
    return "en";
  }

  async function load(code) {
    if (code === "en") return {};
    if (cache.has(code)) return cache.get(code);
    let table = {};
    try {
      const response = await fetch(`/i18n/${code}.json`, { cache: "no-cache" });
      if (response.ok) table = await response.json();
    } catch (error) {
      // A missing catalogue is not a failure worth showing anybody: the page
      // is already readable in English.
      table = {};
    }
    cache.set(code, table);
    return table;
  }

  function t(key, fallback) {
    const found = strings[key];
    return (found === undefined || found === "") ? (fallback ?? key) : found;
  }

  function paint() {
    for (const node of document.querySelectorAll("[data-i18n]")) {
      const key = node.getAttribute("data-i18n");
      node.textContent = t(key, english.get(node) ?? node.textContent);
    }
    for (const [attribute, key] of ATTRIBUTES) {
      for (const node of document.querySelectorAll(`[${key}]`)) {
        const name = node.getAttribute(key);
        node.setAttribute(attribute, t(name, english.get(name)));
      }
    }
  }

  async function apply(code) {
    capture();
    const wanted = codes().includes(code) ? code : "en";
    strings = await load(wanted);
    current = wanted;
    document.documentElement.lang = wanted;
    document.documentElement.dir = RTL.has(wanted) ? "rtl" : "ltr";
    paint();
    // Anything drawn from the script rather than from the markup has to be
    // redrawn now that the words have changed.
    document.dispatchEvent(new CustomEvent("i18n:changed", { detail: wanted }));
    return wanted;
  }

  return {
    LANGUAGES, RTL, codes, detect, apply, t,
    get language() { return current; },
  };
})();
