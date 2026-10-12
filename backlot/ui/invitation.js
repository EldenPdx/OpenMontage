import { el } from "/ui/lib.js";
import { initI18n, languageTabs, t, localePath } from "/ui/i18n.js";
import { session, authScope, activeScope, writeJSON } from "/ui/session.js";
import "/ui/account.js";

let ticket = window.takeInvitationTicket?.();
delete window.takeInvitationTicket;
let me, boundScope, key = crypto.randomUUID(), busy = false, message;
const actions = document.getElementById("invitation-actions");
initI18n(); document.querySelector(".slate").append(languageTabs());
const expiry = setTimeout(() => { ticket = null; message = "Open the original invitation link again."; render(); }, 10 * 60 * 1000);

async function refreshIdentity() {
  try { me = await session(true); render(); }
  catch (error) { message = error.i18nKey || error.message; render(); }
}

async function accept() {
  if (!ticket || busy || !me) return;
  boundScope = authScope(me); busy = true; render();
  try {
    await writeJSON("/api/invitations/accept", { ticket }, { transientKey: key });
    if (!activeScope(boundScope)) return;
    ticket = null;
    message = "Invitation accepted. Select the organization in your workspace menu.";
    actions.replaceChildren(el("a", { class: "chip", href: localePath("/") }, t("Project library")));
    document.getElementById("invitation-status").textContent = t(message);
  } catch (error) { message = error.i18nKey || error.message; }
  finally { busy = false; if (ticket) render(); }
}

function render() {
  document.getElementById("invitation-heading").textContent = t("Accept organization invitation");
  document.getElementById("invitation-status").textContent = t(message || (!ticket ? "Open the original invitation link again."
    : me ? "Accept this invitation with the verified account shown above." : "Sign in in a new window, then return here to accept the invitation."));
  actions.replaceChildren();
  if (!ticket) { actions.append(el("a", { class: "chip", href: localePath("/") }, t("Project library"))); return; }
  if (!me) {
    actions.append(el("a", { class: "chip", target: "_blank", rel: "noopener noreferrer",
      href: "/api/auth/login?" + new URLSearchParams({ return_to: localePath("/"), screen_hint: "sign_in" }) }, t("Sign in in a new window")),
    el("button", { type: "button", onclick: refreshIdentity }, t("I have signed in")));
  } else {
    const button = el("button", { type: "button", onclick: accept }, t("Accept invitation"));
    button.disabled = busy || !me.user?.email_verified; actions.append(button);
  }
}

window.addEventListener("accountchange", () => { if (boundScope) { ticket = null; message = "Open the original invitation link again."; } me = null; render(); });
window.addEventListener("accountready", () => { session().then((value) => { me = value; render(); }); });
window.addEventListener("languagechange", render);
window.addEventListener("pagehide", () => { clearTimeout(expiry); ticket = null; key = null; });
refreshIdentity();
