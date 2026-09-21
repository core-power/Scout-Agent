/* Scout Agent Service Worker — 提供 PWA 安装能力与离线秒开.
 *
 * 缓存策略（安全优先）:
 *   - /api /ws /v1 /a2a 一律不缓存（含对话内容、密钥、配置，防泄漏）
 *   - /static/vendor/*、icons、manifest: cache-first（体积稳定，可长期缓存）
 *   - HTML 页面、应用自身的 css / js: network-first，失败回退缓存（离线可用）
 *   - /static/ 其余: stale-while-revalidate
 *
 * 发版时把 CACHE_VERSION 加一。
 * 浏览器只在 sw.js 字节发生变化时才去更新 SW 本身，所以「改了前端但没改这里」
 * 会出现新页面配旧缓存的情况 —— 这是这个文件唯一需要人工记住的事。
 */
'use strict';

const CACHE_VERSION = 'v2';
const CACHE_NAME = `scout-web-${CACHE_VERSION}`;      // 运行时缓存：页面 / css / js / vendor
const SHELL_CACHE = `scout-shell-${CACHE_VERSION}`;   // 预缓存：图标 / manifest
const CACHE_PREFIX = 'scout-';                        // 清理旧缓存时只认自己家前缀

const SHELL_ASSETS = [
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/maskable-512.png',
];

/* 只有真正成功的响应才值得进缓存。
   原实现无条件 cache.put —— 一个 500 或者 404 会被缓存下来，
   之后断网打开看到的就是那张错误页。 */
function cacheIfOk(cacheName, request, response) {
  if (response && response.ok && response.type === 'basic') {
    // clone 必须在返回之前做：响应一旦交给页面，body 就被消费掉了，
    // 之后再 clone 会抛 InvalidStateError。
    const copy = response.clone();
    caches.open(cacheName).then((c) => c.put(request, copy)).catch(() => {});
  }
  return response;
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            /* 只删「自己前缀 + 不是当前版本」的缓存。
               原实现写的是 keys.filter(k => k !== SHELL_CACHE)，有两个问题：
                 1. 它会把 CACHE_NAME 也删掉 —— 而 CACHE_NAME 正是下面 fetch
                    里一直在写的那个。等于每次 SW 激活都把运行时缓存清空，
                    离线能力形同虚设。
                 2. 它删掉同源下所有其它缓存，不区分归属。 */
            .filter((k) => k.startsWith(CACHE_PREFIX) && k !== SHELL_CACHE && k !== CACHE_NAME)
            .map((k) => caches.delete(k))
        )
      )
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

  // 静态 vendor 资源：cache-first（第三方库，体积大且极少变动）
  if (path.startsWith('/static/vendor/')) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => cacheIfOk(CACHE_NAME, req, res)))
    );
    return;
  }

  // manifest / 图标：cache-first。注意 manifest 有两个入口 ——
  // 根路径 /manifest.json（页面里 link 引用的）和 /static/manifest.json（预缓存用的）。
  if (path === '/manifest.json' || path === '/static/manifest.json' || path.startsWith('/static/icons/')) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
    return;
  }

  /* 应用自身的样式与脚本：network-first。
     原来是 stale-while-revalidate，结果是 —— 构建出新 app.css 后第一次打开
     仍然是旧样式，要再刷一次才生效，非常容易被当成「改了没生效」。
     这类文件很小（app.css 约 80KB，且服务在本地回环上），走网络没有代价。 */
  if (path.startsWith('/static/css/') || path === '/static/theme.js' || path === '/static/i18n.js') {
    event.respondWith(
      fetch(req)
        .then((res) => cacheIfOk(CACHE_NAME, req, res))
        .catch(() => caches.match(req))
    );
    return;
  }

  /* HTML 页面：network-first，失败回退缓存（离线可打开已访问页面）。
     页面路由都无扩展名（/chat /usage /monitor /plugin-builder …），
     原实现是逐个 URL 硬枚举，新增页面必须回来改这里才能拿到离线能力
     —— plugin-builder / plugin-config 就是这么被漏掉的。 */
  const isPage = !path.slice(1).includes('.');
  if (isPage) {
    event.respondWith(
      fetch(req)
        .then((res) => cacheIfOk(CACHE_NAME, req, res))
        .catch(() =>
          caches.match(req).then((hit) => hit || caches.match('/chat') || caches.match('/'))
        )
    );
    return;
  }

  // 其余静态资源：stale-while-revalidate
  if (path.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((hit) => {
        const network = fetch(req)
          .then((res) => cacheIfOk(CACHE_NAME, req, res))
          .catch(() => hit);
        return hit || network;
      })
    );
  }
});
