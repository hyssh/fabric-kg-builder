// fabric-kg-builder landing page — minimal vanilla JS (no dependencies)

// Mobile nav toggle
const navToggle = document.getElementById("navToggle");
const navLinks = document.getElementById("navLinks");
if (navToggle && navLinks) {
  navToggle.addEventListener("click", () => navLinks.classList.toggle("is-open"));
  navLinks.querySelectorAll("a").forEach((a) =>
    a.addEventListener("click", () => navLinks.classList.remove("is-open"))
  );
}

// Copy-to-clipboard for code blocks
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
