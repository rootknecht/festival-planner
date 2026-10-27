/* Live channel: appends incoming chat messages and reloads a page whose
   shared data changed elsewhere. Without JavaScript everything still works —
   the page is simply as fresh as the last reload. */
(function () {
  "use strict";

  var host = document.querySelector("[data-ws]");
  if (!host || !("WebSocket" in window)) return;

  var path = host.getAttribute("data-ws");
  var protocol = location.protocol === "https:" ? "wss://" : "ws://";
  var socket;
  var retry = 1000;

  function escapeText(value) {
    var node = document.createElement("span");
    node.textContent = value;
    return node.innerHTML;
  }

  function appendMessage(event) {
    var list = document.getElementById("messages");
    if (!list || document.getElementById("m" + event.id)) return;
    var item = document.createElement("li");
    item.className = "message";
    item.id = "m" + event.id;
    // The body arrives already sanitised by the server; the author name is
    // escaped here anyway rather than trusted twice.
    item.innerHTML =
      '<div class="row row--between"><strong>' +
      escapeText(event.author) +
      "</strong></div>" +
      '<div class="message__body">' +
      event.html +
      "</div>";
    list.appendChild(item);
    item.scrollIntoView({ block: "nearest" });
  }

  function connect() {
    socket = new WebSocket(protocol + location.host + path);

    socket.addEventListener("open", function () {
      retry = 1000;
    });

    socket.addEventListener("message", function (event) {
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        return;
      }
      if (data.type === "message") {
        appendMessage(data);
      } else if (data.type === "changed" && host.getAttribute("data-tool") === data.tool) {
        location.reload();
      }
    });

    socket.addEventListener("close", function () {
      // Reconnect with a growing delay so a restarted server does not get
      // hammered by every open tab at once.
      setTimeout(connect, Math.min(retry *= 2, 30000));
    });
  }

  connect();
})();
