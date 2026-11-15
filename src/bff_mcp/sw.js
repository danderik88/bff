// BFF service worker: only shows push alerts and opens the app on tap. No caching.
self.addEventListener("activate", e => e.waitUntil(clients.claim()));
self.addEventListener("push", e => {
  let m = {};
  try { m = e.data.json(); } catch (x) { m = { title: "BFF", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(m.title || "BFF", { body: m.body || "", data: { url: m.url || "/app" }, tag: m.tag }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = new URL(e.notification.data.url, self.location.origin).href;
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) if (new URL(c.url).pathname === "/app")
      return c.navigate(url).then(w => (w || c).focus()).catch(() => clients.openWindow(url));
    return clients.openWindow(url);
  }));
});
