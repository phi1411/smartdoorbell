const incoming = document.querySelector("[data-intercom-incoming]");
let rendered = incoming.innerHTML.trim();
let polling = false;

async function refreshIncoming() {
  if (polling || document.hidden || incoming.contains(document.activeElement)) return;
  polling = true;
  try {
    const response = await fetch(incoming.dataset.intercomIncoming, {
      redirect: "error",
      signal: AbortSignal.timeout(4500),
    });
    if (!response.ok || !response.headers.get("Content-Type")?.startsWith("text/html")) return;
    const updated = (await response.text()).trim();
    if (updated !== rendered && !incoming.contains(document.activeElement)) {
      incoming.innerHTML = updated;
      rendered = updated;
    }
  } catch {
    // Keep the current controls available during a brief network interruption.
  } finally {
    polling = false;
  }
}

setInterval(refreshIncoming, 2500);
document.addEventListener("visibilitychange", refreshIncoming);
