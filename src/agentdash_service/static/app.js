// AgentDash SPA shell — hash router that swaps the iframe source, plus a
// shared "обновлено" timestamp in the topbar.
// Hashes: #tokens (default), #projects, #sessions.

const VIEWS = {
  tokens:   { title: "Token Dashboard",   file: "tokens.html" },
  projects: { title: "Project Dashboard", file: "projects.html" },
  sessions: { title: "Session Dashboard", file: "sessions.html" },
};

const SHELL_REFRESH_MS = 60 * 1000;

function resolveView() {
  const hash = (location.hash || "").replace(/^#/, "");
  return Object.prototype.hasOwnProperty.call(VIEWS, hash) ? hash : "tokens";
}

function fmtMsk(iso) {
  return new Date(iso).toLocaleString("ru-RU", { timeZone: "Europe/Moscow" });
}

function setUpdated(text) {
  const node = document.getElementById("shell-updated");
  if (node) node.textContent = text;
}

async function refreshShellUpdated() {
  try {
    const r = await fetch("/api/v1/tokens/snapshot", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    if (data && data.now_msk) setUpdated("Обновлено: " + fmtMsk(data.now_msk));
  } catch (_) {
    // Leave the previous timestamp visible on transient failures.
  }
}

function render() {
  const view = resolveView();
  const meta = VIEWS[view];
  document.title = meta.title;
  const frame = document.getElementById("frame");
  if (frame.dataset.view !== view) {
    frame.dataset.view = view;
    frame.src = meta.file;
  }
  for (const a of document.querySelectorAll("#nav a")) {
    a.classList.toggle("active", a.dataset.view === view);
  }
}

window.addEventListener("hashchange", render);
window.addEventListener("DOMContentLoaded", () => {
  render();
  refreshShellUpdated();
  setInterval(refreshShellUpdated, SHELL_REFRESH_MS);
});
