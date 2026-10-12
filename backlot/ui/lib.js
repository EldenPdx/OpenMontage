// Shared helpers for the Backlot UI.
import { t, getLocale } from "/ui/i18n.js";
import { readJSON, scopedSource, session, authScope, activeScope } from "/ui/session.js";

export async function getJSON(url) {
  return readJSON(url);
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function fmtDuration(seconds) {
  const n = Number(seconds);
  if (seconds == null || !Number.isFinite(n)) return "";
  const s = Math.max(0, Math.round(n));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

export function fmtMoney(v) {
  const n = Number(v);
  if (v == null || !Number.isFinite(n)) return "—";
  return new Intl.NumberFormat(getLocale(), { style: "currency", currency: "USD" }).format(n);
}

export function fmtAgo(epochSeconds) {
  if (!epochSeconds) return "";
  const diff = Date.now() / 1000 - epochSeconds;
  if (diff < 90) return t("just now");
  const formatter = new Intl.RelativeTimeFormat(getLocale(), { style: "short" });
  if (diff < 3600) return formatter.format(-Math.round(diff / 60), "minute");
  if (diff < 86400) return formatter.format(-Math.round(diff / 3600), "hour");
  return formatter.format(-Math.round(diff / 86400), "day");
}

export function fmtClock(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString(getLocale(), { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return "";
  }
}

export function mediaURL(projectId, relPath) {
  return `/media/${encodeURIComponent(projectId)}/${relPath.split("/").map(encodeURIComponent).join("/")}`;
}

// Downscaled cached JPEG for images (full media only in players/lightbox).
export function thumbURL(projectId, relPath, w = 640) {
  return `/thumb/${encodeURIComponent(projectId)}/${relPath.split("/").map(encodeURIComponent).join("/")}?w=${w}`;
}

// Subscribe to a server-sent change feed; call onChange (debounced) per burst.
export async function subscribe(url, onChange) {
  await session();
  const scope = authScope();
  if (!scope) return;
  let timer = null;
  return scopedSource(url, (source, subscription) => {
    source.onmessage = (msg) => {
      if (!activeScope(scope)) { subscription.close(); return; }
      try {
        const data = JSON.parse(msg.data);
        if (data.type !== "change") return;
      } catch { return; }
      clearTimeout(timer);
      timer = setTimeout(() => { if (activeScope(scope)) onChange(); }, 250);
    };
  }, () => { if (activeScope(scope)) onChange(); });
}

// Deterministic pseudo-waveform bars (seeded by a string).
export function waveBars(container, seedStr, count = 26, maxH = 14) {
  let seed = 0;
  for (const c of seedStr || "wave") seed = (seed * 31 + c.charCodeAt(0)) % 2147483647;
  seed = seed || 7;
  container.innerHTML = "";
  for (let i = 0; i < count; i++) {
    seed = (seed * 16807) % 2147483647;
    const h = 3 + ((seed % 100) / 100) * maxH * (0.55 + 0.45 * Math.sin(i / 5));
    const bar = document.createElement("i");
    bar.style.height = `${Math.max(3, h)}px`;
    container.append(bar);
  }
}

export const STAGE_ICONS = {
  completed: "✓",
  in_progress: "◉",
  awaiting_human: "◈",
  failed: "✕",
};
