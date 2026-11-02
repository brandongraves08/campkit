/* Progressive enhancement only: every workflow works without this file.
   Destructive forms declare their prompt in data-confirm so templates stay
   free of inline handlers. */
(() => {
  "use strict";

  const forms = document.querySelectorAll("form[data-confirm]");
  forms.forEach((form) => {
    form.addEventListener("submit", (event) => {
      const message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) {
        event.preventDefault();
      }
    });
  });

  document.querySelectorAll("[data-dismiss]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.querySelector(button.getAttribute("data-dismiss"));
      if (target) {
        target.hidden = true;
      }
    });
  });

  const main = document.getElementById("main");
  if (main && window.location.hash === "#main") {
    main.focus();
  }
})();
