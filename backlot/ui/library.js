import { el, fmtAgo, getJSON, subscribe, thumbURL } from "/ui/lib.js";
import { t, registerMessages, localePath, initI18n, languageTabs } from "/ui/i18n.js";
import messages from "/ui/library-i18n.js";
import "/ui/account.js";

registerMessages(messages);
initI18n();

const grid = document.getElementById("grid");
let projects = [];
const THEME_KEY = "backlot.theme";
let currentTheme = localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";

function applyTheme(theme) {
  currentTheme = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = currentTheme;
  localStorage.setItem(THEME_KEY, currentTheme);
}

function renderThemeToggle() {
  const next = currentTheme === "light" ? "dark" : "light";
  const label = t(next === "light" ? "Switch to light theme" : "Switch to dark theme");
  return el("button", {
    class: "theme-toggle",
    type: "button",
    title: label,
    "aria-label": label,
    "aria-pressed": currentTheme === "light" ? "true" : "false",
    onclick: () => {
      applyTheme(next);
      const replacement = renderThemeToggle();
      document.querySelector(".theme-toggle").replaceWith(replacement);
    },
  }, el("span", { class: "theme-toggle-icon", "aria-hidden": "true" }, currentTheme === "light" ? "☾" : "☀"));
}

applyTheme(currentTheme);
document.getElementById("liveBadge").before(languageTabs(), renderThemeToggle());

function miniRail(states) {
  const rail = el("div", { class: "mini-rail" });
  for (const s of states) {
    const cls = s.status === "completed" ? "d"
      : s.status === "in_progress" ? "a"
      : s.status === "awaiting_human" ? "w" : "";
    rail.append(el("i", { class: cls, title: `${t(s.name)}: ${t(s.status)}` }));
  }
  return rail;
}

function card(p) {
  const poster = el("div", { class: "lib-poster" });
  if (p.poster) {
    poster.append(el("img", { src: thumbURL(p.project_id, p.poster, 640), loading: "lazy", alt: "" }));
  } else {
    poster.append(el("span", { class: "lp-txt" }, t("NO MEDIA YET")));
  }
  if (p.live && p.active_stage) {
    poster.append(el("span", { class: "lp-live" },
      el("span", { class: "dot" }),
      p.awaiting_human ? t("◈ AWAITING YOU") : t("LIVE · {stage}", { stage: t(p.active_stage).toUpperCase() })));
  } else if (p.awaiting_human) {
    poster.append(el("span", { class: "lp-live" }, t("◈ AWAITING YOU")));
  }

  const meta = el("div", { class: "lb-meta" },
    el("span", { class: "chip" }, p.pipeline_type || t("unknown")),
    p.scene_count ? el("span", { class: "chip" }, t("{count} scenes", { count: p.scene_count })) : null,
    p.render_count ? el("span", { class: "chip" }, t("{count} renders", { count: p.render_count })) : null,
    el("span", { class: "when" }, fmtAgo(p.last_activity)),
  );

  const staticSuffix = new URLSearchParams(location.search).has("static") ? "?static=1" : "";
  return el("a", { class: `lib-card${p.live ? " live-card" : ""}`, href: localePath(`/p/${p.project_id.split("/").map(encodeURIComponent).join("/")}${staticSuffix}`), style: "text-decoration:none;color:inherit" },
    poster,
    el("div", { class: "lib-body" },
      el("h3", {}, (p.title || p.project_id).toUpperCase()),
      meta,
      p.stage_states.length ? miniRail(p.stage_states) : null,
    ),
  );
}

function render() {
  document.getElementById("count").textContent = t("{count} projects", { count: projects.length });
  const liveCount = projects.filter((p) => p.live).length;
  const badge = document.getElementById("liveBadge");
  badge.classList.toggle("idle", liveCount === 0);
  document.getElementById("liveText").textContent = liveCount ? t("{count} LIVE", { count: liveCount }) : t("IDLE");
  grid.innerHTML = "";
  document.getElementById("empty").style.display = projects.length ? "none" : "block";
  document.getElementById("empty").textContent = t("No projects yet — create a task in Production Studio or run a production with the CLI.");
  for (const p of projects) grid.append(card(p));
}

async function refresh() {
  projects = await getJSON("/api/projects");
  render();
}

window.addEventListener("languagechange", () => {
  document.querySelector(".theme-toggle").replaceWith(renderThemeToggle());
  render();
});
window.addEventListener("accountchange", () => { projects = []; render(); });
refresh().catch(() => { projects = []; render(); });
if (!new URLSearchParams(location.search).has("static")) {
  subscribe("/api/library/events", () => refresh().catch(() => { projects = []; render(); })).catch(() => {});
}
