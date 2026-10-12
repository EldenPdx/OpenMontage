import { el, getJSON } from "/ui/lib.js";
import { t, localePath } from "/ui/i18n.js";
import { session, invalidateSession, responseError, clearProtectedView, can, writeJSON, authScope, activeScope } from "/ui/session.js";
import "/ui/account-i18n.js";

const panel = el("section", { id: "account-control", class: "account-control", "aria-label": t("Account") });
document.querySelector(".wrap").before(panel);
let me, logoutSession, organizations = [], members = [], invitations = [], managementOpen = false, loadingScope, announcement;
let renderScope, resetInputs = false;
const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("backlot.account") : null;

async function logout(button) {
  const previous = me || logoutSession;
  if (!previous) return;
  button.disabled = true;
  logoutSession = null; invalidateSession("signedout"); me = null; render(); channel?.postMessage("signedout");
  try {
    const response = await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin", headers: {
      "Content-Type": "application/json", "X-CSRF-Token": previous.csrf_token, "X-Auth-Context": previous.context_id,
    }, body: "{}" });
    if (!response.ok) throw await responseError(response);
    const result = await response.json();
    location.assign(result.logout_url || localePath("/"));
  } catch (error) {
    if (error.status !== 401 && error.code !== "auth_context_conflict") logoutSession = previous;
    render();
    panel.append(el("p", { role: "alert" }, error.message));
  }
}

async function pages(path) {
  const items = [];
  let after;
  do {
    const result = await getJSON(path + `?limit=100${after ? `&after=${encodeURIComponent(after)}` : ""}`);
    items.push(...result.items); after = result.next_cursor;
  } while (after);
  return items;
}

async function loadOrganizations() {
  if (!me || me.mode === "local") return;
  const scope = authScope(me);
  if (loadingScope === scope) return;
  loadingScope = scope;
  try { organizations = await pages("/api/organizations"); if (activeScope(scope)) render(); }
  catch (error) { if (activeScope(scope)) { announcement = error.i18nKey || error.message; render(); } }
  finally { if (loadingScope === scope) loadingScope = null; }
}

async function loadMembers() {
  if (!can("organization:manage") || me.workspace.kind !== "organization") return;
  const scope = authScope(me), path = `/api/organizations/${encodeURIComponent(me.workspace.owner_id)}`;
  try {
    const result = await Promise.all([pages(path + "/members"), pages(path + "/invitations")]);
    if (activeScope(scope)) { [members, invitations] = result; render(true); }
  } catch (error) { if (activeScope(scope)) { announcement = error.i18nKey || error.message; render(); } }
}

async function switchWorkspace(value) {
  const previous = me;
  invalidateSession("identity"); me = null; render(); channel?.postMessage("workspace");
  try {
    const response = await fetch("/api/auth/workspace", { method: "POST", credentials: "same-origin", headers: {
      "Content-Type": "application/json", "X-CSRF-Token": previous.csrf_token, "X-Auth-Context": previous.context_id,
    }, body: JSON.stringify({ kind: value === "personal" ? "personal" : "organization",
      owner_id: value === "personal" ? previous.user.user_id : value }) });
    if (!response.ok) throw await responseError(response);
    channel?.postMessage("workspace");
    location.replace(localePath(location.pathname.includes("/studio") ? "/studio" : "/"));
  } catch (error) { announcement = error.i18nKey || error.message; me = previous; render(); }
}

async function mutation(path, body, { method = "POST", button } = {}) {
  button.disabled = true; announcement = null;
  panel.setAttribute("aria-busy", "true");
  try {
    const result = await writeJSON(path, body, { method });
    resetInputs = true;
    announcement = "The change was saved.";
    await loadOrganizations(); await loadMembers();
    return result;
  } catch (error) { announcement = error.i18nKey || error.message; }
  finally { render(); panel.setAttribute("aria-busy", "false"); }
}

function roles(id, role) {
  const select = el("select", { id, "aria-label": t("Member role") });
  for (const value of ["admin", "editor", "viewer"]) select.append(el("option", { value }, t(value)));
  select.value = role || "viewer";
  return select;
}

function localizeForm(form) {
  form.addEventListener("invalid", (event) => {
    const field = event.target;
    const message = t(field.validity.typeMismatch ? "Enter a valid email address." : "Complete this field.");
    field.setCustomValidity(message); field.setAttribute("aria-invalid", "true");
    const id = field.id + "-error";
    document.getElementById(id)?.remove();
    form.append(el("p", { id, class: "account-error", role: "alert" }, message));
    field.setAttribute("aria-describedby", id);
  }, true);
  form.addEventListener("input", (event) => {
    event.target.setCustomValidity(""); event.target.removeAttribute("aria-invalid");
    document.getElementById(event.target.id + "-error")?.remove();
  });
  return form;
}

function management() {
  const details = el("details", { id: "account-management", class: "account-management" }, el("summary", {}, t("Manage organization")));
  details.open = managementOpen;
  details.addEventListener("toggle", () => { if (details.open !== managementOpen) { managementOpen = details.open; if (managementOpen) loadMembers(); } });
  const path = `/api/organizations/${encodeURIComponent(me.workspace.owner_id)}`;
  const email = el("input", { id: "invitation-email", type: "email", required: "", autocomplete: "email", maxlength: "254" });
  const role = roles("invitation-role");
  const send = el("button", { type: "submit" }, t("Send invitation"));
  const form = localizeForm(el("form", { onsubmit: (event) => {
    event.preventDefault(); if (!form.reportValidity()) return;
    mutation(path + "/invitations", { email: email.value.trim(), role: role.value }, { button: send });
  } }, el("label", { for: "invitation-email" }, t("Invitation email")), email,
  el("label", { for: "invitation-role" }, t("Member role")), role, send));
  const roster = el("ul", { class: "account-list", "aria-label": t("Members") });
  for (const member of members) {
    const label = member.email || member.name || member.user_id;
    const role = ["admin", "editor", "viewer"].find((value) => member.roles.includes(value));
    const select = roles(`member-role-${member.user_id}`, role);
    if (!role) { select.prepend(el("option", { value: "", disabled: "" }, t("No recognized role"))); select.value = ""; }
    select.setAttribute("aria-label", t("Role for {member}", { member: label }));
    const save = el("button", { type: "button", onclick: (event) => mutation(`${path}/members/${encodeURIComponent(member.user_id)}`,
      { role: select.value }, { method: "PATCH", button: event.currentTarget }), "aria-label": t("Save role for {member}", { member: label }) }, t("Save role"));
    save.disabled = !select.value;
    select.addEventListener("change", () => { save.disabled = !select.value; });
    const remove = el("button", { type: "button", onclick: (event) => mutation(`${path}/members/${encodeURIComponent(member.user_id)}`,
      undefined, { method: "DELETE", button: event.currentTarget }), "aria-label": t("Remove {member}", { member: label }) }, t("Remove member"));
    roster.append(el("li", {}, el("span", {}, label), select, save, remove));
  }
  const pending = el("ul", { class: "account-list", "aria-label": t("Invitations") });
  for (const invitation of invitations) pending.append(el("li", {}, el("span", {}, invitation.email,
    ` · ${t(invitation.role)} · ${t(invitation.status)}`), invitation.status === "pending" ? el("button", { type: "button",
      "aria-label": t("Cancel invitation for {email}", { email: invitation.email }), onclick: (event) => mutation(
        `${path}/invitations/${encodeURIComponent(invitation.invitation_id)}`, undefined, { method: "DELETE", button: event.currentTarget }) }, t("Cancel invitation")) : null));
  details.append(el("h2", {}, t("Invite a member")), form, el("h2", {}, t("Members")), roster,
    el("h2", {}, t("Invitations")), pending);
  return details;
}

function createOrganization() {
  const name = el("input", { id: "organization-name", required: "", maxlength: "100", autocomplete: "organization" });
  const button = el("button", { type: "submit" }, t("Create organization"));
  const form = localizeForm(el("form", { onsubmit: (event) => {
    event.preventDefault(); if (form.reportValidity()) mutation("/api/organizations", { name: name.value.trim() }, { button });
  } }, el("label", { for: "organization-name" }, t("Organization name")), name, button));
  return el("details", { class: "account-management" }, el("summary", {}, t("Create organization")), form);
}
channel?.addEventListener("message", () => {
  logoutSession = null;
  if (location.pathname.endsWith("/invitations/accept")) { session(true).catch(() => invalidateSession("unavailable")); return; }
  invalidateSession("identity"); location.replace(localePath("/"));
});

function loginLink(label, screenHint) {
  const path = localePath(location.pathname);
  const returnTo = /^\/(zh|ja|en)(?:\/studio|\/p\/[a-z0-9][a-z0-9_-]{0,79}|\/invitations\/accept)?$/.test(path) ? path : localePath("/");
  return el("a", { href: "/api/auth/login?" + new URLSearchParams({ return_to: returnTo,
    screen_hint: screenHint }), class: "chip", ...(location.pathname.endsWith("/invitations/accept") ? { target: "_blank", rel: "noopener noreferrer" } : {}) }, t(label));
}

function render(freshMembers = false) {
  const scope = authScope(me);
  const fields = !resetInputs && scope && scope === renderScope
    ? [...panel.querySelectorAll("input[id], select[id]")].map((node) => [node.id, node.value]) : [];
  const focused = panel.contains(document.activeElement) ? document.activeElement.id : null;
  resetInputs = false; renderScope = scope;
  panel.setAttribute("aria-label", t("Account"));
  if (me?.mode === "local") {
    panel.replaceChildren(el("span", { class: "account-identity" }, t("Local workspace")));
  } else if (me) {
    const workspace = el("select", { id: "account-workspace", "aria-label": t("Workspace") },
      el("option", { value: "personal" }, t("Personal workspace")));
    for (const organization of organizations) workspace.append(el("option", { value: organization.organization_id }, organization.name));
    workspace.value = me.workspace.kind === "personal" ? "personal" : me.workspace.owner_id;
    workspace.addEventListener("change", () => switchWorkspace(workspace.value));
    panel.replaceChildren(el("span", { class: "account-identity" }, me.user.name || me.user.email,
      el("span", { class: "account-email" }, me.user.email)),
    el("label", { for: "account-workspace" }, t("Workspace")), workspace,
    el("span", { class: "chip" }, t(me.role || "No recognized role")), el("button", { type: "button", id: "account-logout", onclick: (event) => logout(event.currentTarget) }, t("Sign out")));
    panel.append(el("span", { id: "current-workspace", class: "account-space" },
      t("Current workspace: {name}", { name: me.workspace.kind === "personal" ? t("Personal workspace") : me.workspace.name })));
    if (can("organization:create")) panel.append(createOrganization());
    if (can("organization:manage")) panel.append(management());
    if (!can("production:create")) panel.append(el("p", { class: "account-explanation" },
      t(can("resource:read") ? "You have read access to this workspace. Creating, approving and changing tasks is unavailable."
        : "This workspace grants no resource access.")));
  } else {
    panel.replaceChildren(el("span", {}, t("Sign in to use your workspace.")), loginLink("Sign in", "sign_in"),
      loginLink("Create account", "sign_up"), loginLink("Reset password or enterprise SSO", "sign_in"));
    if (logoutSession) panel.append(el("button", { type: "button", id: "account-logout", onclick: (event) => logout(event.currentTarget) }, t("Sign out")));
  }
  for (const [id, value] of fields) {
    const node = document.getElementById(id);
    if (node && id !== "account-workspace" && !(freshMembers === true && id.startsWith("member-role-"))) node.value = value;
  }
  if (focused) document.getElementById(focused)?.focus();
  if (announcement) panel.append(el("p", { id: "account-status", role: "status", "aria-live": "polite" }, t(announcement)));
}

session().then((value) => { me = value; if (value?.mode === "authenticated") logoutSession = value; render(); loadOrganizations(); }).catch((error) => {
  me = null; announcement = error.i18nKey || error.message; render();
});
window.addEventListener("languagechange", render);
window.addEventListener("accountchange", (event) => { clearProtectedView(); me = null; organizations = []; members = []; invitations = []; managementOpen = false;
  if (event.detail.reason === "identity") logoutSession = null;
  announcement = event.detail.reason === "unavailable" ? "The account service is unavailable. Please try again." : null; render(); });
window.addEventListener("accountready", () => { session().then((value) => {
  const changed = !me || authScope(me) !== authScope(value);
  me = value; if (value?.mode === "authenticated") logoutSession = value; if (changed) render();
}); });
window.addEventListener("permissionschange", () => { if (!can("organization:manage")) { members = []; invitations = []; managementOpen = false; } render(); });
window.addEventListener("focus", () => session(true).catch(() => invalidateSession("unavailable")));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) session(true).catch(() => invalidateSession("unavailable"));
});
const authorityCheck = setInterval(() => {
  if (!document.hidden && me?.mode === "authenticated") session(true).catch(() => invalidateSession("unavailable"));
}, 15000);
window.addEventListener("pagehide", () => { clearInterval(authorityCheck); channel?.close(); });
