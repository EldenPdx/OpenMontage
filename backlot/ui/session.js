import { t } from "/ui/i18n.js";

let current, pending, generation = 0;
const reads = new Set(), streams = new Set();

export function authScope(value = current) {
  return value?.mode === "local" ? "local" : value ? `${value.user.user_id}|${value.workspace.key}|${value.context_id}` : null;
}

export function activeScope(scope) { return scope != null && scope === authScope(); }

export function scopedKey(name) {
  const scope = authScope();
  return scope === "local" ? name : scope ? `${name}@${scope}` : null;
}

export function invalidateSession(reason = "identity") {
  generation++;
  for (const read of reads) read.abort();
  for (const source of streams) source.close();
  reads.clear(); streams.clear(); pending = null; current = null;
  if (!["unavailable", "restore"].includes(reason)) for (const storage of [localStorage, sessionStorage]) for (const key of Object.keys(storage)) {
    if (key.startsWith("backlot.studio.")) storage.removeItem(key);
  }
  window.dispatchEvent(new CustomEvent("accountchange", { detail: { reason } }));
}

function sessionError(message, status, code) {
  const error = new Error(t(message));
  Object.assign(error, { i18nKey: message, status, code });
  return error;
}

export function session(refresh = false) {
  if (!pending || refresh) {
    const epoch = generation;
    pending = fetch("/api/auth/me", { credentials: "same-origin", cache: "no-store" }).then(async (response) => {
      if (epoch !== generation) throw sessionError("Your workspace changed. Review the current space before continuing.", 409, "auth_context_conflict");
      if (response.status === 401) {
        invalidateSession("signedout"); pending = Promise.resolve(null); return current = null;
      }
      if (!response.ok) throw sessionError("The account service is unavailable. Please try again.", response.status);
      const value = await response.json();
      if (epoch !== generation) throw sessionError("Your workspace changed. Review the current space before continuing.", 409, "auth_context_conflict");
      if (current && authScope(current) !== authScope(value)) invalidateSession("identity");
      const changed = current && JSON.stringify(current.permissions) !== JSON.stringify(value.permissions);
      if (!current) for (const storage of [localStorage, sessionStorage]) {
        for (const key of Object.keys(storage)) if (key.startsWith("backlot.studio.")) {
          const matching = value.mode === "local" ? !key.includes("@") : key.endsWith(`@${authScope(value)}`);
          if (!matching) storage.removeItem(key);
        }
      }
      current = value;
      pending = Promise.resolve(value);
      window.dispatchEvent(new Event("accountready"));
      if (changed) window.dispatchEvent(new Event("permissionschange"));
      return current;
    }).catch((error) => { if (epoch === generation) pending = null; throw error; });
  }
  return pending;
}

export function can(permission) {
  return current?.mode === "local" || !!current?.permissions?.includes(permission);
}

export function clearProtectedView() {
  for (const media of document.querySelectorAll("video, audio")) {
    media.pause(); media.removeAttribute("src"); media.load();
  }
}

export async function responseError(response) {
  const body = await response.json().catch(() => ({}));
  const code = body?.error?.code;
  const messages = {
    invitation_invalid: "This invitation is unavailable. Check the intended account or request a new invitation.",
    last_admin: "An organization must keep at least one administrator.",
    last_admin_conflict: "An organization must keep at least one administrator.",
    rate_limited: "Too many requests. Wait before trying again.",
    identity_unavailable: "The account service is unavailable. Please try again.",
    dependency_unavailable: "The account service is unavailable. Please try again.",
    operation_pending: "The previous request outcome is unconfirmed. Retry the same action to confirm it.",
  };
  const message = response.status === 401 ? "Sign in to use your workspace."
    : response.status === 403 ? "You do not have permission for this action."
    : code === "auth_context_conflict" ? "Your workspace changed. Review the current space before continuing."
    : messages[code] || (response.status === 409 ? "The request conflicts with the current state. Refresh before trying again." : "The request failed. Please try again.");
  if (response.status === 401 || code === "auth_context_conflict") invalidateSession(response.status === 401 ? "signedout" : "identity");
  else if (response.status === 403) session(true).catch(() => invalidateSession("unavailable"));
  return sessionError(message, response.status, code);
}

export async function readJSON(url) {
  const value = await session();
  if (!value) throw sessionError("Sign in to use your workspace.", 401);
  const epoch = generation, controller = new AbortController();
  reads.add(controller);
  try {
    const response = await fetch(url, { credentials: "same-origin", cache: "no-store", signal: controller.signal });
    if (!response.ok) throw await responseError(response);
    const result = await response.json();
    if (epoch !== generation || (value.mode !== "local" && response.headers.get("X-Auth-Context") !== value.context_id)) {
      if (epoch === generation) { invalidateSession("identity"); session(true).catch(() => {}); }
      throw sessionError("Your workspace changed. Review the current space before continuing.", 409, "auth_context_conflict");
    }
    return result;
  } catch (error) {
    if (epoch !== generation) throw sessionError("Your workspace changed. Review the current space before continuing.", 409, "auth_context_conflict");
    throw error;
  } finally { reads.delete(controller); }
}

function trackStream(source) {
  streams.add(source);
  const close = source.close.bind(source);
  source.close = () => { streams.delete(source); close(); };
  return source;
}

export function scopedSource(url, configure, onError = () => {}) {
  const scope = authScope();
  let source, timer, closed = false;
  const subscription = trackStream({
    get readyState() { return closed ? EventSource.CLOSED : source?.readyState === EventSource.OPEN ? EventSource.OPEN : EventSource.CONNECTING; },
    close() { closed = true; clearTimeout(timer); source?.close(); },
  });
  function connect() {
    if (closed || !activeScope(scope)) { subscription.close(); return; }
    source = new EventSource(typeof url === "function" ? url() : url);
    configure(source, subscription);
    source.onerror = () => {
      source.close(); onError();
      session(true).then(() => {
        if (!closed && activeScope(scope)) timer = setTimeout(connect, 1000);
        else subscription.close();
      }).catch(() => { subscription.close(); invalidateSession("unavailable"); });
    };
  }
  connect(); return subscription;
}

export function savedRequest(name) {
  const key = scopedKey(name);
  if (!key) return null;
  try { return JSON.parse(localStorage.getItem(key) || "null"); }
  catch { localStorage.removeItem(key); return null; }
}

export async function writeJSON(path, body, { method = "POST", localConfig, transientKey } = {}) {
  let value = await session();
  const scope = authScope(value), epoch = generation;
  if (!scope) throw sessionError("Sign in to use your workspace.", 401);
  if (!activeScope(scope)) throw sessionError("Your workspace changed. Review the current space before continuing.", 409, "auth_context_conflict");
  const encoded = body === undefined ? undefined : JSON.stringify(body);
  const storageKey = scopedKey(`backlot.studio.pending:${method === "POST" ? "" : method + ":"}${path}`);
  let operation = !transientKey ? savedRequest(`backlot.studio.pending:${method === "POST" ? "" : method + ":"}${path}`) : null;
  if (operation && (operation.body !== encoded || (operation.scope && operation.scope !== scope))) {
    throw sessionError("The previous request outcome is unconfirmed. Refresh this task before choosing another action.");
  }
  operation ||= { key: transientKey || crypto.randomUUID(), body: encoded, scope };
  if (!transientKey) localStorage.setItem(storageKey, JSON.stringify(operation));
  let local = value.mode === "local" ? await localConfig() : null;
  const stillCurrent = () => epoch === generation && activeScope(scope);
  const forget = () => { if (!transientKey) localStorage.removeItem(storageKey); };
  const send = () => {
    if (!stillCurrent()) throw sessionError("Your workspace changed. Review the current space before continuing.", 409, "auth_context_conflict");
    return fetch(path, { method, credentials: "same-origin", headers: {
      "Content-Type": "application/json", "X-CSRF-Token": local?.csrf_token || value.csrf_token,
      ...(value.mode === "local" ? {} : { "X-Auth-Context": value.context_id }),
      "Idempotency-Key": operation.key,
    }, body: operation.body });
  };
  let response;
  try { response = await send(); }
  catch (error) {
    if (!stillCurrent()) { forget(); throw sessionError("Your workspace changed. Review the current space before continuing.", 409, "auth_context_conflict"); }
    throw sessionError("Connection lost. The request key is saved; retry this same action to confirm its outcome.");
  }
  if (!stillCurrent()) { forget(); throw sessionError("Your workspace changed. Review the current space before continuing.", 409, "auth_context_conflict"); }
  if (response.status === 403) {
    const rejection = await response.clone().json().catch(() => ({}));
    if (rejection?.error?.code === "csrf_invalid") {
      value = await session(true);
      if (!stillCurrent()) { forget(); throw sessionError("Your workspace changed. Review the current space before continuing.", 409, "auth_context_conflict"); }
      if (value.mode === "local") local = await localConfig(true);
      response = await send();
    }
  }
  if (!response.ok) {
    if (response.status < 500) forget();
    throw await responseError(response);
  }
  let result;
  try { result = response.status === 204 ? null : await response.json(); }
  catch { throw sessionError("The service response is unconfirmed. The request key is saved; retry this same action."); }
  if (!stillCurrent()) { forget(); throw sessionError("Your workspace changed. Review the current space before continuing.", 409, "auth_context_conflict"); }
  forget(); return result;
}

window.addEventListener("pagehide", () => { for (const source of streams) source.close(); for (const read of reads) read.abort(); });
window.addEventListener("pageshow", (event) => { if (event.persisted) { invalidateSession("restore"); location.reload(); } });
