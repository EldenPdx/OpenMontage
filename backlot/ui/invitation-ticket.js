// Run before the page's modules so a slow account read cannot retain a ticket in history.
(() => {
  const address = new URL(location.href);
  let ticket = new URLSearchParams(address.hash.slice(1)).get("ticket") || address.searchParams.get("ticket");
  if (ticket && !/^[A-Za-z0-9_-]{43,128}$/.test(ticket)) ticket = null;
  history.replaceState(null, "", address.pathname);
  window.takeInvitationTicket = () => { const value = ticket; ticket = null; return value; };
  setTimeout(() => { ticket = null; }, 10 * 60 * 1000);
})();
