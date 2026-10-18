// chat.js — first-party, no framework. What the chat's htmx forms do once a request has worked,
// so no template needs an inline handler: the question box is cleared after an answer arrives,
// and a clarifying question's reply form is removed once the reply has been answered. Without
// scripts the forms post normally and the page comes back whole, so nothing here is needed.
(function () {
  "use strict";
  document.addEventListener("htmx:afterRequest", function (event) {
    var form = event.target;
    if (!event.detail || !event.detail.successful || !form || typeof form.matches !== "function") return;
    if (form.matches("form.chat__form")) {
      form.reset();
    } else if (form.matches("form.chat-exchange__reply")) {
      form.remove();
    }
  });
})();
