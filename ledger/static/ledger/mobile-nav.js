(() => {
  const toggle = document.querySelector(".mobile-menu-toggle");
  if (!toggle) return;
  const sidebar = toggle.closest(".sidebar");
  sidebar.classList.add("has-mobile-menu");

  function setOpen(open) {
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Fechar menu" : "Abrir menu");
  }

  toggle.addEventListener("click", () => setOpen(toggle.getAttribute("aria-expanded") !== "true"));
  sidebar.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true" && toggle.offsetWidth) {
      setOpen(false);
      toggle.focus();
    }
  });
  matchMedia("(max-width: 680px)").addEventListener("change", () => setOpen(false));
})();
