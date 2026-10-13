import { el, getJSON, fmtMoney } from "/ui/lib.js";
import { t, localePath, initI18n, languageTabs } from "/ui/i18n.js";
import "/ui/studio-i18n.js";
import "/ui/account.js";
import { authScope, activeScope, can, scopedKey, savedRequest, session, writeJSON, scopedSource } from "/ui/session.js";

let configPromise;
let configScope;
export function studioConfig(refresh = false) {
  return session().then(() => {
    const scope = authScope();
    if (!configPromise || refresh || configScope !== scope) {
      configScope = scope;
      configPromise = getJSON("/api/studio/config").catch((error) => { if (configScope === scope) configPromise = null; throw error; });
    }
    return configPromise;
  });
}

export async function studioAction(path, body) {
  return writeJSON(path, body, { localConfig: studioConfig });
}

export function subscribeTask(taskId, onChange) {
  const scope = authScope();
  const cursorKey = scopedKey(`backlot.studio.cursor:${taskId}`);
  return scopedSource(() => `/api/studio/tasks/${encodeURIComponent(taskId)}/events?after=${Number(sessionStorage.getItem(cursorKey) || 0)}`, (source, subscription) => {
    for (const name of ["state", "stage", "approval", "progress", "cost", "error", "result", "recovery", "resync", "end"]) {
      source.addEventListener(name, (event) => {
        if (!activeScope(scope)) { subscription.close(); return; }
        if (event.lastEventId) sessionStorage.setItem(cursorKey, event.lastEventId);
        if (name === "end") subscription.close();
        onChange();
      });
    }
  }, () => { if (activeScope(scope)) onChange(); });
}

export function stateLabel(value) {
  const text = t(String(value || "unknown")).replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function moneyMicros(value) {
  return value == null ? t("Unquoted") : fmtMoney(Number(value) / 1_000_000);
}

export function costLabel(task) {
  const cost = task.cost || {};
  const known = t("{spent} recorded · {reserved} reserved", { spent: moneyMicros(cost.spent_usd_micros), reserved: moneyMicros(cost.reserved_usd_micros) });
  return cost.price_status === "unquoted" || cost.unknown_call_count > 0 ? t("Unquoted cost · {cost}", { cost: known }) : known;
}

export function localURL(value) {
  if (typeof value !== "string" || !value.startsWith("/")) return "#";
  const url = new URL(value, location.origin);
  return url.origin === location.origin ? localePath(url.pathname + url.search + url.hash) : "#";
}

const form = document.getElementById("studio-form");
if (form) initializeStudio();

async function initializeStudio() {
  initI18n();
  document.querySelector(".slate").append(languageTabs());
  document.documentElement.dataset.theme = localStorage.getItem("backlot.theme") === "light" ? "light" : "dark";
  const status = document.getElementById("studio-status");
  const profileSelect = document.getElementById("profile");
  const submit = document.getElementById("create-task");
  let config, selectedId, stream, tasks, currentTask, cancellingId, submitting = false, refreshPromise;
  let announcement = { message: "Loading available profiles…", error: false, translate: true };
  function renderStatus() {
    status.textContent = announcement.translate ? t(announcement.message) : announcement.message;
    status.classList.toggle("error", announcement.error);
  }
  function announce(message, error = false, translate = true) {
    announcement = { message, error, translate };
    status.removeAttribute("data-i18n");
    renderStatus();
  }
  function announceError(error, fallback) {
    const generated = error?.i18nKey;
    const useFallback = !error?.serverMessage && (!error?.message || error instanceof TypeError || error instanceof SyntaxError || /^\d{3} \/api\/studio(?:\/|\?|$)/.test(error.message));
    announce(generated || (useFallback ? fallback : error.message), true, !!generated || useFallback);
  }
  function renderProfiles() {
    for (const option of profileSelect.options) {
      const profile = config.profiles.find((item) => item.profile_id === option.value);
      option.textContent = `${profile.profile_id} · ${profile.model}${profile.ready ? "" : ` ${t("(not configured)")}`}`;
    }
    profileInfo();
  }
  function profileInfo() {
    const profile = config?.profiles.find((item) => item.profile_id === profileSelect.value);
    document.getElementById("profile-info").textContent = profile
      ? `${profile.provider} · ${profile.model} · ${t(profile.price_status === "unquoted" ? "Fees are unquoted; the budget is an estimate." : "Estimated pricing configured.")}` : t("No configured profile is available.");
    submit.disabled = submitting || !config?.ready || !profile?.ready || !can("production:create");
  }
  function taskCard(task, current = false) {
    const actions = el("div", { class: "studio-task-actions" },
      el("a", { href: localURL(task.board_url || `/p/${encodeURIComponent(task.project_id)}`) }, t("Open production board")));
    if (!current) actions.append(el("button", { type: "button", onclick: () => selectTask(task.task_id).catch((error) => announceError(error, "Could not load tasks. Please try again.")) }, t("View task")));
    if (current && !["succeeded", "failed", "cancelled", "cancel_requested"].includes(task.state)) {
      const cancel = el("button", { type: "button", onclick: async () => {
        cancellingId = task.task_id; cancel.disabled = true;
        try { await studioAction(`/api/studio/tasks/${task.task_id}/cancel`, { expected_version: task.version }); announce("Cancellation requested. Remote work may still finish and charge."); }
        catch (error) { announceError(error, "Could not cancel the task. Please try again."); }
        finally { cancellingId = null; }
        await refresh().catch((error) => announceError(error, "Could not load tasks. Please try again."));
      } }, t("Cancel task"));
      cancel.disabled = cancellingId === task.task_id || !can("production:cancel");
      actions.append(cancel);
    }
    const card = el("article", { class: "studio-task", "data-task-id": task.task_id },
      el("span", { class: "chip" }, stateLabel(task.state)), el("h3", {}, task.request?.brief || task.task_id),
      el("p", { class: "studio-task-meta" }, `${task.task_id} · ${task.config_snapshot?.model || ""}`),
      el("p", { class: "studio-task-meta" }, costLabel(task)),
      task.current_stage ? el("p", { class: "studio-task-meta" }, t("Stage: {stage}", { stage: stateLabel(task.current_stage) })) : null,
      task.error ? el("p", { class: "studio-status error" }, task.error.message || t("The task request failed.")) : null, actions);
    if (current && task.state === "succeeded" && task.result?.verified) {
      card.append(el("video", { controls: "", preload: "metadata", src: localURL(task.result.preview_url), "aria-label": t("Verified final video") }),
        el("a", { href: localURL(task.result.download_url), download: "" }, t("Download verified video")));
    }
    return card;
  }
  function renderTasks() {
    if (!tasks) return;
    const list = document.getElementById("task-list");
    list.replaceChildren(...tasks.filter((task) => task.task_id !== selectedId).map((task) => taskCard(task)));
    if (!tasks.length) list.append(el("p", { class: "studio-hint" }, t("Your queued and completed tasks will appear here.")));
    document.getElementById("current-task").replaceChildren(...(currentTask && currentTask.task_id === selectedId ? [taskCard(currentTask, true)] : []));
  }
  async function refresh() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      tasks = await getJSON("/api/studio/tasks");
      if (selectedId) {
        const taskId = selectedId;
        const task = await getJSON(`/api/studio/tasks/${encodeURIComponent(taskId)}`);
        if (taskId === selectedId) {
          currentTask = task;
          if (["succeeded", "cancelled", "failed"].includes(task.state)) stream?.close();
        }
      }
      renderTasks();
    })().finally(() => { refreshPromise = null; });
    return refreshPromise;
  }
  async function selectTask(taskId) {
    selectedId = taskId;
    stream?.close();
    await refresh();
    localStorage.setItem(scopedKey("backlot.studio.last-task"), taskId);
    const url = new URL(location.href);
    if (url.searchParams.get("task") !== taskId) { url.searchParams.set("task", taskId); history.replaceState(null, "", url); }
    stream = subscribeTask(taskId, () => refresh().catch((error) => announceError(error, "Could not load tasks. Please try again.")));
  }
  function localizeValidity(field) {
    if (!field.willValidate) return;
    field.setCustomValidity("");
    const validity = field.validity;
    const message = validity.badInput ? t("Enter a number.")
      : validity.valueMissing ? t(field.id === "profile" ? "Choose an agent profile." : "Complete this field.")
      : validity.tooShort ? t("Use at least {count} characters.", { count: field.minLength })
      : validity.tooLong ? t("Use no more than {count} characters.", { count: field.maxLength })
      : validity.rangeUnderflow ? t("Enter a value of at least {min}.", { min: field.min })
      : validity.rangeOverflow ? t("Enter a value no greater than {max}.", { max: field.max })
      : validity.stepMismatch ? t("Use increments of {step}.", { step: field.step })
      : !validity.valid ? t("Enter a valid value.") : "";
    field.setCustomValidity(message);
  }
  form.addEventListener("invalid", (event) => localizeValidity(event.target), true);
  form.addEventListener("input", (event) => event.target.setCustomValidity?.(""));
  window.addEventListener("languagechange", () => {
    renderStatus();
    if (config) renderProfiles();
    renderTasks();
    for (const field of form.elements) localizeValidity(field);
  });
  window.addEventListener("pagehide", () => stream?.close());
  window.addEventListener("accountchange", () => {
    stream?.close(); configPromise = null; config = null; tasks = []; selectedId = currentTask = null;
    form.reset(); profileSelect.replaceChildren();
    document.getElementById("production-inputs").disabled = true;
    renderTasks();
    const url = new URL(location.href); url.searchParams.delete("task"); history.replaceState(null, "", url);
    announce("Your workspace changed. Review the current space before continuing.", true);
  });
  window.addEventListener("permissionschange", () => {
    document.getElementById("production-inputs").disabled = !config?.ready || !can("production:create");
    renderTasks(); profileInfo();
    if (!can("production:create")) announce("You have read access to this workspace. Creating, approving and changing tasks is unavailable.");
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); if (submitting || !config?.ready || !can("production:create") || !form.reportValidity()) return;
    submitting = true; profileInfo(); form.setAttribute("aria-busy", "true");
    try {
      const task = await studioAction("/api/studio/tasks", {
        brief: document.getElementById("brief").value.trim(), profile_id: profileSelect.value,
        duration_seconds: Number(document.getElementById("duration").value), aspect_ratio: document.getElementById("aspect-ratio").value,
        narration: document.getElementById("narration").checked,
        budget_usd_micros: Math.round(Number(document.getElementById("budget").value) * 1_000_000),
      });
      announce("Task queued. Open its board to review the production."); await selectTask(task.task_id);
    } catch (error) { announceError(error, "Could not create the task. Please try again."); }
    finally { submitting = false; profileInfo(); form.setAttribute("aria-busy", "false"); }
  });
  try {
    config = await studioConfig();
    for (const profile of config.profiles) {
      const option = el("option", { value: profile.profile_id });
      option.disabled = !profile.ready; profileSelect.append(option);
    }
    profileSelect.value = config.default_profile;
    if (!config.profiles.find((item) => item.profile_id === profileSelect.value)?.ready) profileSelect.value = config.profiles.find((item) => item.ready)?.profile_id || "";
    document.getElementById("production-inputs").disabled = !config.ready || !can("production:create");
    profileSelect.addEventListener("change", profileInfo); renderProfiles();
    if (!config.ready) { announce(config.unavailable_reason || "Studio is unavailable. Configure the backend to enable execution.", true); return; }
    announce("Ready. Tasks continue running when this browser is closed.");
    const pending = savedRequest("backlot.studio.pending:/api/studio/tasks");
    if (pending) {
      const draft = JSON.parse(pending.body);
      document.getElementById("brief").value = draft.brief;
      profileSelect.value = draft.profile_id;
      document.getElementById("duration").value = draft.duration_seconds;
      document.getElementById("aspect-ratio").value = draft.aspect_ratio;
      document.getElementById("budget").value = draft.budget_usd_micros / 1_000_000;
      document.getElementById("narration").checked = draft.narration;
      profileInfo();
      announce("A previous submission is unconfirmed. Submit the same brief to recover its saved request.");
    }
    await refresh();
    const savedId = new URLSearchParams(location.search).get("task") || localStorage.getItem(scopedKey("backlot.studio.last-task"));
    if (savedId && savedId !== selectedId) await selectTask(savedId);
  } catch (error) { announceError(error, config ? "Could not load tasks. Please try again." : "Could not load Studio. Please try again."); }
}
