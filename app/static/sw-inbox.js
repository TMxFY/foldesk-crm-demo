const MEDIA_CACHE = "foldesk-media-v1";
const MAX_MEDIA_ENTRIES = 300;

const AVATAR_PATH_RE = /^\/api\/dialogs\/\d+\/avatar$/;
const DIALOG_MEDIA_PATH_RE = /^\/api\/dialogs\/\d+\/messages\/\d+\/media$/;
const INBOX_MEDIA_PATH_RE = /^\/media\/inbox\/[^/]+$/;

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k.startsWith("foldesk-") && k !== MEDIA_CACHE)
            .map((k) => caches.delete(k)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

function isNeverCachedPath(pathname) {
  return (
    pathname === "/sw-inbox.js" ||
    pathname === "/api/inbox" ||
    pathname.startsWith("/api/inbox/") ||
    pathname.startsWith("/api/auth/") ||
    pathname === "/api/leads" ||
    pathname.startsWith("/api/leads/")
  );
}

async function putWithLru(cache, request, response) {
  await cache.delete(request);
  await cache.put(request, response);
  const keys = await cache.keys();
  while (keys.length > MAX_MEDIA_ENTRIES) {
    const oldest = keys.shift();
    if (!oldest) break;
    await cache.delete(oldest);
  }
}

async function handleAvatarStaleWhileRevalidate(event, request) {
  const cache = await caches.open(MEDIA_CACHE);
  const cached = await cache.match(request);

  const revalidate = fetch(request).then(async (netRes) => {
    if (netRes && netRes.ok && netRes.status === 200) {
      const cc = (netRes.headers.get("cache-control") || "").toLowerCase();
      if (!cc.includes("no-store")) {
        await putWithLru(cache, request, netRes.clone());
      }
    }
    return netRes;
  });

  if (cached) {
    event.waitUntil(revalidate.catch(() => {}));
    return cached;
  }
  return revalidate;
}

async function handleMediaCacheFirst(event, request) {
  const cache = await caches.open(MEDIA_CACHE);
  const cached = await cache.match(request);
  if (cached) {
    event.waitUntil(putWithLru(cache, request, cached.clone()).catch(() => {}));
    return cached;
  }

  const netRes = await fetch(request);
  if (netRes && netRes.ok && netRes.status === 200) {
    event.waitUntil(putWithLru(cache, request, netRes.clone()).catch(() => {}));
  }
  return netRes;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  if (request.headers.has("range")) return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (isNeverCachedPath(url.pathname)) return;

  if (AVATAR_PATH_RE.test(url.pathname)) {
    event.respondWith(handleAvatarStaleWhileRevalidate(event, request));
    return;
  }

  if (
    DIALOG_MEDIA_PATH_RE.test(url.pathname) ||
    INBOX_MEDIA_PATH_RE.test(url.pathname)
  ) {
    event.respondWith(handleMediaCacheFirst(event, request));
  }
});
