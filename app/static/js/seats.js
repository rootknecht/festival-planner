/* Drag and drop for car seats.

   Progressive enhancement only: every action here also exists as a plain
   form with a button, which is what phones, screen readers and anyone with
   JavaScript off will use. */
(function () {
  "use strict";

  function post(url, fields) {
    var form = document.createElement("form");
    form.method = "post";
    form.action = url;
    Object.keys(fields).forEach(function (name) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = fields[name];
      form.appendChild(input);
    });
    document.body.appendChild(form);
    form.submit();
  }

  var token = document.querySelector('meta[name="csrf-token"]');
  var csrf = token ? token.getAttribute("content") : "";

  document.querySelectorAll("[data-participant-id]").forEach(function (chip) {
    chip.setAttribute("draggable", "true");
    chip.addEventListener("dragstart", function (event) {
      event.dataTransfer.setData("text/plain", chip.getAttribute("data-participant-id"));
      event.dataTransfer.effectAllowed = "move";
      chip.classList.add("is-dragging");
    });
    chip.addEventListener("dragend", function () {
      chip.classList.remove("is-dragging");
    });
  });

  document.querySelectorAll("[data-drop-car]").forEach(function (zone) {
    zone.addEventListener("dragover", function (event) {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      zone.classList.add("is-over");
    });
    zone.addEventListener("dragleave", function () {
      zone.classList.remove("is-over");
    });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      zone.classList.remove("is-over");
      var participantId = event.dataTransfer.getData("text/plain");
      if (!participantId) return;
      post(zone.getAttribute("data-drop-car"), {
        participant_id: participantId,
        csrf_token: csrf
      });
    });
  });
})();
