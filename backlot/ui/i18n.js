// The URL is the language preference, including on shared links and reloads.
const LOCALES = ["zh", "ja", "en"];
const messages = { zh: Object.create(null), ja: Object.create(null), en: Object.create(null) };

export function getLocale() {
  const prefix = globalThis.location?.pathname.split("/")[1];
  return LOCALES.includes(prefix) ? prefix : "zh";
}

export function registerMessages(translations) {
  for (const locale of LOCALES) Object.assign(messages[locale], translations[locale]);
}

export function t(message, values = {}) {
  return (messages[getLocale()][message] ?? message).replace(/\{(\w+)\}/g,
    (match, key) => values[key] == null ? match : String(values[key]));
}

export function localePath(path, locale = getLocale()) {
  const origin = globalThis.location?.origin || "http://localhost";
  const url = new URL(path, origin);
  if (url.origin !== origin) return path;
  const page = url.pathname.replace(/^\/(zh|ja|en)(?=\/|$)/, "") || "/";
  if (!/^(\/|\/studio\/?|\/p\/.+|\/invitations\/accept)$/.test(page)) return path;
  const language = LOCALES.includes(locale) ? locale : "zh";
  return `/${language}${page === "/" ? "" : page}${url.search}${url.hash}`;
}

registerMessages({
  zh: {
    "Language": "语言", "just now": "刚刚",
    "idea": "创意", "research": "研究", "proposal": "方案", "script": "脚本",
    "scene_plan": "分镜", "assets": "素材", "edit": "剪辑", "compose": "合成", "publish": "发布",
    "pending": "待开始", "completed": "已完成", "in_progress": "进行中",
    "awaiting_human": "等待审核", "failed": "失败",
  },
  ja: {
    "Language": "言語", "just now": "たった今",
    "idea": "アイデア", "research": "リサーチ", "proposal": "企画", "script": "脚本",
    "scene_plan": "絵コンテ", "assets": "素材", "edit": "編集", "compose": "合成", "publish": "公開",
    "pending": "開始待ち", "completed": "完了", "in_progress": "進行中",
    "awaiting_human": "レビュー待ち", "failed": "失敗",
  },
  en: { "scene_plan": "scene plan", "in_progress": "in progress", "awaiting_human": "awaiting approval" },
});

function applyTranslations() {
  document.documentElement.lang = getLocale();
  document.getElementById("language-panel")?.setAttribute("aria-labelledby", `language-tab-${getLocale()}`);
  for (const attribute of ["", "placeholder", "title", "aria-label"]) {
    const dataAttribute = attribute ? `data-i18n-${attribute}` : "data-i18n";
    for (const element of document.querySelectorAll(`[${dataAttribute}]`)) {
      const text = t(element.getAttribute(dataAttribute));
      if (attribute) element.setAttribute(attribute, text);
      else element.textContent = text;
    }
  }
  for (const link of document.querySelectorAll("[data-locale-link]")) {
    link.setAttribute("href", localePath(link.getAttribute("href")));
  }
  for (const tab of document.querySelectorAll(".language-tabs [role=tab]")) {
    const selected = tab.dataset.locale === getLocale();
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
}

let initialized = false;
export function initI18n() {
  applyTranslations();
  if (initialized) return;
  initialized = true;
  window.addEventListener("popstate", () => {
    const tabFocused = document.activeElement?.closest(".language-tabs");
    applyTranslations();
    window.dispatchEvent(new Event("languagechange"));
    if (tabFocused) document.querySelector(`.language-tabs [data-locale=${getLocale()}]`)?.focus();
  });
}

function changeLanguage(locale) {
  if (locale === getLocale()) return;
  history.pushState(history.state, "", localePath(location.href, locale));
  applyTranslations();
  window.dispatchEvent(new Event("languagechange"));
  document.querySelector(`.language-tabs [data-locale=${locale}]`)?.focus();
}

export function languageTabs() {
  const group = document.createElement("div");
  group.className = "language-tabs";
  group.setAttribute("role", "tablist");
  group.setAttribute("aria-label", t("Language"));
  group.setAttribute("data-i18n-aria-label", "Language");
  const labels = { zh: "中文", ja: "日本語", en: "English" };
  for (const locale of LOCALES) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.id = `language-tab-${locale}`;
    tab.setAttribute("aria-controls", "language-panel");
    tab.dataset.locale = locale;
    tab.lang = locale;
    tab.textContent = labels[locale];
    tab.setAttribute("aria-selected", String(locale === getLocale()));
    tab.tabIndex = locale === getLocale() ? 0 : -1;
    tab.addEventListener("click", () => changeLanguage(locale));
    tab.addEventListener("keydown", (event) => {
      const index = LOCALES.indexOf(locale);
      const next = event.key === "Home" ? 0 : event.key === "End" ? LOCALES.length - 1
        : event.key === "ArrowRight" ? (index + 1) % LOCALES.length
        : event.key === "ArrowLeft" ? (index + LOCALES.length - 1) % LOCALES.length : null;
      if (next == null) return;
      event.preventDefault();
      changeLanguage(LOCALES[next]);
      group.querySelector(`[data-locale=${LOCALES[next]}]`)?.focus();
    });
    group.append(tab);
  }
  return group;
}
