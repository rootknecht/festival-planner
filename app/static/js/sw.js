/* Service worker.

   Caches the static shell so the app opens without a network, and shows the
   push notifications the server sends. Pages themselves are always fetched
   fresh: a festival's data must never be served stale from a cache. */

/* BUMP THIS whenever the shell changes. "activate" deletes every cache whose
   name is not this one, so a bump is what purges stale entries from installs
   that are already out there. */
var CACHE = "festival-shell-v2";
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
    /* NETWORK FIRST, cache as the fallback.

       This was cache-first ("hit || fetch"), which meant a changed stylesheet
       could never reach an installed app: shell.css was answered from
       festival-shell-v1 for as long as that cache existed, and because a
       service worker also answers a hard reload, Ctrl+Shift+R could not break
       out of it either. A bring-list fix that lived in CSS therefore shipped
       to the server and never arrived on the phone.

       Offline still works — that is what the catch() is for — and a fresh copy
       replaces the cached one on every successful fetch, so the offline shell
       stays current instead of frozen at whatever was first installed. */
    event.respondWith(
      fetch(request)
        .then(function (response) {
          if (response && response.ok) {
            var copy = response.clone();
            caches.open(CACHE).then(function (cache) {
              cache.put(request, copy);
            });
          }
          return response;
        })
        .catch(function () {
          /* A cache miss resolves to undefined, and respondWith(undefined)
             fails the request outright rather than falling through to the
             network. Since the cache now fills only on successful fetches, a
             first visit on a flaky connection could hit exactly that. Answer
             with a real Response instead. */
          return caches.match(request).then(function (hit) {
            return hit || new Response("", { status: 504, statusText: "offline" });
          });
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
