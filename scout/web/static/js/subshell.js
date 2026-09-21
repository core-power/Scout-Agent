/* =============================================================================
 * subshell.js —— 子页面统一外壳（/plugins /monitor /usage /notify …）
 *
 * 解决的问题：
 *   1. 子页面是整页跳转，桌面端（pywebview）没有浏览器后退键；
 *      plugin-builder.html / plugin-config.html 连返回入口都没有 —— 进去出不来。
 *   2. 子页面没有主题开关，也没有和主界面一致的导航语义（孤岛感强）。
 *
 * 注入内容：返回主对话（缺失时才补）、页面标题、主题切换、语言切换。
 * 幂等：window.__wbSubShell 守卫，重复引用安全。
 * ========================================================================== */
(function () {
    'use strict';
    if (window.__wbSubShell) return;
    window.__wbSubShell = 1;

    // 先插样式，避免中途修改 <html class="dark"> 导致其他页面规则丢失
    var css = document.createElement('style');
    css.textContent = [
        '#wb-sub-bar{display:flex; align-items:center; gap:10px; flex-wrap:wrap;',
        '  padding:8px 14px; border-bottom:0.5px solid rgb(var(--c-line));',
        '  background:rgb(var(--c-surface-1)); position:sticky; top:0; z-index:60}',
        '#wb-sub-bar .wb-sub-home{display:inline-flex; align-items:center; gap:6px;',
        '  padding:5px 9px; border-radius:8px; text-decoration:none;',
        '  font-size:12px; color:rgb(var(--c-ink-2))}',
        '#wb-sub-bar .wb-sub-home:hover{background:rgb(var(--c-surface-3)); color:rgb(var(--c-ink-1))}',
        '#wb-sub-bar .wb-sub-home > svg{width:14px; height:14px}',
        '#wb-sub-bar .wb-sub-title{font-size:12.5px; font-weight:500; color:rgb(var(--c-ink-1))}',
        '#wb-sub-bar .wb-sub-spacer{flex:1}',
        '#wb-sub-bar .wb-sub-btn{display:inline-flex; align-items:center; justify-content:center;',
        '  width:28px; height:28px; border:none; cursor:pointer; border-radius:8px;',
        '  background:transparent; color:rgb(var(--c-ink-3))}',
        '#wb-sub-bar .wb-sub-btn:hover{background:rgb(var(--c-surface-3)); color:rgb(var(--c-ink-1))}',
        '#wb-sub-bar .wb-sub-btn > svg{width:15px; height:15px}',
        '#wb-sub-bar .wb-sub-lang{width:auto; padding:0 8px; height:28px; font-size:11.5px; font-weight:500}'
    ].join('\n');
    (document.head || document.documentElement).appendChild(css);

    var SUN = 'M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z';
    var MOON = 'M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z';

    function svg(d, w) {
        return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' + (w || 2) +
            '" stroke-linecap="round" stroke-linejoin="round"><path d="' + d + '"/></svg>';
    }
    function title() {
        var t = (document.title || '').split('·')[0].trim();
        return t || 'Scout';
    }

    function ensureBar() {
        var bar = document.getElementById('wb-sub-bar');
        if (bar) return bar;
        bar = document.createElement('div');
        bar.id = 'wb-sub-bar';
        bar.innerHTML =
            '<a class="wb-sub-home" href="/" title="返回对话 (Alt + ←)">' +
                svg('M15 19l-7-7 7-7') + '<span>返回对话</span>' +
            '</a>' +
            '<span class="wb-sub-title"></span>' +
            '<span class="wb-sub-spacer"></span>' +
            '<button class="wb-sub-btn" id="wb-sub-theme" title="切换主题"></button>' +
            '<button class="wb-sub-btn wb-sub-lang" id="wb-sub-lang" title="切换界面语言">中 / EN</button>';
        if (document.body) document.body.insertBefore(bar, document.body.firstChild);
        else document.documentElement.appendChild(bar);
        return bar;
    }

    function paintThemeIcon(btn) {
        var dark = document.documentElement.classList.contains('dark');
        btn.innerHTML = svg(dark ? MOON : SUN);
        btn.setAttribute('aria-label', dark ? '切换到浅色' : '切换到深色');
    }

    function wire() {
        var bar = ensureBar();
        var t = bar.querySelector('.wb-sub-title');
        if (t) t.textContent = title();

        var themeBtn = bar.querySelector('#wb-sub-theme');
        if (themeBtn) {
            paintThemeIcon(themeBtn);
            themeBtn.addEventListener('click', function () {
                var dark = document.documentElement.classList.toggle('dark');
                try { localStorage.setItem('theme', dark ? 'dark' : 'light'); } catch (e) {}
                paintThemeIcon(themeBtn);
            });
        }

        var langBtn = bar.querySelector('#wb-sub-lang');
        if (langBtn) {
            langBtn.addEventListener('click', function () {
                if (typeof window.toggleUILang === 'function') { window.toggleUILang(); return; }
                try {
                    var cur = localStorage.getItem('scout_ui_lang') === 'en' ? 'en' : 'zh';
                    localStorage.setItem('scout_ui_lang', cur === 'en' ? 'zh' : 'en');
                } catch (e) {}
                location.reload();
            });
        }

        document.addEventListener('keydown', function (e) {
            if (e.altKey && e.key === 'ArrowLeft') { e.preventDefault(); location.href = '/'; }
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
    else wire();
})();
