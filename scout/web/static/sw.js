/* Scout Agent Service Worker — 提供 PWA 安装能力与离线秒开.
 *
 * 缓存策略（安全优先）:
 *   - /api /ws /v1 /a2a 一律不缓存（含对话内容、密钥、配置，防泄漏）
 *   - /static/vendor/* 与 icons/manifest: cache-first（可长期缓存）
 *   - HTML 页面（/chat /usage ...）: network-first，网络失败回退缓存（离线可用）
 */
'use strict';

const CACHE_NAME = 'scout-web-v1';
const SHELL_CACHE = 'scout-shell-v1';

const SHELL_ASSETS = [
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/maskable-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // 只处理 GET，POST 等直接透传

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // 跨域（CDN/API）不拦截

  const path = url.pathname;

  // 敏感路径一律走网络，绝不缓存
  if (path.startsWith('/api') || path.startsWith('/ws') || path.startsWith('/v1') || path.startsWith('/a2a')) {
    return;
  }

  // 静态 vendor 资源：cache-first（体积稳定，提升加载速度）
  if (path.startsWith('/static/vendor/')) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE_NAME).then((c) => c.put(req, copy));
        return res;
      }))
    );
    return;
  }

  // shell 资源（manifest/图标）：cache-first
  if (path === '/static/manifest.json' || path.startsWith('/static/icons/')) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req))
    );
    return;
  }

  // HTML 页面：network-first，失败回退缓存（离线可打开已访问页面）
  if (path === '/' || path === '/chat' || path === '/usage' || path === '/plugins' ||
      path === '/monitor' || path === '/automation' || path === '/observe' ||
      path === '/notify' || path === '/watcher' || path === '/webhooks' || path === '/events') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then((hit) => hit || caches.match('/chat')))
    );
    return;
  }

  // 其余静态资源（css/js 内联已打包进 HTML，此处兜底）：stale-while-revalidate
  if (path.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((hit) => {
        const network = fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, copy));
          return res;
        }).catch(() => hit);
        return hit || network;
      })
    );
  }
});
