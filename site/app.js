// fabric-kg-builder landing page — minimal vanilla JS (no dependencies)

// Mobile nav toggle
const navToggle = document.getElementById("navToggle");
const navLinks = document.getElementById("navLinks");
if (navToggle && navLinks) {
  navToggle.addEventListener("click", () => navLinks.classList.toggle("is-open"));
}

// ---------- Tabbed navigation ----------
const panels = document.querySelectorAll(".tab-panel");
const TAB_NAMES = new Set(Array.from(panels).map((p) => p.getAttribute("data-panel")));

function activateTab(name, push) {
  if (!TAB_NAMES.has(name)) name = "overview";

  panels.forEach((p) =>
    p.classList.toggle("is-active", p.getAttribute("data-panel") === name)
  );

  // Reflect active state on every control that targets a tab
  document.querySelectorAll("[data-tab]").forEach((el) =>
    el.classList.toggle("is-active", el.getAttribute("data-tab") === name)
  );

  // Close the mobile menu after a choice
  if (navLinks) navLinks.classList.remove("is-open");

  // Keep the URL shareable without forcing a jump
  if (push !== false) history.replaceState(null, "", "#" + name);

  // Bring the new panel into view (below the sticky nav + tab bar)
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// Wire up every tab trigger (nav links, hero buttons, tab bar, footer links)
document.querySelectorAll("[data-tab]").forEach((el) => {
  el.addEventListener("click", (e) => {
    e.preventDefault();
    activateTab(el.getAttribute("data-tab"));
  });
});

// Honor an initial hash (deep links), default to the first tab
activateTab((location.hash || "#overview").slice(1), false);

// React to back/forward navigation
window.addEventListener("hashchange", () =>
  activateTab((location.hash || "#overview").slice(1), false)
);

// ---------- Copy-to-clipboard for code blocks ----------
document.querySelectorAll(".code__copy").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const block = btn.closest(".code");
    const code = block ? block.querySelector("code") : null;
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code.innerText);
      const original = btn.textContent;
      btn.textContent = "Copied!";
      setTimeout(() => (btn.textContent = original), 1500);
    } catch {
      btn.textContent = "Press Ctrl+C";
      setTimeout(() => (btn.textContent = "Copy"), 1500);
    }
  });
});
