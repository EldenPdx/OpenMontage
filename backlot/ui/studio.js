import { el, getJSON } from "/ui/lib.js";

let configPromise;
export function studioConfig(refresh = false) {
  if (!configPromise || refresh) configPromise = getJSON("/api/studio/config").catch((error) => { configPromise = null; throw error; });
  return configPromise;
}

export async function studioAction(path, body) {
  const storageKey = `backlot.studio.pending:${path}`;
  const encoded = JSON.stringify(body);
  let pending = JSON.parse(localStorage.getItem(storageKey) || "null");
  if (pending && pending.body !== encoded) throw new Error("The previous request outcome is unconfirmed. Refresh this task before choosing another action.");
  if (!pending) {
    pending = { key: crypto.randomUUID(), body: encoded };
    localStorage.setItem(storageKey, JSON.stringify(pending));
  }
  let config = await studioConfig();
  let response;
  try {
    const send = () => fetch(path, { method: "POST", credentials: "same-origin", headers: {
      "Content-Type": "application/json", "X-CSRF-Token": config.csrf_token, "Idempotency-Key": pending.key,
    }, body: pending.body });
    response = await send();
    if (response.status === 403) { config = await studioConfig(true); response = await send(); }
  } catch {
    throw new Error("Connection lost. The request key is saved; retry this same action to confirm its outcome.");
  }
  let result;
  try { result = await response.json(); }
  catch { throw new Error("The service response is unconfirmed. The request key is saved; retry this same action."); }
  if (!response.ok) {
    if (response.status < 500) localStorage.removeItem(storageKey);
    const error = new Error(result.error?.message || "The task request failed.");
    error.status = response.status;
    throw error;
  }
  localStorage.removeItem(storageKey);
  return result;
}

export function subscribeTask(taskId, onChange) {
  const cursorKey = `backlot.studio.cursor:${taskId}`;
  const cursor = Number(sessionStorage.getItem(cursorKey) || 0);
  const source = new EventSource(`/api/studio/tasks/${encodeURIComponent(taskId)}/events?after=${cursor}`);
  for (const name of ["state", "stage", "approval", "progress", "cost", "error", "result", "recovery", "resync", "end"]) {
    source.addEventListener(name, (event) => {
      if (event.lastEventId) sessionStorage.setItem(cursorKey, event.lastEventId);
      if (name === "end") source.close();
      onChange();
    });
  }
  source.onerror = () => onChange();
  return source;
}

export function stateLabel(value) {
  const text = String(value || "unknown").replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function moneyMicros(value) {
  return value == null ? "Unquoted" : `$${(Number(value) / 1_000_000).toFixed(2)}`;
}

export function costLabel(task) {
  const cost = task.cost || {};
  const known = `${moneyMicros(cost.spent_usd_micros)} recorded · ${moneyMicros(cost.reserved_usd_micros)} reserved`;
  return cost.price_status === "unquoted" || cost.unknown_call_count > 0 ? `Unquoted cost · ${known}` : known;
}

export function localURL(value) {
  if (typeof value !== "string" || !value.startsWith("/")) return "#";
  const url = new URL(value, location.origin);
  return url.origin === location.origin ? url.pathname + url.search + url.hash : "#";
}

const form = document.getElementById("studio-form");
if (form) initializeStudio();

async function initializeStudio() {
  document.documentElement.dataset.theme = localStorage.getItem("backlot.theme") === "light" ? "light" : "dark";
  const status = document.getElementById("studio-status");
  const profileSelect = document.getElementById("profile");
  const submit = document.getElementById("create-task");
  let config, selectedId, stream, submitting = false, refreshPromise;
  const announce = (message, error = false) => { status.textContent = message; status.classList.toggle("error", error); };
  function profileInfo() {
    const profile = config.profiles.find((item) => item.profile_id === profileSelect.value);
    document.getElementById("profile-info").textContent = profile
      ? `${profile.provider} · ${profile.model} · ${profile.price_status === "unquoted" ? "Fees are unquoted; the budget is an estimate." : "Estimated pricing configured."}` : "No configured profile is available.";
    submit.disabled = submitting || !config.ready || !profile?.ready;
  }
  function taskCard(task, current = false) {
    const actions = el("div", { class: "studio-task-actions" },
      el("a", { href: localURL(task.board_url || `/p/${encodeURIComponent(task.project_id)}`) }, "Open production board"));
    if (!current) actions.append(el("button", { type: "button", onclick: () => selectTask(task.task_id) }, "View task"));
    if (current && !["succeeded", "failed", "cancelled", "cancel_requested"].includes(task.state)) {
      const cancel = el("button", { type: "button", onclick: async () => {
        cancel.disabled = true;
        try { await studioAction(`/api/studio/tasks/${task.task_id}/cancel`, { expected_version: task.version }); announce("Cancellation requested. Remote work may still finish and charge."); }
        catch (error) { announce(error.message, true); }
        await refresh();
      } }, "Cancel task");
      actions.append(cancel);
    }
    const card = el("article", { class: "studio-task", "data-task-id": task.task_id },
      el("span", { class: "chip" }, stateLabel(task.state)), el("h3", {}, task.request?.brief || task.task_id),
      el("p", { class: "studio-task-meta" }, `${task.task_id} · ${task.config_snapshot?.model || ""}`),
      el("p", { class: "studio-task-meta" }, costLabel(task)),
      task.current_stage ? el("p", { class: "studio-task-meta" }, `Stage: ${stateLabel(task.current_stage)}`) : null,
      task.error ? el("p", { class: "studio-status error" }, task.error.message) : null, actions);
    if (current && task.state === "succeeded" && task.result?.verified) {
      card.append(el("video", { controls: "", preload: "metadata", src: localURL(task.result.preview_url) }),
        el("a", { href: localURL(task.result.download_url), download: "" }, "Download verified video"));
    }
    return card;
  }
  async function refresh() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      const tasks = await getJSON("/api/studio/tasks");
      const list = document.getElementById("task-list");
      list.replaceChildren(...tasks.filter((task) => task.task_id !== selectedId).map((task) => taskCard(task)));
      if (!tasks.length) list.append(el("p", { class: "studio-hint" }, "Your queued and completed tasks will appear here."));
      if (selectedId) {
        const task = await getJSON(`/api/studio/tasks/${encodeURIComponent(selectedId)}`);
        document.getElementById("current-task").replaceChildren(taskCard(task, true));
        if (["succeeded", "cancelled", "failed"].includes(task.state)) stream?.close();
      }
    })().finally(() => { refreshPromise = null; });
    return refreshPromise;
  }
  async function selectTask(taskId) {
    selectedId = taskId;
    localStorage.setItem("backlot.studio.last-task", taskId);
    const url = new URL(location.href); url.searchParams.set("task", taskId); history.replaceState(null, "", url);
    stream?.close();
    stream = subscribeTask(taskId, () => refresh().catch((error) => announce(error.message, true)));
    await refresh();
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); if (submitting || !config?.ready || !form.reportValidity()) return;
    submitting = true; profileInfo(); form.setAttribute("aria-busy", "true");
    try {
      const task = await studioAction("/api/studio/tasks", {
        brief: document.getElementById("brief").value.trim(), profile_id: profileSelect.value,
        duration_seconds: Number(document.getElementById("duration").value), aspect_ratio: document.getElementById("aspect-ratio").value,
        narration: document.getElementById("narration").checked,
        budget_usd_micros: Math.round(Number(document.getElementById("budget").value) * 1_000_000),
      });
      announce("Task queued. Open its board to review the production."); await selectTask(task.task_id);
    } catch (error) { announce(error.message, true); }
    finally { submitting = false; profileInfo(); form.setAttribute("aria-busy", "false"); }
  });
  try {
    config = await studioConfig();
    for (const profile of config.profiles) {
      const option = el("option", { value: profile.profile_id }, `${profile.profile_id} · ${profile.model}${profile.ready ? "" : " (not configured)"}`);
      option.disabled = !profile.ready; profileSelect.append(option);
    }
    profileSelect.value = config.default_profile;
    if (!config.profiles.find((item) => item.profile_id === profileSelect.value)?.ready) profileSelect.value = config.profiles.find((item) => item.ready)?.profile_id || "";
    document.getElementById("production-inputs").disabled = !config.ready;
    profileSelect.addEventListener("change", profileInfo); profileInfo();
    if (!config.ready) { announce(config.unavailable_reason || "Studio is unavailable. Configure the backend to enable execution.", true); return; }
    announce("Ready. Tasks continue running when this browser is closed.");
    const pending = JSON.parse(localStorage.getItem("backlot.studio.pending:/api/studio/tasks") || "null");
    if (pending) {
      const draft = JSON.parse(pending.body);
      document.getElementById("brief").value = draft.brief;
      profileSelect.value = draft.profile_id;
      document.getElementById("duration").value = draft.duration_seconds;
      document.getElementById("aspect-ratio").value = draft.aspect_ratio;
      document.getElementById("budget").value = draft.budget_usd_micros / 1_000_000;
      document.getElementById("narration").checked = draft.narration;
      announce("A previous submission is unconfirmed. Submit the same brief to recover its saved request.");
    }
    await refresh();
    const savedId = new URLSearchParams(location.search).get("task") || localStorage.getItem("backlot.studio.last-task");
    if (savedId) await selectTask(savedId);
  } catch (error) { announce(error.message, true); }
  window.addEventListener("pagehide", () => stream?.close());
}
