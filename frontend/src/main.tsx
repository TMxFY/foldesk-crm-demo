import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
import "./styles/inbox-v2.css";
import { InboxApp } from "./components/InboxApp";

function mountInboxV2() {
  const host =
    document.getElementById("inbox-v2-host") ||
    (document.querySelector('.messenger-grid[data-sheet="inbox"]') as HTMLElement | null);

  if (!host) return;
  if (!host.id) host.id = "inbox-v2-host";

  // Signal to app.js immediately that React Inbox v2 owns #s04 / #s05 / #s06
  window.__INBOX_V2_ACTIVE__ = true;
  host.dataset.inboxV2 = "mounted";

  // Remove only the SSR fallback inbox sections while keeping .inspector-col > #s09 alive for Leads card drawer
  for (const legacySel of ["#s04", "#s05", "#s06", "#tg-web-a-panel"]) {
    const node = host.querySelector(legacySel);
    if (node && !node.closest("#inbox-v2-root")) {
      node.remove();
    }
  }

  let mountPoint = document.getElementById("inbox-v2-root");
  if (!mountPoint) {
    mountPoint = document.createElement("div");
    mountPoint.id = "inbox-v2-root";
    mountPoint.className = "inbox-v2-root";
    host.prepend(mountPoint);
  }

  createRoot(mountPoint).render(
    <StrictMode>
      <InboxApp hostEl={host} />
    </StrictMode>,
  );
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mountInboxV2, { once: true });
} else {
  mountInboxV2();
}
