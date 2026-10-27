/* Registers the service worker, offers the install button, and manages the
   push subscription. Everything here is optional: with none of it, the app
   still works and the in-app red dots still tell you what is open. */
(function () {
  "use strict";

  if (!("serviceWorker" in navigator)) return;

  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {
    /* a failed registration must not break the page */
  });

  /* ---- install button ------------------------------------------------- */
  var deferredPrompt = null;
  var installButton = document.querySelector("[data-install-app]");

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredPrompt = event;
    if (installButton) installButton.hidden = false;
  });

  if (installButton) {
    installButton.addEventListener("click", function (event) {
      event.preventDefault();
      if (!deferredPrompt) return;
      deferredPrompt.prompt();
      deferredPrompt = null;
      installButton.hidden = true;
    });
  }

  /* ---- push ------------------------------------------------------------ */
  function urlBase64ToUint8Array(base64String) {
    var padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    var base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    var raw = atob(base64);
    var output = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) output[i] = raw.charCodeAt(i);
    return output;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": (document.querySelector('meta[name="csrf-token"]') || {}).content || ""
      },
      body: JSON.stringify(body)
    });
  }

  var pushToggle = document.querySelector("[data-push-toggle]");
  if (!pushToggle || !("PushManager" in window)) return;

  fetch("/push/key")
    .then(function (response) { return response.json(); })
    .then(function (data) {
      if (!data.enabled) {
        pushToggle.textContent = pushToggle.getAttribute("data-label-unavailable");
        pushToggle.disabled = true;
        return;
      }
      pushToggle.hidden = false;
      pushToggle.addEventListener("click", function () {
        Notification.requestPermission().then(function (permission) {
          if (permission !== "granted") return;
          navigator.serviceWorker.ready.then(function (registration) {
            registration.pushManager
              .subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(data.key)
              })
              .then(function (subscription) {
                return post("/push/subscribe", subscription.toJSON());
              })
              .then(function () {
                pushToggle.textContent = pushToggle.getAttribute("data-label-on");
                pushToggle.disabled = true;
              });
          });
        });
      });
    })
    .catch(function () { /* push stays off */ });
})();
