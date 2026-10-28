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

  // A Cancel button on every reveal form.
  //
  // Generated here rather than written into a dozen templates: it is the same
  // button every time, and a new form added next month gets one for free
  // instead of being the one that silently does not. Opening a form to look
  // at it and then wanting out is the common case, and on a phone the only
  // alternative was scrolling back to the summary line to collapse it.
  //
  // Forms with nothing to fill in are skipped — "I paid this" and the delete
  // crosses are single-action forms where a Cancel button would mean nothing.
  // Without JavaScript nothing is lost: the <details> still collapses on its
  // own summary, which is the same escape route.
  function labelForCancel() {
    // The page carries its own translation, so this works in German too
    // without shipping a second string table to the browser.
    var node = document.querySelector("[data-cancel-label]");
    return (node && node.getAttribute("data-cancel-label")) || "Cancel";
  }

  document.querySelectorAll("details.reveal form").forEach(function (form) {
    if (!form.querySelector("input:not([type=hidden]):not([type=submit]), select, textarea")) {
      return;
    }
    var submit = form.querySelector("button:not([type=button]):not([formaction])");
    if (!submit) return;

    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn btn--small";
    cancel.textContent = labelForCancel();
    cancel.addEventListener("click", function () {
      form.reset();
      var box = form.closest("details.reveal");
      if (box) box.open = false;
    });
    submit.insertAdjacentElement("afterend", cancel);
  });
})();

// "Add option" for the poll form: clone the last field rather than building
// markup here, so the name, classes and validation attributes can only ever
// match the server's expectations.
(function () {
  "use strict";
  document.querySelectorAll("[data-add-option]").forEach(function (button) {
    button.addEventListener("click", function () {
      var list = document.querySelector(button.getAttribute("data-add-option"));
      if (!list) return;
      var fields = list.querySelectorAll("input[name=options]");
      if (fields.length >= 20) return;            // a poll, not a ballot paper
      var copy = fields[fields.length - 1].cloneNode(true);
      copy.value = "";
      copy.required = false;                       // extra options stay optional
      copy.placeholder = "";
      list.appendChild(copy);
      copy.focus();
    });
  });
})();

// Show only the split panel that matches the chosen mode. Without JavaScript
// every panel stays visible and the form still works — the server decides
// which inputs it reads from the mode, so an unused panel is harmless.
(function () {
  "use strict";
  var select = document.querySelector("[data-split-mode]");
  if (!select) return;
  var panels = document.querySelectorAll("[data-split-panel]");
  function sync() {
    panels.forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-split-panel") !== select.value;
    });
  }
  select.addEventListener("change", sync);
  sync();
})();
