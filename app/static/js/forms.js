/* Small form helpers. Everything here is progressive enhancement: without
   JavaScript the checkboxes are still checkable by hand and every form still
   submits, which matters because this app is used on phones in fields with no
   signal. */
(function () {
  "use strict";

  // "Select all" / "Select none" for a checkbox list, used by the meeting-point
  // car selection: an admin wants every car by default and then unticks the
  // two that arrive a day later.
  function bindBulk(attribute, checked) {
    document.querySelectorAll("[" + attribute + "]").forEach(function (button) {
      button.addEventListener("click", function () {
        var list = document.querySelector(button.getAttribute(attribute));
        if (!list) return;
        list.querySelectorAll("input[type=checkbox]").forEach(function (box) {
          box.checked = checked;
        });
      });
    });
  }

  bindBulk("data-select-all", true);
  bindBulk("data-select-none", false);

  // Close any other open <details class="reveal"> when one opens, so a page
  // full of collapsed forms cannot turn into a page full of open ones.
  document.querySelectorAll("details.reveal").forEach(function (box) {
    box.addEventListener("toggle", function () {
      if (!box.open) return;
      document.querySelectorAll("details.reveal[open]").forEach(function (other) {
        if (other !== box && !other.contains(box) && !box.contains(other)) {
          other.open = false;
        }
      });
      var field = box.querySelector("input:not([type=hidden]), select, textarea");
      if (field) field.focus();
    });
  });
})();
