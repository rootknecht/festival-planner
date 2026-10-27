/* Service worker.

   Caches the static shell so the app opens without a network, and shows the
   push notifications the server sends. Pages themselves are always fetched
   fresh: a festival's data must never be served stale from a cache. */

var CACHE = "festival-shell-v1";
var SHELL = [
  "/offline",
  "/static/css/base.css",
  "/static/css/shell.css",
  "/static/css/themes/metal.css",
  "/static/css/themes/matrix.css",
  "/static/css/themes/light.css",
  "/static/js/htmx.min.js",
  "/static/icons/icon-192.png"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      return cache.addAll(SHELL);
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(
        names.filter(function (name) { return name !== CACHE; })
             .map(function (name) { return caches.delete(name); })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;

  var url = new URL(request.url);
  if (url.origin !== location.origin) return;

  if (url.pathname.indexOf("/static/") === 0) {
    event.respondWith(
      caches.match(request).then(function (hit) {
        return hit || fetch(request);
      })
    );
    return;
  }

  // Navigations: network first, offline page as the fallback.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(function () {
        return caches.match("/offline");
      })
    );
  }
});

self.addEventListener("push", function (event) {
  var data = { title: "Festival Planner", body: "", url: "/" };
  if (event.data) {
    try {
      data = Object.assign(data, event.data.json());
    } catch (e) {
      data.body = event.data.text();
    }
  }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
      data: { url: data.url },
      tag: data.url
    })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        if (list[i].url.indexOf(target) !== -1 && "focus" in list[i]) return list[i].focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
