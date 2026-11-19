/*
 * Founder OS service worker.
 *
 * It does two things: shows a notification when the server pushes one, and opens the app on
 * the view that notification is about when it is tapped. It caches nothing: the app always
 * loads from the network, so a deploy is live at once.
 *
 * Scope: the folder this file is served from (/app/), the same as the web app manifest.
 */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// Only ever open the app itself: a notification cannot send the browser anywhere else.
function appUrl(path) {
  const scope = new URL(self.registration.scope);
  try {
    const url = new URL(path || scope.pathname, scope.origin);
    if (url.origin === scope.origin && url.pathname.startsWith(scope.pathname)) return url.href;
  } catch (_) {}
  return scope.href;
}

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; }
  catch (_) { data = { body: event.data ? event.data.text() : "" }; }
  const options = {
    body: data.body || "",
    icon: "icons/icon-192.png",
    badge: "icons/favicon-32.png",
    data: { url: appUrl(data.url) },
  };
  if (data.tag) { options.tag = String(data.tag); options.renotify = true; }
  event.waitUntil(self.registration.showNotification(data.title || "Founder OS", options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = appUrl(event.notification.data && event.notification.data.url);
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const scope = new URL(self.registration.scope).pathname;
    for (const client of windows) {
      if (new URL(client.url).pathname.startsWith(scope)) {
        await client.focus();
        client.postMessage({ type: "founder-os-go", url });      // the open app moves to the view itself
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
