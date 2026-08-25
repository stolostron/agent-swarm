(function () {
  function filterOfficeNav(input) {
    var q = (input.value || "").trim().toLowerCase();
    var root = input.closest(".office-nav-menu__body") || input.closest(".office-nav-menu__panel");
    if (!root) return;
    root.querySelectorAll(".office-nav-menu__item").forEach(function (el) {
      var name = el.getAttribute("data-name") || "";
      el.classList.toggle("is-hidden", q !== "" && name.indexOf(q) === -1);
    });
  }

  document.addEventListener("input", function (event) {
    var input = event.target;
    if (!input || !input.classList || !input.classList.contains("office-nav-menu__search-input")) {
      return;
    }
    filterOfficeNav(input);
  });

  // Close Office / Settings menus when clicking outside.
  document.addEventListener("click", function (event) {
    document.querySelectorAll("details.office-nav-menu[open], details.ws-settings-menu[open]").forEach(function (menu) {
      if (!menu.contains(event.target)) {
        menu.removeAttribute("open");
      }
    });
  });
})();
