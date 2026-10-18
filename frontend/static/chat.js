// The chat's two after-answer steps, as listeners rather than inline attributes: once an answer has
// arrived, the question box is cleared, and a clarifying question's reply form (now answered) goes.
(function () {
  "use strict";
  function afterAnswer(e) {
    var form = e.target, d = e.detail || {};
    if (!d.successful || !form || !form.classList) return;
    if (form.classList.contains("chat__form")) form.reset();
    else if (form.classList.contains("chat-exchange__reply")) form.remove();
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { afterAnswer: afterAnswer };
  else document.addEventListener("htmx:afterRequest", afterAnswer);
})();
