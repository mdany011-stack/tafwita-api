const CACHE_NAME = "tafwita-v2";

const APP_SHELL = [
  "./",
  "./patient.html",
  "./cabinet.html",
  "./manifest.webmanifest",
  "./manifest-cabinet.webmanifest",
  "./icon-192.png",
  "./icon-512.png"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(APP_SHELL);
    })
  );

  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys
          .filter(function (key) {
            return key !== CACHE_NAME;
          })
          .map(function (key) {
            return caches.delete(key);
          })
      );
    })
  );

  self.clients.claim();
});

self.addEventListener("fetch", function (event) {
  const request = event.request;

  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);

  // Les appels API doivent toujours utiliser le réseau
  if (url.pathname.indexOf("/api/") === 0 || url.hostname === "tafwita-api.onrender.com") {
    event.respondWith(
      fetch(request).catch(function () {
        return new Response(
          JSON.stringify({
            offline: true,
            message: "Connexion Internet nécessaire pour accéder à la file."
          }),
          {
            headers: {
              "Content-Type": "application/json"
            }
          }
        );
      })
    );

    return;
  }

  event.respondWith(
    fetch(request)
      .then(function (response) {
        const copy = response.clone();

        caches.open(CACHE_NAME).then(function (cache) {
          cache.put(request, copy);
        });

        return response;
      })
      .catch(function () {
        return caches.match(request).then(function (cached) {
          const fallback = url.pathname.indexOf("cabinet") >= 0 ? "./cabinet.html" : "./patient.html";
          return cached || caches.match(fallback);
        });
      })
  );
});
