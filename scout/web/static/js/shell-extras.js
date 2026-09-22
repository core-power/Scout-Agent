/* =============================================================================
 * shell-extras.js —— scout 外壳交互增强层（零侵入）
 * 加载位置：index.html 最后一行脚本之后
 * 回滚：删掉这一行 <script> 即可
 *
 * 本层负责（均为纯增强，不覆盖业务逻辑）：
 *   1. 消息操作条常驻 + 键盘可达
 *   2. Cmd/Ctrl+K 命令面板（静态命令 + 会话模糊搜索）
 *   3. 输入框：↑/↓ 历史、按会话自动存草稿、字数/token 计数
 *   4. 会话置顶
 *   5. 设置弹窗：Esc 关闭、←/→ 切页、记住上次打开的页
 *   6. 欢迎屏：继续上次会话 + 快捷键提示
 *   7. 无障碍：选中文字时暂停自动滚动、答案完成后一次性播报
 * ========================================================================== */
(function () {
  'use strict';
  if (window.__wbShell) return;
  window.__wbShell = 1;

  function q(s, r) { return (r || document).querySelector(s); }
  function qa(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function debounce(fn, ms) {
    var t; return function () {
      var a = arguments, self = this; clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, a); }, ms);
    };
  }
  function safe(name) {
    return function () {
      try { if (typeof window[name] === 'function') return window[name].apply(null, arguments); }
      catch (e) {}
      return undefined;
    };
  }
  function icon(id) { return '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#' + id + '"/></svg>'; }
  // 走 i18n 词典的模板翻译：T('共 {n} 步', {n: 3})
  // 拼装出来的串（数字+中文）无法被 MutationObserver 整段匹配，必须显式翻译。
  function T(tpl, vars) {
    var out = tpl;
    try { if (typeof __t === 'function') out = __t(tpl); } catch (e) {}
    if (vars) {
      Object.keys(vars).forEach(function (k) {
        out = out.split('{' + k + '}').join(vars[k]);
      });
    }
    return out;
  }
  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) {} }

  /* ═══════════ 1. 消息操作条：常驻 + 可 Tab 聚焦 ═══════════ */
  function tagActs(root) {
    var boxes = qa('[class*="group-hover:opacity-100"]', root || document);
    for (var i = 0; i < boxes.length; i++) boxes[i].classList.add('wb-acts');
  }

  /* ═══════════ 2. 命令面板 ═══════════ */
  var TABS = ['model', 'agent', 'security', 'channels', 'tools', 'auth', 'version'];
  var COMMANDS = [
    { t: '新对话', k: '会话', i: 'i-message-square', run: safe('newChat') },
    { t: '在当前会话中查找…', k: '会话', i: 'i-search', run: fsOpen },
    { t: '导出当前会话（Markdown）', k: '会话', i: 'i-export', run: function () { exportSession('md'); } },
    { t: '导出当前会话（JSON）', k: '会话', i: 'i-export', run: function () { exportSession('json'); } },
    { t: '添加附件', k: '输入', i: 'i-upload', run: function () { var f = q('#file-input'); if (f) f.click(); } },
    { t: '打开产物面板', k: '面板', i: 'i-folder', kbd: 'Alt + F', run: fpOpen },
    { t: '打开记忆库', k: '面板', i: 'i-pin', run: function () { safe('openPanel')('memory'); } },
    { t: '打开知识库', k: '面板', i: 'i-bot', run: function () { safe('openPanel')('knowledge'); } },
    { t: '回到最新消息', k: '视图', i: 'i-download', run: safe('scrollToBottom'), args: [true] },
    { t: '折叠 / 展开侧栏', k: '视图', i: 'i-sidebar', kbd: modKey() + ' + B', run: function () { setSidebarCollapsed(!sbCollapsed()); } },
    { t: '切换主题（深 / 浅）', k: '视图', i: 'i-info', run: safe('toggleTheme') },
    { t: '快捷键与斜杠命令', k: '帮助', i: 'i-keyboard', run: showHelp },
    { t: '切换界面语言', k: '视图', i: 'i-globe', run: safe('toggleUILang') },
    { t: '设置 · 模型配置', k: '设置', i: 'i-cpu', run: safe('openSettings'), args: ['model'] },
    { t: '设置 · Agent 行为', k: '设置', i: 'i-terminal', run: safe('openSettings'), args: ['agent'] },
    { t: '设置 · 安全策略', k: '设置', i: 'i-warning', run: safe('openSettings'), args: ['security'] },
    { t: '设置 · 渠道管理', k: '设置', i: 'i-message-circle', run: safe('openSettings'), args: ['channels'] },
    { t: '设置 · 工具配置', k: '设置', i: 'i-code', run: safe('openSettings'), args: ['tools'] }
  ];

  var pal = null, palInput = null, palList = null, palItems = [], palIdx = 0, sessCache = [];

  function buildPalette() {
    if (pal) return pal;
    pal = document.createElement('div');
    pal.id = 'wb-palette';
    pal.innerHTML =
      '<div class="wb-scrim" data-wb-close="1"></div>' +
      '<div class="wb-card">' +
        '<input class="wb-input" type="text" placeholder="输入命令或搜索会话…  Esc 关闭" autocomplete="off">' +
        '<div class="wb-list"></div>' +
        '<div class="wb-foot">' +
          '<span>↑↓ 选择</span><span>Enter 执行</span><span>Esc 关闭</span>' +
          '<span style="margin-left:auto">Ctrl / ⌘ + K</span>' +
        '</div>' +
      '</div>';
    document.body.appendChild(pal);
    palInput = q('.wb-input', pal);
    palList = q('.wb-list', pal);

    pal.addEventListener('mousedown', function (e) {
      if (e.target.getAttribute('data-wb-close')) return closePalette();
    });
    palInput.addEventListener('input', function () { renderPalette(palInput.value); });
    palInput.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter') { e.preventDefault(); exec(); }
      else if (e.key === 'Escape') { e.preventDefault(); closePalette(); }
    });
    palList.addEventListener('click', function (e) {
      var it = e.target.closest ? e.target.closest('.wb-item') : null;
      if (!it) return;
      palIdx = parseInt(it.dataset.i, 10) || 0;
      exec();
    });
    return pal;
  }

  function move(d) {
    if (!palItems.length) return;
    palIdx = (palIdx + d + palItems.length) % palItems.length;
    paintSel();
    var el = palList.querySelector('.wb-item[aria-selected="true"]');
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
  }
  function paintSel() {
    for (var i = 0; i < palItems.length; i++) {
      palItems[i].setAttribute('aria-selected', i === palIdx ? 'true' : 'false');
    }
  }
  function exec() {
    var it = palItems[palIdx];
    closePalette();
    if (!it) return;
    try { it.__run(); } catch (e) {}
  }

  function fuzzy(text, q) {
    if (!q) return true;
    text = String(text || '').toLowerCase();
    q = q.toLowerCase();
    var pos = 0;
    for (var i = 0; i < q.length; i++) {
      var p = text.indexOf(q.charAt(i), pos);
      if (p < 0) return false;
      pos = p + 1;
    }
    return true;
  }

  function renderPalette(query) {
    var rows = [], runs = [], n = 0;
    var hits = [];
    for (var i = 0; i < COMMANDS.length; i++) {
      if (fuzzy(COMMANDS[i].t, query)) hits.push(COMMANDS[i]);
    }
    if (hits.length) {
      rows.push('<div class="wb-group">命令</div>');
      for (var j = 0; j < hits.length; j++) { rows.push(itemHtml(hits[j], n++)); runs.push(runOf(hits[j])); }
    }
    var sm = [];
    for (var k = 0; k < sessCache.length && sm.length < 8; k++) {
      if (fuzzy(sessCache[k].title || sessCache[k].t, query)) sm.push(sessCache[k]);
    }
    if (!query) sm = sessCache.slice(0, 6);
    if (sm.length) {
      rows.push('<div class="wb-group">会话</div>');
      for (var m = 0; m < sm.length; m++) { rows.push(itemHtml(sm[m], n++)); runs.push(runOf(sm[m])); }
    }
    palList.innerHTML = rows.length ? rows.join('') :
      '<div class="wb-empty">没有匹配的命令或会话</div>';
    palItems = qa('.wb-item', palList);
    for (var p = 0; p < palItems.length; p++) palItems[p].__run = runs[p];
    palIdx = 0;
    paintSel();
  }

  function runOf(o) {
    return function () {
      var args = o.args || [];
      try {
        if (typeof o.run === 'function') {
          if (args.length) o.run.apply(null, args); else o.run();
        }
      } catch (e) {}
    };
  }

  function itemHtml(o, idx) {
    var args = o.args || [];
    return '<div class="wb-item" data-i="' + idx + '" aria-selected="false">' +
      icon(o.i || 'i-compass') +
      '<span>' + String(o.t).replace(/</g, '&lt;') + '</span>' +
      (o.k ? '<span class="wb-kind">' + o.k + '</span>' : '') +
      (o.kbd ? kbdHtml(o.kbd) : '') +
      '</div>';
  }

  function loadSessionsForPalette() {
    try {
      var x = new XMLHttpRequest();
      x.open('GET', '/api/sessions?limit=30', true);
      x.onreadystatechange = function () {
        if (x.readyState !== 4 || x.status !== 200) return;
        try {
          var d = JSON.parse(x.responseText);
          sessCache = (d.sessions || []).map(function (s) {
            var title = (s.preview || '新对话').replace(/<[^>]*>/g, '').replace(/\n/g, ' ').slice(0, 40);
            return {
              t: title, k: s.updated_at ? String(s.updated_at).slice(0, 10) : '',
              i: 'i-message-circle', run: safe('loadSession'), args: [s.id]
            };
          });
        } catch (e) { sessCache = []; }
      };
      x.send();
    } catch (e) {}
  }

  function openPalette() {
    buildPalette();
    pal.classList.add('wb-on');
    palInput.value = '';
    palIdx = 0;
    renderPalette('');
    a11yOpen(pal, '命令面板');
  }
  function closePalette() {
    if (!pal) return;
    pal.classList.remove('wb-on');
    a11yClose(pal);
  }
  function paletteOpen() { return pal && pal.classList.contains('wb-on'); }

  /* ═══════════ 3. 输入框：历史 / 草稿 / 计数 ═══════════ */
  var hist = [], hi = -1, lastSid = null;
  function currentSid() {
    try { return (typeof currentSessionId !== 'undefined' && currentSessionId) ? currentSessionId : '__new__'; }
    catch (e) { return '__new__'; }
  }
  function draftKey(sid) { return 'scout_draft_' + sid; }

  function estTokens(s) {
    var cjk = (s.match(/[\u4e00-\u9fa5\u3040-\u30ff]/g) || []).length;
    var rest = s.length - cjk;
    return Math.round(cjk * 1.6 + rest / 4);
  }
  function ensureCounter() {
    var holder = q('#latency-badge');
    if (!holder || !holder.parentNode) return null;
    if (q('#wb-counter')) return q('#wb-counter');
    var span = document.createElement('span');
    span.id = 'wb-counter';
    holder.parentNode.appendChild(span);
    return span;
  }
  var updCount = debounce(function () {
    var el = ensureCounter();
    if (!el || !inputEl) return;
    var v = inputEl.value || '';
    if (!v.trim()) { el.textContent = ''; el.classList.remove('wb-warn'); return; }
    var tk = estTokens(v);
    el.textContent = T('{n} 字 · ≈{m} tokens', { n: v.length, m: tk });
    el.classList.toggle('wb-warn', tk > 6000);
  }, 160);

  var inputEl = null;
  function bindInput() {
    inputEl = q('#input');
    if (!inputEl) return;
    inputEl.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowUp' && !e.shiftKey) {
        var multi = inputEl.value.indexOf('\n') >= 0;
        var atStart = inputEl.selectionStart === 0 && inputEl.selectionEnd === 0;
        if (!multi || atStart) {
          if (hist.length) {
            hi = hi < 0 ? hist.length - 1 : Math.max(0, hi - 1);
            inputEl.value = hist[hi];
            e.preventDefault();
            setTimeout(function () { inputEl.setSelectionRange(inputEl.value.length, inputEl.value.length); }, 0);
          }
        }
      } else if (e.key === 'ArrowDown' && !e.shiftKey) {
        if (hi >= 0) {
          hi = hi + 1;
          if (hi >= hist.length) { hi = -1; inputEl.value = ''; }
          else inputEl.value = hist[hi];
          e.preventDefault();
        }
      } else if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        var v = inputEl.value.trim();
        if (v) { hist.push(v); if (hist.length > 60) hist.shift(); hi = -1; }
      }
    });
    inputEl.addEventListener('input', function () {
      updCount();
      saveDraft();
      autoGrow();
    });
  }
  var saveDraft = debounce(function () {
    if (!inputEl) return;
    var v = inputEl.value || '';
    var k = draftKey(currentSid());
    if (v.trim()) store(k, v); else try { localStorage.removeItem(k); } catch (e) {}
  }, 400);

  function autoGrow() {
    if (!inputEl) return;
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 240) + 'px';
  }

  function watchSessionSwitch() {
    var check = function () {
      var sid = currentSid();
      if (sid === lastSid) return;
      if (lastSid !== null && inputEl) {
        var old = inputEl.value || '';
        if (old.trim()) store(draftKey(lastSid), old); else try { localStorage.removeItem(draftKey(lastSid)); } catch (e) {}
      }
      lastSid = sid;
      if (!inputEl) return;
      var saved = store(draftKey(sid));
      inputEl.value = saved || '';
      autoGrow();
      updCount();
    };
    // 兜底轮询（开销只是读一个变量），真正的切换检测靠下面的 loadSession 钩子
    setInterval(check, 1000);
    if (typeof window.loadSession === 'function' && !window.loadSession.__wbDraft) {
      var oldLs = window.loadSession;
      window.loadSession = function () {
        var r = oldLs.apply(this, arguments);
        // loadSession 内部多为异步渲染，稍等一拍再对齐草稿
        setTimeout(check, 120);
        return r;
      };
      window.loadSession.__wbDraft = 1;
    }
  }

  /* ═══════════ 4. 会话置顶 ═══════════ */
  var PINKEY = 'scout_pinned_sessions';
  function pins() { try { return JSON.parse(localStorage.getItem(PINKEY) || '[]'); } catch (e) { return []; } }
  function setPins(a) { store(PINKEY, JSON.stringify(a)); }
  function togglePin(sid, btn) {
    var a = pins(), i = a.indexOf(sid);
    if (i >= 0) a.splice(i, 1); else a.unshift(sid);
    setPins(a);
    if (btn) btn.classList.toggle('wb-pinned', a.indexOf(sid) >= 0);
    renderPins();
  }

  var sessTitleMap = {};
  function tagSessionRows() {
    var list = q('#session-list');
    if (!list) return;
    qa('[id^="session-title-"]', list).forEach(function (sp) {
      var row = sp.parentNode;
      if (!row || row.dataset.wb) return;
      row.dataset.wb = '1';
      var sid = sp.id.replace('session-title-', '');
      sessTitleMap[sid] = sp.textContent;
      var box = row.children[row.children.length - 1];
      if (box && box.classList.contains('hidden')) {
        var b = document.createElement('button');
        b.className = 'wb-sess-pin' + (pins().indexOf(sid) >= 0 ? ' wb-pinned' : '');
        b.title = '置顶此会话';
        b.innerHTML = icon('i-pin');
        b.addEventListener('click', function (e) { e.stopPropagation(); togglePin(sid, b); });
        box.appendChild(b);
      }
    });
    renderPins();
  }
  function renderPins() {
    var list = q('#session-list');
    if (!list) return;
    var old = q('.wb-pin-box', list);
    if (old) old.remove();
    var a = pins();
    if (!a.length) return;
    var box = document.createElement('div');
    box.className = 'wb-pin-box';
    var html = '<div class="wb-pin-grp">置顶</div>';
    for (var i = 0; i < a.length; i++) {
      var sid = a[i];
      if (!sessTitleMap[sid]) continue;
      html += '<div class="wb-pin-row">' +
        '<span class="wb-pin-title" data-sid="' + sid + '">' +
          String(sessTitleMap[sid]).replace(/</g, '&lt;') + '</span>' +
        '<button class="wb-pin-btn" data-unpin="' + sid + '" title="取消置顶">' + icon('i-x') + '</button>' +
        '</div>';
    }
    box.innerHTML = html;
    list.insertBefore(box, list.firstChild);
    qa('.wb-pin-title', box).forEach(function (el) {
      el.addEventListener('click', function () { safe('loadSession')(el.dataset.sid); });
    });
    qa('.wb-pin-btn', box).forEach(function (el) {
      el.addEventListener('click', function (e) {
        e.stopPropagation();
        togglePin(el.dataset.unpin, null);
        qa('.wb-sess-pin').forEach(function (b) { b.classList.remove('wb-pinned'); });
        var arr = pins();
        qa('[id^="session-title-"]').forEach(function (sp) {
          if (arr.indexOf(sp.id.replace('session-title-', '')) >= 0) {
            var r = sp.parentNode, bx = r && r.children[r.children.length - 1], pb = bx && q('.wb-sess-pin', bx);
            if (pb) pb.classList.add('wb-pinned');
          }
        });
      });
    });
  }

  /* ═══════════ 5. 设置弹窗：键盘 + 记忆 tab ═══════════ */
  function bindSettingsKeys() {
    if (typeof window.openSettings === 'function' && !window.__wbOpenSettingsPatched) {
      window.__wbOpenSettingsPatched = 1;
      var orig = window.openSettings;
      window.openSettings = function (tab) {
        if (!tab) tab = store('scout_settings_tab') || 'model';
        store('scout_settings_tab', tab);
        orig(tab);
      };
    }
    document.addEventListener('keydown', function (e) {
      var m = q('#settings-modal');
      if (!m || m.classList.contains('hidden')) return;
      if (e.key === 'Escape') { safe('closeSettings')(); return; }
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      var cur = TABS.filter(function (t) {
        var el = q('#panel-' + t);
        return el && !el.classList.contains('hidden');
      })[0] || 'model';
      var i = TABS.indexOf(cur);
      i = (i + (e.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length;
      safe('switchTab')(TABS[i]);
      store('scout_settings_tab', TABS[i]);
      e.preventDefault();
    });
  }

  /* ═══════════ 6. 欢迎屏：继续上次会话 + 快捷键提示 ═══════════ */
  function enrichWelcome() {
    var w = q('#welcome-screen');
    if (!w || w.dataset.wb) return;
    w.dataset.wb = '1';
    try {
      var x = new XMLHttpRequest();
      x.open('GET', '/api/sessions?limit=1', true);
      x.onreadystatechange = function () {
        if (x.readyState !== 4 || x.status !== 200) return;
        var s;
        try { s = (JSON.parse(x.responseText).sessions || [])[0]; } catch (e) { return; }
        if (!s) return;
        var title = (s.preview || '上次的对话').replace(/<[^>]*>/g, '').slice(0, 30);
        var a = document.createElement('div');
        a.className = 'wb-resume';
        a.innerHTML = icon('i-message-square') + '<span>继续上次：</span><span class="wb-resume-t">' +
          String(title).replace(/</g, '&lt;') + '</span>';
        a.addEventListener('click', function () { safe('loadSession')(s.id); });
        var grid = q('.grid', w);
        if (grid && grid.parentNode) grid.parentNode.insertBefore(a, grid);
      };
      x.send();
    } catch (e) {}
    var tips = document.createElement('div');
    tips.className = 'wb-tips';
    tips.innerHTML =
      '<span><b>Enter</b> 发送 · <b>Shift+Enter</b> 换行</span>' +
      '<span><b>↑</b> 调出上一条输入</span>' +
      '<span><b>Ctrl/⌘ + K</b> 命令面板</span>' +
      '<span><b>Esc</b> 停止生成</span>';
    var g = q('.grid', w);
    if (g && g.parentNode) g.parentNode.appendChild(tips);
  }

  /* ═══════════ 7. 无障碍 ═══════════ */
  var srNode = null;
  function ensureSr() {
    if (srNode) return srNode;
    srNode = document.createElement('div');
    srNode.className = 'wb-sr';
    srNode.setAttribute('aria-live', 'polite');
    srNode.setAttribute('aria-atomic', 'true');
    document.body.appendChild(srNode);
    return srNode;
  }
  function announce(txt) {
    var el = ensureSr();
    el.textContent = '';
    setTimeout(function () { el.textContent = txt; }, 60);
  }

  function hookBubbleFinalize() {
    // class 顶层声明不挂 window —— 直接用全局词法绑定
    var AC = (typeof AgentBubble !== 'undefined') ? AgentBubble : window.AgentBubble;
    if (!AC || !AC.prototype || AC.prototype.__wbAnn) return;
    AC.prototype.__wbAnn = 1;
    var old = AC.prototype.finalize;
    AC.prototype.finalize = function (steps, usage) {
      var text = (this.text || '').replace(/[#*`>]/g, ' ').replace(/\s+/g, ' ').slice(0, 140);
      try { old.call(this, steps, usage); } catch (e) {}
      try {
        if (text) announce(T('回答已完成，共 {n} 步。', { n: steps || 0 }) + text);
        else announce(T('任务已完成，共 {n} 步。', { n: steps || 0 }));
      } catch (e) {}
    };
  }

  function hookScrollPause() {
    if (typeof window.scrollToBottom !== 'function' || window.__wbScrollPatched) return;
    window.__wbScrollPatched = 1;
    var old = window.scrollToBottom;
    window.scrollToBottom = function (force) {
      if (!force) {
        try {
          var sel = window.getSelection();
          if (sel && String(sel).length > 2) return;   // 用户正在选中内容 — 别动滚动
        } catch (e) {}
      }
      return old.apply(null, arguments);
    };
  }

  /* ═══════════ 8. 长会话虚拟化：远离视口的旧消息折叠为占位 ═══════════
     动机：几百轮的会话 DOM 节点数会到几万，滚动/样式重算明显掉帧。
     做法：#messages 的直接子节点若离视口 > margin，替换为等高占位 div，
           原节点以 detached 形式保留引用（保留 markmarked 渲染结果），
           回滚到视口附近（或点击占位）立即原地还原。
     安全边界（任一命中即不折叠）：
       - 末尾 keepTail 条（含正在流式的消息）
       - 内部仍有 spinner / 光标 / running 状态工具卡
       - 包含焦点元素
  */
  var VZ = {
    minCount: 40,      // 少于这个数量不启用
    keepTail: 8,       // 末尾始终保留
    margin: 900,       // 视口外多少 px 才折叠
    freeze: false,     // 查找期间冻结，只展开不折叠
    box: null, area: null, items: [], ticking: false
  };

  function vzNodeBusy(n) {
    if (!n || !n.querySelector) return false;
    var ae = document.activeElement;
    if (ae && ae !== document.body && ae !== document.documentElement && n.contains(ae)) return true;
    return !!n.querySelector(
      '.am-caret, .animate-pulse, [data-am-state="running"], ' +
      '.tool-icon.animate-spin, .font-thinking'
    );
  }

  function vzCollect() {
    var box = VZ.box;
    if (!box) return;
    var kids = Array.prototype.slice.call(box.children);
    var next = [];
    for (var i = 0; i < kids.length; i++) {
      var n = kids[i];
      if (n.__wbPh) {
        // 占位：找回挂在它身上的记录
        var rec = n.__wbRec;
        if (rec) { rec.ph = n; next.push(rec); }
      } else {
        var found = null;
        for (var j = 0; j < VZ.items.length; j++) {
          if (VZ.items[j].node === n) { found = VZ.items[j]; break; }
        }
        if (!found) found = { node: n, ph: null, h: 0 };
        found.ph = null;
        found.i = i;
        next.push(found);
      }
    }
    // 清理已经被彻底移除的记录
    for (var k = 0; k < VZ.items.length; k++) {
      var it = VZ.items[k];
      if (next.indexOf(it) < 0 && it.ph && !it.ph.isConnected) it.ph.__wbRec = null;
    }
    VZ.items = next;
  }

  function vzLabel(item) {
    var n = item.node;
    var who = n && n.classList && n.classList.contains('flex-row-reverse') ? '我' : 'Scout';
    var body = (n && n.querySelector) ? (n.querySelector('.msg-text, .agent-content') || n) : n;
    var txt = ((body && body.textContent) || '').replace(/\s+/g, ' ').trim().slice(0, 40);
    return who + (txt ? ' · ' + txt : '');
  }

  function vzCollapse(item) {
    var n = item.node;
    if (!n.parentNode) return;
    item.h = n.offsetHeight || item.h || 0;
    var ph = document.createElement('div');
    ph.className = 'wb-ph';
    // 高度与外边距都照搬原节点：保证替换后文档总高不变，滚动位置不跳
    try { ph.style.marginTop = getComputedStyle(n).marginTop; } catch (e) {}
    ph.style.height = (item.h || 48) + 'px';
    ph.innerHTML =
      '<span class="wb-ph-bar"></span>' +
      '<span class="wb-ph-t">' + escHtml(vzLabel(item)) + '</span>' +
      '<span class="wb-ph-a">已折叠 · 点击查看</span>';
    ph.__wbPh = 1;
    ph.__wbRec = item;
    ph.addEventListener('click', function () { vzExpand(item, true); });
    n.parentNode.replaceChild(ph, n);
    item.ph = ph;
  }

  function vzExpand(item, scrollKeep) {
    var ph = item.ph;
    if (!ph || !ph.parentNode) return;
    var prevTop = VZ.area ? VZ.area.scrollTop : 0;
    ph.parentNode.replaceChild(item.node, ph);
    item.ph = null;
    ph.__wbRec = null;
    if (scrollKeep && VZ.area) VZ.area.scrollTop = prevTop;
  }

  function vzUpdate(force) {
    if (!VZ.box || !VZ.area) return;
    vzCollect();
    if (VZ.items.length < VZ.minCount && !force) {
      // 数量回落：已折叠的保持现状即可（用户可能正停在早期位置）
      return;
    }
    var areaRect = VZ.area.getBoundingClientRect();
    var top = areaRect.top - VZ.margin;
    var bottom = areaRect.bottom + VZ.margin;
    var n = VZ.items.length;
    for (var i = 0; i < n; i++) {
      var it = VZ.items[i];
      if (i >= n - VZ.keepTail) { if (it.ph) vzExpand(it, true); continue; }
      if (it.ph) continue;                       // 已折叠的不在此被动处理（等滚动靠近）
      if (vzNodeBusy(it.node)) continue;
      // 查找期间冻结折叠：否则刚展开的旧消息会在下一次滚动回调里又被收回去，
      // 命中明明存在却定位不到。
      if (VZ.freeze) continue;
      var r = it.node.getBoundingClientRect();
      var far = r.bottom < top || r.top > bottom;
      if (far) vzCollapse(it);
    }
    var nearHits = 0;
    for (var j = 0; j < n; j++) {
      var it2 = VZ.items[j];
      if (!it2.ph) continue;
      var pr = it2.ph.getBoundingClientRect();
      var near = pr.bottom > areaRect.top - 240 && pr.top < areaRect.bottom + 240;
      if (near) { nearHits++; vzExpand(it2, true); }
    }
    VZ.lastRun = { n: n, ph: document.querySelectorAll('.wb-ph').length,
                   near: nearHits, at: Date.now() };
  }

  function vzExpandAll() {
    for (var i = 0; i < VZ.items.length; i++) if (VZ.items[i].ph) vzExpand(VZ.items[i], true);
  }

  /* 跳转前必须先还原：被折叠的节点是 detached 的，scrollIntoView 对它毫无作用。
     用法：vzReveal(target) → true 表示刚被展开（调用方需延后一帧再滚动）。 */
  function vzReveal(node) {
    if (!node || !VZ.box) return false;
    var i, it;
    // 1) 目标可能藏在某条被折叠（detached）消息的子树里 —— contains 对 detached 树同样有效，
    //    但 parentNode 链已断，所以只能逐个 item 比，不能往上爬。
    for (i = 0; i < VZ.items.length; i++) {
      it = VZ.items[i];
      if (!it.ph || !it.node) continue;
      if (it.node === node || (it.node.contains && it.node.contains(node))) { vzExpand(it, true); return true; }
    }
    // 2) 目标在当前文档里：向上爬到 #messages 的直接子节点再比对
    var n = node;
    while (n && n.parentNode && n.parentNode !== VZ.box) n = n.parentNode;
    if (!n || n.parentNode !== VZ.box) return false;
    for (i = 0; i < VZ.items.length; i++) {
      it = VZ.items[i];
      if (it.node === n && it.ph) { vzExpand(it, true); return true; }
    }
    return false;
  }
  window.__wbVzReveal = vzReveal;

  // 统一的"滚到某个节点"入口：先还原折叠、再滚动、再打高亮
  function vzScrollTo(node, flash) {
    if (!node) return;
    var revealed = vzReveal(node);
    var go = function () {
      try { node.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
      catch (e) { node.scrollIntoView(); }
      if (flash !== false) {
        node.classList.add('wb-crumb-flash');
        setTimeout(function () { node.classList.remove('wb-crumb-flash'); }, 1100);
      }
    };
    // 展开后文档高度变化，等一帧布局稳定再滚，否则会滚到错误位置
    if (revealed) requestAnimationFrame(function () { requestAnimationFrame(go); });
    else go();
  }

  function initVirtualizer() {
    VZ.box = q('#messages');
    VZ.area = q('#chat-area');
    if (!VZ.box || !VZ.area) return;

    var upd = debounce(function () { vzUpdate(false); }, 140);
    VZ.area.addEventListener('scroll', upd, { passive: true });
    window.addEventListener('resize', debounce(function () { vzUpdate(false); }, 220));
    var mo = new MutationObserver(debounce(function () {
      if (VZ.box.children.length >= VZ.minCount) vzUpdate(false);
    }, 500));
    mo.observe(VZ.box, { childList: true });

    // 打印/查找前先全部还原，避免 Ctrl+F 找不到折叠掉的内容
    window.addEventListener('beforeprint', vzExpandAll);
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'f' || e.key === 'F')) vzExpandAll();
    }, true);
  }

  /* ═══════════ 9. 产物面板：会话内生成/修改的文件统一收拢 ═══════════
     数据源：AgentBubble.addFileAttachment（历史渲染与实时 file 事件都走这里），
     patch 一处即可全量收集；切换会话时清空重收。 */
  var FP = { map: {}, order: [], drawer: null, listEl: null, prevEl: null, btn: null, open: false };

  function fpCollect(f) {
    var p = f && f.file_path;
    if (!p) return;
    var rec = FP.map[p];
    if (!rec) {
      rec = {
        path: p,
        name: f.file_name || String(p).split(/[\\/]/).pop(),
        size: f.file_size || 0,
        ts: Date.now()
      };
      FP.map[p] = rec;
      FP.order.unshift(rec);
      if (FP.order.length > 200) delete FP.map[FP.order.pop().path];
    } else {
      rec.ts = Date.now();
    }
    fpBadge();
    if (FP.open && FP.listEl) fpRender();
  }

  function fpReset() {
    FP.map = {};
    FP.order = [];
    fpBadge();
    if (FP.open && FP.listEl) fpRender();
  }

  function fpHook() {
    var AB = (typeof AgentBubble !== 'undefined') ? AgentBubble : window.AgentBubble;
    if (AB && AB.prototype && AB.prototype.addFileAttachment) {
      var orig = AB.prototype.addFileAttachment;
      AB.prototype.addFileAttachment = function (f) {
        try { fpCollect(f); } catch (e) {}
        return orig.call(this, f);
      };
    }
    if (typeof window.loadSession === 'function') {
      var origLoad = window.loadSession;
      window.loadSession = function () {
        try { fpReset(); } catch (e) {}
        return origLoad.apply(this, arguments);
      };
    }
  }

  function fmtSize(n) {
    if (!n) return '';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }

  function fpIsImg(name) { return /\.(png|jpe?g|gif|webp|bmp|svg|ico)$/i.test(name || ''); }

  function fpDl(path) {
    var w = window.withAuth || function (u) { return u; };
    return w('/api/files/download?path=' + encodeURIComponent(String(path).replace(/\\/g, '/')));
  }

  function fpEnsure() {
    if (FP.drawer) return FP.drawer;
    FP.drawer = document.createElement('div');
    FP.drawer.id = 'wb-files';
    FP.drawer.innerHTML =
      '<div class="wb-scrim" data-wb-fp-close="1"></div>' +
      '<aside class="wb-panel">' +
        '<header class="wb-fp-head">' +
          '<span class="wb-fp-title">' + icon('i-folder') + '产物</span>' +
          '<span class="wb-fp-count"></span>' +
          '<button class="wb-fp-close" data-wb-fp-close="1" title="关闭">' + icon('i-x') + '</button>' +
        '</header>' +
        '<div class="wb-fp-list"></div>' +
        '<div class="wb-fp-preview hidden"></div>' +
      '</aside>';
    document.body.appendChild(FP.drawer);
    FP.listEl = q('.wb-fp-list', FP.drawer);
    FP.prevEl = q('.wb-fp-preview', FP.drawer);
    FP.drawer.addEventListener('click', function (e) {
      if (e.target.closest('[data-wb-fp-close]')) { fpClose(); return; }
      if (e.target.closest('[data-wb-fp-close-pv]')) {
        FP.prevEl.classList.add('hidden');
        FP.listEl.classList.remove('hidden');
        return;
      }
      var pv = e.target.closest('[data-wb-fp-pv]');
      if (pv) { fpPreview(pv.getAttribute('data-wb-fp-pv')); return; }
      var row = e.target.closest('.wb-file-row');
      if (row && !e.target.closest('button')) fpPreview(row.getAttribute('data-path'));
    });
    // 图标：面板依赖 sprite 里的 i-folder / i-x，缺失时兜底注册
    fpEnsureIcons();
    return FP.drawer;
  }

  function fpEnsureIcons() {
    var sprite = q('#app-icons') || q('svg defs') || null;
    var need = {
      'i-folder': 'M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z',
      'i-x': 'M6 6l12 12M18 6L6 18',
      'i-file': 'M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8l-5-5zM14 3v5h5',
      'i-image': 'M5 3h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2zM3 17l5-5 4 4 3-3 6 6',
      'i-eye2': 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7zm10 3a3 3 0 100-6 3 3 0 000 6z'
    };
    var holder = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    holder.setAttribute('style', 'display:none');
    for (var id in need) {
      if (document.getElementById(id)) continue;
      var s = document.createElementNS('http://www.w3.org/2000/svg', 'symbol');
      s.id = id; s.setAttribute('viewBox', '0 0 24 24');
      s.setAttribute('fill', 'none'); s.setAttribute('stroke', 'currentColor');
      s.setAttribute('stroke-width', '2');
      s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round');
      var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      p.setAttribute('d', need[id]);
      s.appendChild(p);
      holder.appendChild(s);
    }
    document.body.insertBefore(holder, document.body.firstChild);
  }

  function fpBadge() {
    if (!FP.btn) return;
    var n = FP.order.length;
    FP.btn.classList.toggle('has-files', n > 0);
    var b = q('.wb-fp-badge', FP.btn);
    if (b) b.textContent = n > 99 ? '99+' : String(n);
  }

  function fpRender() {
    var c = q('.wb-fp-count', FP.drawer);
    if (c) c.textContent = FP.order.length ? String(FP.order.length) : '';
    if (!FP.order.length) {
      FP.listEl.innerHTML =
        '<div class="wb-fp-empty">本会话还没有产出文件。<br>让 Scout 生成或修改文件后，会自动汇总到这里。</div>';
      return;
    }
    FP.listEl.innerHTML = FP.order.map(function (r) {
      var dir = String(r.path).replace(/[\\/]/g, '/').split('/');
      dir.pop();
      return '<div class="wb-file-row" data-path="' + escHtml(r.path) + '">' +
        '<div class="wb-f-ico">' + (fpIsImg(r.name) ? icon('i-image') : icon('i-file')) + '</div>' +
        '<div class="wb-f-meta">' +
          '<div class="wb-f-name">' + escHtml(r.name) + '</div>' +
          '<div class="wb-f-path">' + escHtml(dir.join('/')) + '</div>' +
        '</div>' +
        '<span class="wb-f-size">' + escHtml(fmtSize(r.size)) + '</span>' +
        '<button data-wb-fp-pv="' + escHtml(r.path) + '" title="预览">' + icon('i-eye2') + '</button>' +
        '<button onclick="downloadFile(\'' + escHtml(r.path).replace(/'/g, "\\'") + '\')" title="下载">' + icon('i-download') + '</button>' +
      '</div>';
    }).join('');
  }

  function fpPreview(path) {
    var rec = FP.map[path];
    if (!rec) return;
    var url = fpDl(path);
    if (fpIsImg(rec.name)) {
      FP.prevEl.innerHTML =
        '<div class="wb-fp-pv-head"><button data-wb-fp-close-pv="1">' + icon('i-x') + ' 返回列表</button>' +
        '<span>' + escHtml(rec.name) + '</span></div>' +
        '<div class="wb-fp-pv-img"><img src="' + escHtml(url) + '" alt=""></div>';
      FP.prevEl.classList.remove('hidden');
      FP.listEl.classList.add('hidden');
      return;
    }
    FP.prevEl.innerHTML =
      '<div class="wb-fp-pv-head"><button data-wb-fp-close-pv="1">' + icon('i-x') + ' 返回列表</button>' +
      '<span>' + escHtml(rec.name) + '</span></div>' +
      '<pre class="wb-fp-pv-text">加载中…</pre>';
    FP.prevEl.classList.remove('hidden');
    FP.listEl.classList.add('hidden');
    fetch(url).then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (t) {
        var pre = q('.wb-fp-pv-text', FP.prevEl);
        if (!pre) return;
        pre.textContent = t.length > 4000
          ? t.slice(0, 4000) + '\n\n' + T('…（前 4000 字符，完整内容请下载）')
          : t;
      })
      .catch(function () {
        var pre = q('.wb-fp-pv-text', FP.prevEl);
        if (pre) pre.textContent = '无法预览该文件（可能是二进制或需要登录），请直接下载。';
      });
  }

  function fpOpen() {
    fpEnsure();
    fpRender();
    FP.drawer.classList.add('wb-open');
    FP.open = true;
    a11yOpen(FP.drawer.querySelector('.wb-panel') || FP.drawer, '产物面板');
  }
  function fpClose() {
    if (!FP.drawer) return;
    a11yClose(FP.drawer.querySelector('.wb-panel') || FP.drawer);
    FP.drawer.classList.remove('wb-open');
    FP.open = false;
    // 返回列表视图，下次打开从列表开始
    FP.prevEl && FP.prevEl.classList.add('hidden');
    FP.listEl && FP.listEl.classList.remove('hidden');
  }
  FP.close = fpClose;

  function fpBtnInject() {
    if (FP.btn) return;
    var anchor = document.querySelector('.w-px.h-5.bg-surface-4');
    var btn = document.createElement('button');
    btn.id = 'wb-files-btn';
    btn.title = '产物面板（本会话生成的文件）  Alt+F';
    btn.innerHTML = icon('i-folder') + '<span class="wb-fp-badge hidden"></span>';
    btn.addEventListener('click', fpOpen);
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(btn, anchor);
    else if (anchor) anchor.appendChild(btn);
    FP.btn = btn;
    fpBadge();
  }

  /* ═══════════ 10. Multi-Agent 面包屑：阶段直达 + 子代理下钻 ═══════════
     ma-panel 已有「规划 → 执行 → 汇总」三段，但只是状态展示：
       1) 三个阶段 pill 变成可点击 → 平滑滚动到对应区 + 短暂高亮
       2) 点击子代理卡片 → 下钻焦点视图（等高占位防跳动，实时更新不受影响）
          + 顶部面包屑「对话 › Multi-Agent › 子代理名」，Esc / 点遮罩返回 */
  var CR = { cur: null, spacer: null, bar: null, scrim: null, parent: null, next: null };

  function crBarHtml(name) {
    return '<span class="wb-crumb-root" data-wb-crumb-exit="1">对话</span>' +
      '<span class="wb-crumb-sep">›</span>' +
      '<span class="wb-crumb-mid">Multi-Agent</span>' +
      '<span class="wb-crumb-sep">›</span>' +
      '<span class="wb-crumb-cur">' + escHtml(name) + '</span>' +
      '<span class="wb-crumb-hint">Esc 返回</span>';
  }

  function crEnter(card) {
    if (!card || !card.isConnected || card.classList.contains('wb-sub-focus')) return;
    vzReveal(card);  // 卡片若仍在折叠区（detached），先还原再下钻
    crExit();
    // 卡片可能已随回合收束折叠进 <details class="activity-wrap"> —— details 不展开时内容不渲染
    var wrap = card.closest('details');
    if (wrap && !wrap.open) wrap.open = true;
    var name = card.getAttribute('data-sub-name') || '子代理';

    // portal：把卡片临时移到 body 下（祖先里的 transform 会劫持 position:fixed）
    CR.spacer = document.createElement('div');
    CR.spacer.style.height = card.offsetHeight + 'px';
    CR.parent = card.parentNode;
    CR.next = card.nextSibling;
    CR.parent.insertBefore(CR.spacer, card);
    document.body.appendChild(card);

    card.classList.add('wb-sub-focus');
    CR.cur = card;

    CR.scrim = document.createElement('div');
    CR.scrim.id = 'wb-sub-scrim';
    CR.scrim.addEventListener('click', crExit);
    document.body.appendChild(CR.scrim);

    CR.bar = document.createElement('div');
    CR.bar.id = 'wb-crumb';
    CR.bar.innerHTML = crBarHtml(name);
    CR.bar.addEventListener('click', function (e) {
      if (e.target.closest('[data-wb-crumb-exit]')) crExit();
    });
    document.body.appendChild(CR.bar);

    CR.a11y = a11yOpen(card, T('子代理') + ' · ' + name);
  }

  function crExit() {
    if (CR.cur) {
      a11yClose(CR.a11y);
      CR.a11y = null;
      CR.cur.classList.remove('wb-sub-focus');
      // 归位：占位处还原（若原父容器被重渲染则丢弃占位、尾插回去）
      if (CR.parent && CR.parent.isConnected) {
        if (CR.spacer && CR.spacer.parentNode === CR.parent) CR.parent.replaceChild(CR.cur, CR.spacer);
        else if (CR.next && CR.next.parentNode === CR.parent) CR.parent.insertBefore(CR.cur, CR.next);
        else CR.parent.appendChild(CR.cur);
      }
      CR.spacer = null;
      CR.parent = null;
      CR.next = null;
      CR.cur = null;
    }
    if (CR.scrim) { CR.scrim.remove(); CR.scrim = null; }
    if (CR.bar) { CR.bar.remove(); CR.bar = null; }
  }

  function crZooming() { return !!CR.cur; }

  function crWireStages(panel) {
    var map = [
      ['ma-stage-plan', '.ma-plan'],
      ['ma-stage-exec', '.ma-subgrid'],
      ['ma-stage-fin', '.ma-summary']
    ];
    for (var i = 0; i < map.length; i++) {
      (function (pair) {
        var pill = panel.querySelector('.' + pair[0]);
        if (!pill || pill.dataset.wbStage) return;
        pill.dataset.wbStage = '1';
        pill.classList.add('wb-stage-pill');
        pill.addEventListener('click', function () {
          var target = panel.querySelector(pair[1]);
          if (!target) return;
          vzScrollTo(target);
        });
      })(map[i]);
    }
  }

  function crWireSubCards(panel) {
    var grid = panel.querySelector('.ma-sub-cards');
    if (!grid) return;
    var cards = qa('.sub-card', grid);
    for (var i = 0; i < cards.length; i++) {
      (function (card) {
        if (card.dataset.wbCrumb) return;
        card.dataset.wbCrumb = '1';
        card.classList.add('wb-sub-clickable');
        card.addEventListener('click', function (e) {
          if (e.target.closest('button')) return;
          crEnter(card);
        });
      })(cards[i]);
    }
  }

  function initCrumbs() {
    var mo = new MutationObserver(debounce(function () {
      var panel = q('#messages .ma-panel');
      if (!panel) return;
      crWireStages(panel);
      crWireSubCards(panel);
    }, 260));
    var box = q('#messages');
    if (box) mo.observe(box, { childList: true, subtree: true });

    // 调试/截图钩子：?crumb=1 自动下钻第一个子代理卡片
    if (/[?&]crumb=1/.test(location.search)) {
      setTimeout(function () {
        var card = q('#messages .ma-sub-cards .sub-card');
        if (card) crEnter(card);
      }, 2600);
    }
  }

  /* ═══════════ 浮层无障碍 ═══════════
     命令面板 / 产物抽屉 / 子代理下钻都是运行时插入的浮层，原本没有 dialog 语义：
     读屏不知道它是弹窗，打开后焦点还留在原处，Tab 能跑到浮层背后的内容上。
     这里统一补：role=dialog + aria-modal + aria-label、打开聚焦首个可交互元素、
     Esc/关闭后焦点还原、Tab 在浮层内循环。
  */
  var FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select,textarea,' +
    '[tabindex]:not([tabindex="-1"])';

  function a11yMark(el, label) {
    if (!el) return;
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    if (label) el.setAttribute('aria-label', T(label));
    if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1');
  }

  function a11yOpen(el, label) {
    if (!el) return null;
    a11yMark(el, label);
    var prev = document.activeElement;
    el.__wbPrevFocus = (prev && prev !== document.body) ? prev : null;
    var first = el.querySelector(FOCUSABLE);
    setTimeout(function () {
      try { (first || el).focus({ preventScroll: true }); } catch (e) { try { (first || el).focus(); } catch (e2) {} }
    }, 20);
    return first || el;
  }

  function a11yClose(el) {
    if (!el) return;
    var prev = el.__wbPrevFocus;
    el.__wbPrevFocus = null;
    if (prev && prev.isConnected) {
      setTimeout(function () { try { prev.focus({ preventScroll: true }); } catch (e) {} }, 10);
    }
  }

  // Tab / Shift+Tab 在浮层内循环，不会跑到背后内容上
  function a11yTrap(el, e) {
    if (!el || e.key !== 'Tab') return;
    var list = qa(FOCUSABLE, el).filter(function (n) {
      return n.offsetParent !== null || n === document.activeElement;
    });
    if (!list.length) { e.preventDefault(); return; }
    var first = list[0], last = list[list.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === el)) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  }

  /* ═══════════ 11. emoji → 线性图标 ═══════════
     动机：界面上散落着上百个彩色 emoji（🧩📌🔧🤖✅⚠️…），跟已经统一好的
           发丝边 + 单色线性风格不搭，且在深浅两主题下颜色不可控。
     做法：渲染后扫描叶子文本节点，把 emoji 段替换成 <svg><use href="#i-*"/></svg>，
           图标颜色继承 currentColor，主题切换自动跟随。
     边界：跳过 script/style/pre/code/textarea（那里的 emoji 是内容不是装饰）；
           只处理叶子文本节点，不重建 innerHTML，事件与输入状态不受影响。
  */
  var EMOJI_MAP = {
    // 工具类
    '\u{1F50D}': 'i-search',      // 🔍
    '\u{1F310}': 'i-globe',       // 🌐
    '\u{26A1}': 'i-zap',          // ⚡
    '\u{1F4C2}': 'i-folder',      // 📂
    '\u{1F9E0}': 'i-cpu',         // 🧠
    '\u{1F4BB}': 'i-code',        // 💻
    '\u{1F4E4}': 'i-upload',      // 📤
    '\u{1F3A8}': 'i-palette',     // 🎨
    '\u{1F441}': 'i-eye',         // 👁
    '\u{23F0}': 'i-clock',        // ⏰
    '\u{1F5A5}': 'i-monitor',     // 🖥
    '\u{1F4DA}': 'i-book-open',   // 📚
    '\u{1F527}': 'i-wrench',      // 🔧
    '\u{1F6E0}': 'i-wrench',      // 🛠
    '\u{1F4CB}': 'i-clipboard',   // 📋
    '\u{1F9E9}': 'i-layers',      // 🧩
    '\u{1F4AD}': 'i-lightbulb',   // 💭
    '\u{1F4A1}': 'i-lightbulb',   // 💡
    '\u{2728}': 'i-zap',          // ✨
    '\u{1F31F}': 'i-zap',         // 🌟
    '\u{1F525}': 'i-zap',         // 🔥
    '\u{1F3AF}': 'i-zap',         // 🎯
    '\u{1F4BE}': 'i-archive',     // 💾
    '\u{1F4C5}': 'i-clock',       // 📅
    '\u{1F550}': 'i-clock',       // 🕐
    '\u{23F1}': 'i-clock',        // ⏱
    '\u{23F3}': 'i-clock',        // ⏳
    '\u{2699}': 'i-wrench',       // ⚙
    '\u{270F}': 'i-pencil',       // ✏
    '\u{1F5D1}': 'i-trash',       // 🗑
    // 文件类型
    '\u{1F5BC}': 'i-image',       // 🖼
    '\u{1F3B5}': 'i-music',       // 🎵
    '\u{1F3AC}': 'i-video',       // 🎬
    '\u{1F4C4}': 'i-file-text',   // 📄
    '\u{1F4DD}': 'i-file-text',   // 📝
    '\u{1F5DC}': 'i-archive',     // 🗜
    '\u{1F4CA}': 'i-table',       // 📊
    '\u{1F4D1}': 'i-presentation',// 📑
    '\u{1F4CE}': 'i-paperclip',   // 📎
    '\u{1F4E5}': 'i-download',    // 📥
    '\u{1F3A4}': 'i-mic',         // 🎤
    '\u{1F4ED}': 'i-inbox',       // 📭
    // 状态
    '\u{2705}': 'i-check-circle', // ✅
    '\u{274C}': 'i-x-circle',     // ❌
    '\u{26A0}': 'i-warning',      // ⚠
    // Agent / 协作
    '\u{1F916}': 'i-bot',         // 🤖
    '\u{1F91D}': 'i-bot',         // 🤝
    '\u{1F9ED}': 'i-compass',     // 🧭
    '\u{1F500}': 'i-branch',      // 🔀
    '\u{1F4CC}': 'i-pin',         // 📌
    // 渠道
    '\u{1F426}': 'i-feishu',      // 🐦 飞书
    '\u{1F4AC}': 'i-message-circle', // 💬
    '\u{1F4E2}': 'i-megaphone',   // 📢
    '\u{1F3E2}': 'i-building',    // 🏢
    '\u{1F3AE}': 'i-gamepad',     // 🎮
    '\u{1F4BC}': 'i-briefcase',   // 💼
    '\u{1F427}': 'i-hash',        // 🐧 QQ
    '\u{2708}': 'i-plane',        // ✈ Telegram
    '\u{1F399}': 'i-headphones',  // 🎧
    '\u{1F464}': 'i-user',        // 👤
    '\u{1F4E1}': 'i-radio'        // 📡
  };
  // emoji（含变体选择符 / ZWJ 连字）连续段
  // 注意：带 u 标志时正则按码点匹配，代理对区间 [\uD800-\uDBFF][\uDC00-\uDFFF] 匹配不到
  // astral 字符，必须用 \u{...} 直接写码点范围。
  var EMOJI_RE = new RegExp(
    '(?:[\\u2190-\\u21FF\\u2300-\\u23FF\\u25A0-\\u27BF\\u2B00-\\u2BFF\\uFE0F\\u200D]' +
    '|[\\u{1F000}-\\u{1FAFF}])+', 'gu');
  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, PRE: 1, CODE: 1, TEXTAREA: 1, INPUT: 1, NOSCRIPT: 1, SVG: 1 };

  function emojiIdFor(ch) {
    return EMOJI_MAP[ch] || EMOJI_MAP[ch.replace(/\uFE0F/g, '')] || null;
  }

  function emojiInNode(node) {
    var txt = node.textContent;
    if (!txt) return false;
    // 快速否定：注意不能用 indexOf('\uD83D') 之类的代理对判断 —— U+1F900 段的
    // 🧩🤖🧠🧭🤝 高位代理是 D83E，会被漏掉。统一交给正则判。
    EMOJI_RE.lastIndex = 0;
    if (!EMOJI_RE.test(txt)) return false;
    EMOJI_RE.lastIndex = 0;
    var hits = [];
    EMOJI_RE.lastIndex = 0;
    var m;
    while ((m = EMOJI_RE.exec(txt))) {
      var id = emojiIdFor(m[0]);
      if (id) hits.push([m.index, m.index + m[0].length, id]);
    }
    if (!hits.length) return false;
    // 从后往前切，避免前面的替换打乱后面的索引
    var cur = node;
    for (var i = hits.length - 1; i >= 0; i--) {
      var h = hits[i];
      if (h[1] > cur.textContent.length) continue;
      cur.splitText(h[1]);            // 尾段
      var mid = cur.splitText(h[0]);  // emoji 段
      var span = document.createElement('span');
      span.className = 'wb-ico';
      span.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#' + h[2] + '"/></svg>';
      mid.parentNode.replaceChild(span, mid);
    }
    return true;
  }

  function emojiSweep(root) {
    if (!root) return 0;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var nodes = [], n;
    while (walker.nextNode()) {
      n = walker.currentNode;
      var p = n.parentNode;
      if (!p || p.nodeType !== 1) continue;
      if (SKIP_TAGS[p.tagName]) continue;
      if (p.closest && p.closest('.wb-ico, script, style')) continue;
      if (!n.textContent.trim()) continue;
      nodes.push(n);
    }
    var hit = 0, touched = [];
    for (var i = 0; i < nodes.length; i++) {
      if (!nodes[i].parentNode) continue;
      if (emojiInNode(nodes[i])) { hit++; if (touched.indexOf(nodes[i].parentNode) < 0) touched.push(nodes[i].parentNode); }
    }
    // 英文模式下，被拆出来的剩余中文要补一次翻译
    if (hit && typeof I18N !== 'undefined' && I18N && I18N._translateTree) {
      for (var j = 0; j < touched.length; j++) {
        try { I18N._translateTree(touched[j]); } catch (e) {}
      }
    }
    return hit;
  }

  var emojiMO = null;
  var emojiBusy = false;
  function initEmoji() {
    emojiSweep(document.body);
    if (emojiMO) return;
    // 同步处理：流式插入的节点若延到下一帧再扫，截图/自动化测试常抓不到替换后的结果。
    // 用 emojiBusy 挡住自己造成的二次回调（替换本身也是 DOM 改动）。
    emojiMO = new MutationObserver(function (muts) {
      if (emojiBusy) return;
      var pend = [], i, j;
      for (i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (m.type === 'characterData') {
          var tn = m.target;
          if (tn && tn.nodeType === 3 && tn.parentNode && !SKIP_TAGS[tn.parentNode.tagName]) pend.push(tn);
          continue;
        }
        // textContent 赋值走的是 childList（换掉旧文本节点），所以文本节点也要收集
        var a = m.addedNodes;
        for (j = 0; j < a.length; j++) {
          if (a[j].nodeType === 1 || a[j].nodeType === 3) pend.push(a[j]);
        }
      }
      if (!pend.length) return;
      emojiBusy = true;
      try {
        emojiMO.takeRecords();
        for (i = 0; i < pend.length; i++) {
          var nd = pend[i];
          if (!nd.isConnected && nd.parentNode == null) continue;
          if (nd.nodeType === 3) { try { emojiInNode(nd); } catch (e) {} }
          else emojiSweep(nd);
        }
      } catch (e) {}
      emojiBusy = false;
    });
    emojiMO.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  /* ═══════════ 12. 会话内查找 ═══════════
     浏览器原生 Ctrl+F 在长会话上有两个问题：折叠区搜不到（节点已 detached），
     命中也不会在虚拟滚动里定位。这里做一个会话内查找条：
       · 打开时先展开全部折叠（模块 8）
       · 命中用 <mark class="wb-hit"> 包裹纯文本节点，退出时原样 unwrap
       · Enter/↓ 下一个，Shift+Enter/↑ 上一个，Esc 关闭并清除高亮
     快捷键用 Ctrl+Shift+F，避开浏览器原生查找；重复按可继续聚焦。
  */
  var FS = { bar: null, input: null, hits: [], idx: -1, q: '', marks: [] };

  function fsFind(root, needle) {
    var out = [];
    if (!root || !needle) return out;
    var lower = needle.toLowerCase();
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var nodes = [], n;
    while (walker.nextNode()) {
      n = walker.currentNode;
      var p = n.parentNode;
      if (!p || p.nodeType !== 1) continue;
      if (SKIP_TAGS[p.tagName]) continue;
      if (!n.textContent) continue;
      nodes.push(n);
    }
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var txt = node.textContent.toLowerCase();
      var pos = 0, at;
      while ((at = txt.indexOf(lower, pos)) >= 0) {
        out.push({ node: node, start: at, len: needle.length });
        pos = at + Math.max(lower.length, 1);
        if (out.length > 4000) break;
      }
    }
    return out;
  }

  function fsClear() {
    for (var i = 0; i < FS.marks.length; i++) {
      var mk = FS.marks[i];
      if (!mk.parentNode) continue;
      var parent = mk.parentNode;
      while (mk.firstChild) parent.insertBefore(mk.firstChild, mk);
      parent.removeChild(mk);
      try { parent.normalize(); } catch (e) {}
    }
    FS.marks = []; FS.hits = []; FS.idx = -1;
  }

  function fsPaint() {
    fsClear();
    var box = q('#messages');
    if (!box || !FS.q) return;
    FS.hits = fsFind(box, FS.q);
    // 从后往前包，避免前面的 splitText 打乱后面的偏移
    for (var i = FS.hits.length - 1; i >= 0; i--) {
      var h = FS.hits[i];
      var node = h.node;
      if (!node.parentNode) continue;
      if (h.start + h.len > node.textContent.length) continue;
      var tail = node.splitText(h.start + h.len);
      var mid = node.splitText(h.start);
      var mk = document.createElement('mark');
      mk.className = 'wb-hit';
      mid.parentNode.insertBefore(mk, mid);
      mk.appendChild(mid);
      FS.marks.push(mk);
    }
    FS.marks.reverse();
    fsCount();
  }

  function fsCount() {
    var el = FS.bar && q('.wb-fs-n', FS.bar);
    if (!el) return;
    if (!FS.hits.length) el.textContent = FS.q ? T('无匹配') : '';
    else el.textContent = (FS.idx + 1) + ' / ' + FS.hits.length;
  }

  function fsGo(step) {
    if (!FS.hits.length) return;
    var old = FS.marks[FS.idx];
    if (old) old.classList.remove('wb-hit-cur');
    FS.idx = (FS.idx + step + FS.hits.length) % FS.hits.length;
    var mk = FS.marks[FS.idx];
    if (!mk) return;
    mk.classList.add('wb-hit-cur');
    try { mk.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    catch (e) { mk.scrollIntoView(); }
    fsCount();
  }

  function fsBar() {
    if (FS.bar) return FS.bar;
    var bar = document.createElement('div');
    bar.id = 'wb-findbar';
    bar.className = 'wb-fs';
    bar.innerHTML =
      icon('i-search') +
      '<input class="wb-fs-input" type="text" spellcheck="false" placeholder="">' +
      '<span class="wb-fs-n"></span>' +
      '<button class="wb-fs-b" data-fs="-1" title="">' + icon('i-up') + '</button>' +
      '<button class="wb-fs-b" data-fs="1" title="">' + icon('i-down') + '</button>' +
      '<button class="wb-fs-b" data-fs="x" title="">' + icon('i-x') + '</button>';
    document.body.appendChild(bar);
    FS.input = q('.wb-fs-input', bar);
    FS.input.placeholder = T('在当前会话中查找…');
    var bs = qa('.wb-fs-b', bar);
    bs.forEach(function (b) {
      var a = b.getAttribute('data-fs');
      b.title = a === 'x' ? T('关闭') : (a === '1' ? T('下一个') : T('上一个'));
      b.addEventListener('click', function () {
        if (a === 'x') fsClose(); else fsGo(parseInt(a, 10));
      });
    });
    FS.input.addEventListener('input', debounce(function () {
      FS.q = FS.input.value.trim();
      FS.idx = -1;
      fsPaint();
      if (FS.hits.length) fsGo(1); else fsCount();
    }, 140));
    FS.input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); fsGo(e.shiftKey ? -1 : 1); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); fsGo(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); fsGo(-1); }
      else if (e.key === 'Escape') { e.preventDefault(); fsClose(); }
    });
    FS.bar = bar;
    return bar;
  }

  function fsOpen() {
    var bar = fsBar();
    bar.classList.add('wb-on');
    // 折叠区里的节点搜不到 —— 先全部展开，并冻结自动折叠直到关闭查找
    VZ.freeze = true;
    try { vzExpandAll(); } catch (e) {}
    setTimeout(function () {
      try { FS.input.focus(); FS.input.select(); } catch (e) {}
    }, 20);
    if (FS.q) fsPaint();
  }

  function fsClose() {
    if (FS.bar) FS.bar.classList.remove('wb-on');
    fsClear();
    VZ.freeze = false;
  }

  function fsToggle() { if (FS.bar && FS.bar.classList.contains('wb-on')) fsClose(); else fsOpen(); }

  /* ═══════════ 13. 会话导出（Markdown / JSON）═══════════
     服务端没有导出接口，这里直接复用 /api/sessions/<sid> 的原始消息数组，
     在前端拼装后走 Blob 下载，不碰后端。
     Markdown 保留 角色 / 思考 / 工具调用 / 正文 四个层次，方便直接贴进文档。
  */
  function exSlug(s) {
    return String(s || '').replace(/[\\/:*?"<>|\s]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'session';
  }

  function exDownload(name, text, mime) {
    try {
      var blob = new Blob([text], { type: (mime || 'text/plain') + ';charset=utf-8' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = name;
      document.body.appendChild(a); a.click();
      setTimeout(function () { a.remove(); URL.revokeObjectURL(url); }, 400);
      return true;
    } catch (e) { return false; }
  }

  function exFetch(sid) {
    return fetch('/api/sessions/' + encodeURIComponent(sid)).then(function (r) { return r.json(); });
  }

  function exTitle() {
    var t = q('#chat-title');
    var s = t ? (t.textContent || '').trim() : '';
    return (s && s !== '新对话') ? s : '';
  }

  function exStrip(c) {
    return String(c || '')
      .replace(/<runtime_context>[\s\S]*?<\/runtime_context>/g, '')
      .replace(/<memories>[\s\S]*?<\/memories>/g, '')
      .replace(/<skills>[\s\S]*?<\/skills>/g, '')
      .trim();
  }

  function exToMarkdown(d, sid) {
    var msgs = (d && Array.isArray(d.messages)) ? d.messages : [];
    var out = [];
    out.push('# ' + (exTitle() || T('对话')));
    out.push('');
    out.push('> ' + T('会话') + ' ID: `' + sid + '`  ');
    out.push('> ' + new Date().toLocaleString() + '  ');
    if (d && d.model) out.push('> model: `' + d.model + '`  ');
    out.push('');
    for (var i = 0; i < msgs.length; i++) {
      var m = msgs[i];
      var role = m.role;
      if (role === 'system') continue;
      var body = exStrip(m.content);
      var reason = (m.reasoning || '').trim();
      var calls = (m.metadata && Array.isArray(m.metadata.tool_calls)) ? m.metadata.tool_calls : null;
      if (role === 'tool') {
        var nm = m.tool_name || m.name || 'tool';
        if (!body) continue;
        out.push('    - ' + nm + ': `' + body.replace(/\n+/g, ' ⏎ ').slice(0, 300) + '`');
        continue;
      }
      if (role === 'user') {
        if (!body) continue;
        out.push('## ' + T('我'));
        out.push('');
        out.push(body);
        out.push('');
        continue;
      }
      if (role === 'assistant') {
        if (!body && !reason && !(calls && calls.length)) continue;
        out.push('## Scout');
        out.push('');
        if (reason) {
          out.push('<details><summary>' + T('思考') + '</summary>');
          out.push('');
          out.push(reason);
          out.push('');
          out.push('</details>');
          out.push('');
        }
        if (calls && calls.length) {
          for (var j = 0; j < calls.length; j++) {
            var fn = (calls[j].function && calls[j].function.name) || calls[j].name || 'tool';
            var args = (calls[j].function && calls[j].function.arguments) || calls[j].arguments || '';
            out.push('- ' + T('调用') + ' `' + fn + '`');
            if (args) out.push('  ```json\n  ' + String(args).slice(0, 2000) + '\n  ```');
          }
          out.push('');
        }
        if (body) { out.push(body); out.push(''); }
      }
    }
    return out.join('\n');
  }

  // 调试/自检钩子：?selftest=ex 可直接调 Markdown 生成
  try { window.__wbExportMd = exToMarkdown; } catch (e) {}

  function exportSession(kind) {
    var sid = null;
    try { sid = (typeof currentSessionId !== 'undefined' && currentSessionId) ? currentSessionId : null; } catch (e) {}
    if (!sid) { try { toast && toast.info && toast.info(T('当前没有可导出的会话')); } catch (e) {} return; }
    exFetch(sid).then(function (d) {
      if (!d || d.error) throw new Error('bad');
      var base = exSlug(exTitle() || sid) + '-' + new Date().toISOString().slice(0, 10);
      if (kind === 'json') {
        var payload = {
          session_id: sid, title: exTitle(), exported_at: new Date().toISOString(),
          model: d.model || null, messages: d.messages || []
        };
        exDownload(base + '.json', JSON.stringify(payload, null, 2), 'application/json');
      } else {
        exDownload(base + '.md', exToMarkdown(d, sid), 'text/markdown');
      }
      try { toast && toast.success && toast.success(T('已导出')); } catch (e) {}
    }).catch(function () {
      try { toast && toast.error && toast.error(T('导出失败')); } catch (e) {}
    });
  }

  /* ═══════════ 14. 加载态 ═══════════
     切会话 / 载入历史时界面毫无反馈（尤其会话很长时像卡死）。
     patch loadSession：开始 → 顶栏下方出现一条不确定进度条；结束 → 淡出。
     loadSessions（侧栏翻页）同理，在侧栏底部显示一个小 spinner。
  */
  var LD = { bar: null, depth: 0, timer: null };

  function ldBar() {
    if (LD.bar && LD.bar.isConnected) return LD.bar;
    var host = q('#chat-area') || q('#messages') || document.body;
    var bar = document.createElement('div');
    bar.id = 'wb-loading';
    bar.innerHTML = '<span class="wb-ld-fill"></span>';
    host.appendChild(bar);
    LD.bar = bar;
    return bar;
  }

  function ldStart() {
    LD.depth++;
    if (LD.timer) return;
    // 延迟 90ms 才显示：秒开的加载不闪一下反而更吵
    LD.timer = setTimeout(function () {
      LD.timer = null;
      var b = ldBar();
      if (b) b.classList.add('wb-on');
    }, 90);
  }

  function ldEnd() {
    LD.depth = Math.max(0, LD.depth - 1);
    if (LD.depth > 0) return;
    if (LD.timer) { clearTimeout(LD.timer); LD.timer = null; }
    var b = LD.bar;
    if (b) { b.classList.remove('wb-on'); setTimeout(function () { b.classList.remove('wb-fade'); }, 260); }
  }

  function patchLoading() {
    if (typeof window.loadSession === 'function' && !window.loadSession.__wbLd) {
      var old = window.loadSession;
      window.loadSession = function () {
        ldStart();
        var p;
        try { p = old.apply(this, arguments); } catch (e) { ldEnd(); throw e; }
        if (p && typeof p.finally === 'function') p.finally(ldEnd);
        else ldEnd();
        return p;
      };
      window.loadSession.__wbLd = 1;
    }
    if (typeof window.loadSessions === 'function' && !window.loadSessions.__wbLd) {
      var old2 = window.loadSessions;
      window.loadSessions = function () {
        var sb = q('#sidebar-scroll');
        if (sb) sb.classList.add('wb-sb-loading');
        var p2;
        try { p2 = old2.apply(this, arguments); } catch (e) { if (sb) sb.classList.remove('wb-sb-loading'); throw e; }
        var done = function () { var s = q('#sidebar-scroll'); if (s) s.classList.remove('wb-sb-loading'); };
        if (p2 && typeof p2.finally === 'function') p2.finally(done); else done();
        return p2;
      };
      window.loadSessions.__wbLd = 1;
    }
  }

  /* ═══════════ 15. 会话列表虚拟化 ═══════════
     与消息区同一套思路：侧栏会话上百条后，滚动和样式重算会明显掉帧。
     差异点：
       · 只折叠真正的会话行（带 [onclick]），分组标题（"今天"/"更早"）保持可见
       · 当前会话、置顶会话、鼠标悬停中的行不折叠，避免闪烁和误点
       · 侧栏是分页加载的，list.innerHTML 会整体重建 —— 每次更新前重新收集即可
  */
  var SV = {
    minCount: 60,      // 少于这个数量不启用
    margin: 420,       // 视口外多少 px 才折叠
    box: null, area: null, items: [], ticking: false
  };

  function svRowKey(n) { return n && n.getAttribute ? (n.getAttribute('onclick') || '') : ''; }

  function svNodeBusy(n) {
    if (!n || n.nodeType !== 1) return true;
    var ae = document.activeElement;
    if (ae && ae !== document.body && ae !== document.documentElement && n.contains(ae)) return true;
    try { if (n.matches(':hover')) return true; } catch (e) {}
    if (n.querySelector && n.querySelector('.wb-sess-pin, .wb-sess-pinned')) return true;
    var sid = null;
    try { sid = (typeof currentSessionId !== 'undefined' && currentSessionId) ? currentSessionId : null; } catch (e) {}
    if (sid && svRowKey(n).indexOf(sid) >= 0) return true;
    return false;
  }

  function svCollect() {
    var box = SV.box;
    if (!box) return;
    var kids = Array.prototype.slice.call(box.children);
    var next = [];
    for (var i = 0; i < kids.length; i++) {
      var n = kids[i];
      if (n.__wbSph) {
        var rec = n.__wbSRec;
        if (rec) { rec.ph = n; next.push(rec); }
      } else if (!svRowKey(n)) {
        next.push({ node: n, ph: null, h: 0, skip: 1 });   // 分组标题：只占位不折叠
      } else {
        var found = null;
        for (var j = 0; j < SV.items.length; j++) {
          if (SV.items[j].node === n) { found = SV.items[j]; break; }
        }
        if (!found) found = { node: n, ph: null, h: 0 };
        found.ph = null;
        next.push(found);
      }
    }
    SV.items = next;
  }

  function svCollapse(item) {
    var n = item.node;
    if (!n.parentNode || svNodeBusy(n)) return;
    item.h = n.offsetHeight || item.h || 34;
    var ph = document.createElement('div');
    ph.className = 'wb-sph';
    try { ph.style.marginTop = getComputedStyle(n).marginTop; } catch (e) {}
    ph.style.height = item.h + 'px';
    ph.__wbSph = 1;
    ph.__wbSRec = item;
    n.parentNode.replaceChild(ph, n);
    item.ph = ph;
  }

  function svExpand(item) {
    var ph = item.ph;
    if (!ph || !ph.parentNode) return;
    var prevTop = SV.area ? SV.area.scrollTop : 0;
    ph.parentNode.replaceChild(item.node, ph);
    item.ph = null;
    ph.__wbSRec = null;
    if (SV.area) SV.area.scrollTop = prevTop;
    // 展开后行上的置顶按钮等装饰可能已随重建丢失，补一次
    try { tagSessionRows(); } catch (e) {}
  }

  function svUpdate() {
    if (!SV.box || !SV.area) return;
    svCollect();
    var rows = 0, i;
    for (i = 0; i < SV.items.length; i++) if (!SV.items[i].skip) rows++;
    if (rows < SV.minCount) {
      // 数量回落：已折叠的全部还原，避免留下永远展开不了的占位
      for (i = 0; i < SV.items.length; i++) if (SV.items[i].ph) svExpand(SV.items[i]);
      return;
    }
    var ar = SV.area.getBoundingClientRect();
    var top = ar.top - SV.margin, bottom = ar.bottom + SV.margin;
    for (i = 0; i < SV.items.length; i++) {
      var it = SV.items[i];
      if (it.skip || it.ph) continue;
      var r = it.node.getBoundingClientRect();
      if (r.bottom < top || r.top > bottom) svCollapse(it);
    }
    for (i = 0; i < SV.items.length; i++) {
      var it2 = SV.items[i];
      if (!it2.ph) continue;
      var pr = it2.ph.getBoundingClientRect();
      if (pr.bottom > ar.top - 160 && pr.top < ar.bottom + 160) svExpand(it2);
    }
  }

  function initSessionVirt() {
    SV.box = q('#session-list');
    SV.area = q('#sidebar-scroll');
    if (!SV.box || !SV.area) return;
    var onScroll = function () {
      if (SV.ticking) return;
      SV.ticking = true;
      requestAnimationFrame(function () { SV.ticking = false; svUpdate(); });
    };
    SV.area.addEventListener('scroll', onScroll, { passive: true });
    // 侧栏是分页/全量重渲染的：列表一变就重新收集
    var mo = new MutationObserver(debounce(function () { svUpdate(); }, 120));
    mo.observe(SV.box, { childList: true });
    svUpdate();
  }

  /* ════════ 16. 顶栏下拉菜单：键盘化 ════════
     index.html 里三组功能菜单是纯 CSS `group-hover:visible`，容器常态 visibility:hidden，
     这类元素拿不到焦点 → Tab 直接跳过 → 键盘用户够不到里面的二级页面。
     这里保留 hover 直觉，同时补上：role=menu/menuitem、aria-expanded、↑↓/Home/End/Esc、
     点击外部关闭、一次只开一个。 */
  function initTopMenus() {
    function closeAll(except) {
      qa('header div.relative.group').forEach(function (g) {
        if (g !== except && g.__wbClose) g.__wbClose();
      });
    }
    qa('header div.relative.group').forEach(function (g, gi) {
      var tri = null, panel = null;
      var kids = Array.prototype.slice.call(g.children);
      for (var i = 0; i < kids.length; i++) {
        if (!tri && kids[i].tagName === 'BUTTON') tri = kids[i];
        else if (!panel && kids[i].classList && kids[i].classList.contains('absolute')) panel = kids[i];
      }
      if (!tri || !panel) return;
      panel.classList.add('wb-menu-panel');
      if (!panel.id) panel.id = 'wb-menu-panel-' + gi;
      panel.setAttribute('role', 'menu');
      panel.setAttribute('aria-hidden', 'true');
      tri.setAttribute('aria-haspopup', 'menu');
      tri.setAttribute('aria-expanded', 'false');
      tri.setAttribute('aria-controls', panel.id);
      var items = qa('button, a', panel);
      items.forEach(function (it) { it.setAttribute('role', 'menuitem'); it.tabIndex = -1; });
      if (!items.length) return;

      function isOpen() { return panel.classList.contains('wb-open'); }
      function open(focusIdx) {
        closeAll(g);
        panel.classList.add('wb-open');
        panel.setAttribute('aria-hidden', 'false');
        tri.setAttribute('aria-expanded', 'true');
        var idx = focusIdx === 'last' ? items.length - 1 : (focusIdx || 0);
        if (items[idx]) { try { items[idx].focus(); } catch (e) {} }
      }
      function close(refocus) {
        panel.classList.remove('wb-open');
        panel.setAttribute('aria-hidden', 'true');
        tri.setAttribute('aria-expanded', 'false');
        if (refocus) { try { tri.focus(); } catch (e) {} }
      }
      g.__wbClose = close;

      tri.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();
        if (isOpen()) close(); else open(0);
      });
      tri.addEventListener('keydown', function (e) {
        var k = e.key;
        if (k === 'ArrowDown' || k === 'Enter' || k === ' ') { e.preventDefault(); open(0); }
        else if (k === 'ArrowUp') { e.preventDefault(); open('last'); }
        else if (k === 'Escape') close();
      });
      panel.addEventListener('keydown', function (e) {
        var k = e.key, idx = items.indexOf(document.activeElement);
        if (idx < 0) idx = 0;
        if (k === 'ArrowDown') { e.preventDefault(); items[(idx + 1) % items.length].focus(); }
        else if (k === 'ArrowUp') { e.preventDefault(); items[(idx - 1 + items.length) % items.length].focus(); }
        else if (k === 'Home') { e.preventDefault(); items[0].focus(); }
        else if (k === 'End') { e.preventDefault(); items[items.length - 1].focus(); }
        else if (k === 'Escape') { e.preventDefault(); close(true); }
        else if (k === 'Tab') close();
      });
      panel.addEventListener('click', function (e) {
        if (e.target.closest && e.target.closest('[role="menuitem"]')) close();
      });
      panel.addEventListener('mouseleave', function () { if (!isOpen()) return; });
    });
    document.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('header div.relative.group')) return;
      closeAll();
    });
  }

  /* ════════ 17. 键位胶囊 <kbd> 统一渲染 ════════ */
  function kbdHtml(s) { return '<span class="wb-kbd">' + s + '</span>'; }
  function isMac() { return /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent || ''); }
  function modKey() { return isMac() ? '⌘' : 'Ctrl'; }
  /* Windows 中文输入法组字期间的 Enter/方向键属于 IME，不应被前端快捷键吃掉 */
  function imeOn(e) { return !!(e && (e.isComposing === true || e.keyCode === 229 || e.which === 229)); }
  /* 是否跑在桌面外壳（WinForms + WebView2）里 —— WebView2 会注入 window.chrome.webview */
  function isDesktopShell() {
    try { return !!(window.chrome && window.chrome.webview); } catch (e) { return false; }
  }

  function initPaletteBtn() {
    var head = q('header');
    if (!head || q('#wb-pal-btn')) return;
    var b = document.createElement('button');
    b.id = 'wb-pal-btn';
    b.className = 'hidden md:flex items-center gap-1.5 px-2 py-1.5 rounded-lg hover:bg-surface-3 text-ink-3 hover:text-ink-1 transition-colors text-xs';
    b.innerHTML = '<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z"/></svg>';
    b.insertAdjacentHTML('beforeend', kbdHtml(modKey() + ' K'));
    b.title = T('命令面板（搜索命令与会话）  ' + modKey() + ' + K');
    b.setAttribute('aria-label', T('命令面板'));
    b.addEventListener('click', openPalette);
    var anchor = q('#theme-toggle-btn');
    if (anchor && anchor.parentNode) head.insertBefore(b, anchor);
    else head.appendChild(b);
  }

  /* ════════ 18. 侧栏折叠（桌面端） ════════ */
  var SB_KEY = 'scout_sidebar_collapsed';
  function sbCollapsed() { return document.body.classList.contains('wb-sb-collapsed'); }
  function setSidebarCollapsed(v) {
    document.body.classList.toggle('wb-sb-collapsed', !!v);
    store(SB_KEY, v ? '1' : '0');
    var b = q('#wb-sb-toggle');
    if (b) { b.setAttribute('aria-pressed', v ? 'true' : 'false'); b.setAttribute('aria-label', T(v ? '展开侧栏' : '折叠侧栏')); }
  }
  function initSidebarCollapse() {
    var head = q('header');
    if (!head || q('#wb-sb-toggle')) return;
    var b = document.createElement('button');
    b.id = 'wb-sb-toggle';
    b.className = 'hidden md:inline-flex p-2 -ml-1 rounded-lg hover:bg-surface-3 text-ink-3 transition-colors';
    b.innerHTML = '<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 12h16M4 18h16"/></svg>';
    b.title = T('折叠 / 展开侧栏') + '  ' + modKey() + ' + B';
    b.addEventListener('click', function () { setSidebarCollapsed(!sbCollapsed()); });
    head.insertBefore(b, head.firstChild);
    if (store(SB_KEY) === '1') setSidebarCollapsed(true); else setSidebarCollapsed(false);

    document.addEventListener('keydown', function (e) {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      if (e.key !== 'b' && e.key !== 'B') return;
      var t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      e.preventDefault();
      setSidebarCollapsed(!sbCollapsed());
    });
  }

  /* ════════ 19. 斜杠命令（composer 内 / 触发） ════════ */
  var SLASH = [
    { c: 'new', d: '开一个新会话', run: safe('newChat') },
    { c: 'model', d: '切换本轮模型', run: function () { openSettingsModel(); } },
    { c: 'think', d: '切换到思考模式', run: function () { safe('selectMode')('thinking'); } },
    { c: 'fast', d: '切换到快速模式', run: function () { safe('selectMode')('fast'); } },
    { c: 'files', d: '打开产物面板', k: 'Alt + F', run: fpOpen },
    { c: 'find', d: '在当前会话中查找', k: modKey() + ' + ⇧ + F', run: fsOpen },
    { c: 'export', d: '导出会话为 Markdown', run: function () { exportSession('md'); } },
    { c: 'json', d: '导出会话为 JSON', run: function () { exportSession('json'); } },
    { c: 'memory', d: '打开记忆库', run: function () { safe('openPanel')('memory'); } },
    { c: 'knowledge', d: '打开知识库', run: function () { safe('openPanel')('knowledge'); } },
    { c: 'theme', d: '切换深 / 浅主题', run: safe('toggleTheme') },
    { c: 'lang', d: '切换界面语言', run: safe('toggleUILang') },
    { c: 'stop', d: '停止当前生成', k: 'Esc', run: safe('stopGeneration') },
    { c: 'help', d: '快捷键速查', run: showHelp }
  ];
  function openSettingsModel() { safe('openSettings')('model'); }

  var SH = null;   // slash menu 状态
  function initSlash() {
    var input = q('#input');
    var host = q('#composer') && q('#composer').parentNode;
    if (!input || !host) return;
    var menu = document.createElement('div');
    menu.id = 'wb-slash';
    menu.className = 'wb-slash hidden';
    menu.setAttribute('role', 'listbox');
    host.appendChild(menu);
    SH = { menu: menu, input: input, items: [], idx: 0, open: false };

    menu.addEventListener('mousedown', function (e) { e.preventDefault(); });
    menu.addEventListener('click', function (e) {
      var it = e.target.closest ? e.target.closest('.wb-slash-item') : null;
      if (!it) return;
      SH.idx = parseInt(it.dataset.i, 10) || 0;
      slashExec();
    });
    input.addEventListener('input', slashMaybe);
    input.addEventListener('blur', function () { setTimeout(slashClose, 120); });
    // 捕获阶段抢在「↑↓ 历史」之前处理，并且用 stopPropagation 阻止后续监听器
    document.addEventListener('keydown', function (e) {
      if (!SH.open) return;
      if (imeOn(e)) return;            // 中文输入法组字中的 Enter/↑↓ 归输入法
      var t = e.target;
      if (t !== input) return;
      var k = e.key;
      if (k === 'ArrowDown' || k === 'ArrowUp' || k === 'Enter' || k === 'Tab' || k === 'Escape') {
        e.preventDefault(); e.stopPropagation();
        if (k === 'ArrowDown') slashMove(1);
        else if (k === 'ArrowUp') slashMove(-1);
        else if (k === 'Tab') slashMove(e.shiftKey ? -1 : 1);
        else if (k === 'Enter') slashExec();
        else slashClose();
      }
    }, true);
  }
  function slashMaybe() {
    var v = SH.input.value;
    var m = /^\/([a-zA-Z]*)$/.exec(v);
    if (!m) { slashClose(); return; }
    var kw = m[1].toLowerCase();
    var hits = SLASH.filter(function (s) { return s.c.indexOf(kw) === 0; });
    if (!hits.length) { slashClose(); return; }
    SH.items = hits;
    SH.menu.innerHTML = hits.map(function (s, i) {
      return '<div class="wb-slash-item' + (i === 0 ? ' wb-sel' : '') + '" data-i="' + i + '" role="option" aria-selected="' + (i === 0) + '">' +
        '<b>/' + s.c + '</b><span>' + escHtml(T(s.d)) + '</span>' +
        (s.k ? kbdHtml(s.k) : '') + '</div>';
    }).join('');
    SH.idx = 0;
    SH.menu.classList.remove('hidden');
    SH.open = true;
  }
  function slashMove(d) {
    if (!SH.items.length) return;
    SH.idx = (SH.idx + d + SH.items.length) % SH.items.length;
    qa('.wb-slash-item', SH.menu).forEach(function (el, i) {
      el.classList.toggle('wb-sel', i === SH.idx);
      el.setAttribute('aria-selected', i === SH.idx ? 'true' : 'false');
    });
  }
  function slashExec() {
    var cmd = SH.items[SH.idx];
    slashClose();
    if (!cmd) return;
    SH.input.value = '';
    try { SH.input.style.height = 'auto'; } catch (e) {}
    setTimeout(function () { try { cmd.run(); } catch (e) {} }, 0);
  }
  function slashClose() {
    if (!SH) return;
    SH.menu.classList.add('hidden');
    SH.open = false; SH.items = []; SH.idx = 0;
  }

  /* 快捷键速查浮层（同样作为 /help 的落点，解决「功能看不见」） */
  function showHelp() {
    var old = q('#wb-help');
    if (old) old.remove();
    var wrap = document.createElement('div');
    wrap.id = 'wb-help';
    wrap.className = 'wb-help-wrap';
    var rows = [
      [modKey() + ' K', T('命令面板')],
      [modKey() + ' ⇧ F', T('在当前会话中查找')],
      [modKey() + ' B', T('折叠 / 展开侧栏')],
      [modKey() + ' ↑', T('调出上一条输入')],
      ['Alt + F', T('打开产物面板')],
      ['Esc', T('停止生成 / 关闭浮层')],
      ['Enter · ⇧+Enter', T('发送 · 换行')],
      ['/', T('斜杠命令')],
      ['@', T('引用文件')]
    ];
    wrap.innerHTML = '<div class="wb-help-scrim" data-close="1"></div>' +
      '<div class="wb-help-card">' +
        '<div class="wb-help-head"><b>' + T('快捷键与斜杠命令') + '</b>' +
          '<button data-close="1" aria-label="' + T('关闭') + '">' + icon('i-x') + '</button></div>' +
        '<div class="wb-help-body">' +
          '<div class="wb-help-sec">' + rows.map(function (r) {
            return '<div class="wb-help-row">' + kbdHtml(r[0]) + '<span>' + escHtml(r[1]) + '</span></div>';
          }).join('') + '</div>' +
          '<div class="wb-help-sec"><div class="wb-help-title">' + T('/ 斜杠命令') + '</div>' +
            SLASH.map(function (s) {
              return '<div class="wb-help-row"><code>/' + s.c + '</code><span>' + escHtml(T(s.d)) + '</span></div>';
            }).join('') +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(wrap);
    wrap.addEventListener('mousedown', function (e) {
      if (e.target.getAttribute && e.target.getAttribute('data-close')) {
        try { a11yClose(wrap); } catch (e) {}
        wrap.remove();
      }
    });
    try { a11yOpen(q('.wb-help-card', wrap) || wrap, T('快捷键与斜杠命令')); } catch (e) {}
    var closeBtn = q('.wb-help-head button', wrap);
    if (closeBtn) closeBtn.addEventListener('click', function () {
      try { a11yClose(wrap); } catch (e) {}
      wrap.remove();
    });
    document.addEventListener('keydown', function esc(e) {
      if (e.key !== 'Escape') return;
      if (!wrap.isConnected) { document.removeEventListener('keydown', esc); return; }
      if (openPalette && pal && pal.classList.contains('wb-on')) return;
      try { a11yClose(wrap); } catch (er) {}
      wrap.remove();
      document.removeEventListener('keydown', esc);
    });
  }

  /* ════════ 20. 上下文余量 ════════ */
  function ctxTokens() {
    var box = q('#messages');
    var txt = box ? (box.innerText || box.textContent || '') : '';
    var cjk = (txt.match(/[\u4e00-\u9fff\u3040-\u30ff]/g) || []).length;
    var rest = txt.replace(/[\u4e00-\u9fff\u3040-\u30ff]/g, ' ');
    var words = (rest.match(/[A-Za-z0-9_]+/g) || []).length;
    return Math.round(cjk / 1.4 + words / 0.75 + 1500);
  }
  function fmtK(n) { return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n); }
  var ctxWarned = false;
  // 圆环几何常量：viewBox 20x20，r=8，周长 = 2πr ≈ 50.2655
  var WB_CTX_CIRC = 2 * Math.PI * 8;
  function updCtx() {
    var el = q('#wb-ctx');
    if (!el) return;
    var lmt = parseInt(store('scout_ctx_limit') || '', 10);
    if (!lmt || lmt < 4096) lmt = 128000;
    var used = ctxTokens();
    var pct = Math.max(0, Math.min(1, used / lmt));
    var lab = q('.wb-ctx-label', el), fg = q('.wb-ctx-ring-fg', el);
    if (lab) lab.textContent = T('上下文') + ' ' + fmtK(used) + ' / ' + fmtK(lmt);
    if (fg) fg.style.strokeDashoffset = String(WB_CTX_CIRC * (1 - pct));
    el.classList.toggle('wb-ctx-warn', pct >= 0.6 && pct < 0.85);
    el.classList.toggle('wb-ctx-hot', pct >= 0.85);
    el.title = T('本会话上下文估算占用 {n}%，接近上限时建议新开会话', { n: Math.round(pct * 100) });
    if (pct >= 0.85 && !ctxWarned) {
      ctxWarned = true;
      try { if (typeof window.showToast === 'function') window.showToast(T('上下文接近上限，建议新开会话'), 'warn'); } catch (e) {}
    }
    if (pct < 0.8) ctxWarned = false;
  }
  function initCtxBar() {
    var composer = q('#composer');
    if (!composer) return;
    if (q('#wb-ctx')) { updCtx(); return; }
    // ★ 位置：模型徽章那一栏，放在模型按钮前面（模型名左侧）。
    //   结构: <div.flex 栏> <div.relative> <button#chat-model-btn> </div> <button#send-btn> </div>
    //   #chat-model-btn 的直接父是 div.relative（它是 flex 栏的直接子），
    //   所以必须以 div.relative 作为 insertBefore 锚点 —— 传 modelBtn 会因非直接子抛 NotFoundError。
    var modelBtn = q('#chat-model-btn');
    var ref = modelBtn ? modelBtn.parentElement : null;   // div.relative
    var row = (ref && ref.parentElement) || (modelBtn ? modelBtn.closest('div.flex') : null);
    if (!row) row = composer;
    var el = document.createElement('div');
    el.id = 'wb-ctx';
    el.className = 'wb-ctx';
    el.innerHTML =
      '<svg class="wb-ctx-ring" viewBox="0 0 20 20" aria-hidden="true">' +
        '<circle class="wb-ctx-ring-bg" cx="10" cy="10" r="8"></circle>' +
        '<circle class="wb-ctx-ring-fg" cx="10" cy="10" r="8" ' +
          'stroke-dasharray="' + WB_CTX_CIRC.toFixed(2) + '" ' +
          'stroke-dashoffset="' + WB_CTX_CIRC.toFixed(2) + '"></circle>' +
      '</svg>' +
      '<span class="wb-ctx-label"></span>';
    // 插到 div.relative（模型按钮包裹层）前面 → 圆环出现在模型徽章左侧
    if (ref && ref.parentNode === row) {
      row.insertBefore(el, ref);
    } else if (ref) {
      ref.parentNode.insertBefore(el, ref);
    } else {
      row.appendChild(el);
    }
    updCtx();
    var mo = new MutationObserver(debounce(updCtx, 600));
    mo.observe(q('#messages') || document.body, { childList: true, subtree: true, characterData: true });
  }

  /* ════════ 21. 运行中排队下一条 ════════
     原来：运行中按 Enter 被 send() 静默 return，用户不知道发生了什么。
     现在：收进队列 + 显示「已排队」提示 + 本轮结束后自动发出。 */
  function runBusy() {
    var s = q('#stop-btn');
    return !!(s && !s.classList.contains('hidden'));
  }
  function initQueue() {
    var input = q('#input');
    var composer = q('#composer');
    if (!input || !composer) return;
    var queue = [];
    var chip = document.createElement('div');
    chip.id = 'wb-queue-chip';
    chip.className = 'wb-queue-chip hidden';
    chip.dataset.count = '0';
    composer.parentNode.insertBefore(chip, composer);

    function paint() {
      if (!queue.length) { chip.classList.add('hidden'); chip.dataset.count = '0'; return; }
      chip.classList.remove('hidden');
      chip.dataset.count = String(queue.length);
      chip.innerHTML = '<span>' + T('已排队 {n} 条 · 本轮结束后自动发送', { n: queue.length }) + '</span>' +
        '<button type="button" class="wb-queue-cancel">' + T('取消') + '</button>';
      var b = q('.wb-queue-cancel', chip);
      if (b) b.addEventListener('click', function () { queue.length = 0; paint(); });
    }

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
      if (e.target !== input) return;
      if (!runBusy()) return;
      var txt = input.value.trim();
      e.preventDefault(); e.stopPropagation();
      if (!txt) {
        try { if (typeof window.showToast === 'function') window.showToast(T('正在生成中，先别急'), 'info'); } catch (er) {}
        return;
      }
      queue.push(txt);
      input.value = '';
      try { input.style.height = 'auto'; } catch (er) {}
      paint();
    }, true);

    var mo = new MutationObserver(debounce(function () {
      if (runBusy() || !queue.length) return;
      var txt = queue.shift();
      paint();
      if (!txt) return;
      setTimeout(function () {
        if (runBusy()) { queue.unshift(txt); paint(); return; }
        input.value = txt;
        try { if (typeof window.send === 'function') window.send(); } catch (e) {}
      }, 450);
    }, 260));
    var stopBtn = q('#stop-btn');
    if (stopBtn) mo.observe(stopBtn, { attributes: true, attributeFilter: ['class'] });
  }

  /* ════════ 22. 代码高亮：按需加载（首屏 ~124KB 不再阻塞） ════════
     出现第一个代码块才拉 highlight.min.js；加载完成后回填已存在的代码块，
     后续新增由 MutationObserver 接管。marked 的 highlight 回调在 hljs 未就绪时
     try/catch 返回原文，加载完成后这里再把颜色补上。 */
  var hlLoading = false;
  function hlPaint() {
    var hl = window.hljs;
    if (!hl || !hl.highlightElement) return false;
    qa('pre code:not([data-wb-hl])').forEach(function (el) {
      if (el.closest('#composer')) return;
      el.setAttribute('data-wb-hl', '1');
      try { hl.highlightElement(el); } catch (e) {}
    });
    return true;
  }
  function hlEnsure() {
    if (window.hljs || hlLoading) { hlPaint(); return; }
    var has = q('pre code');
    if (!has) return;
    hlLoading = true;
    var s = document.createElement('script');
    s.src = '/static/vendor/highlight.min.js';
    s.async = true;
    s.onload = function () {
      hlLoading = false;
      // 加载完成那一刻可能还有块没渲染完（流式输出），补两次延迟回填
      hlPaint();
      setTimeout(hlPaint, 300);
      setTimeout(hlPaint, 1200);
    };
    s.onerror = function () { hlLoading = false; };
    document.head.appendChild(s);
  }
  function initLazyHighlight() {
    hlEnsure();
    var mo = new MutationObserver(debounce(function () {
      hlEnsure(); hlPaint();
    }, 220));
    mo.observe(document.body, { childList: true, subtree: true });
  }

  /* ════════ 23. 文件改动 diff 审阅 ════════
     后端工具事件不带 diff 字段，所以这里用「会话内版本快照」近似：
     同一文件第 2 次及以上出现在附件里时，抓当前内容并与上一次快照做行级 diff。
     纯前端、不改后端；二进制/超大文本自动跳过。 */
  var DIFFP = {}, diffHooked = false;
  function isTextName(name) {
    return !/\.(png|jpe?g|gif|webp|bmp|svg|ico|zip|rar|7z|pdf|xlsx?|docx?|pptx?|mp4|mp3|exe|dll|woff2?|ttf)$/i.test(name || '');
  }
  function lcs(a, b) {
    // 行级 LCS；超过 1200 行退化为「全删+全加」摘要，避免卡主线程
    if (a.length > 1200 || b.length > 1200) {
      return a.map(function (s) { return { t: '-', s: s }; })
        .concat(b.map(function (s) { return { t: '+', s: s }; }));
    }
    var m = a.length, n = b.length, i, j;
    var dp = [new Array(n + 1).fill(0)];
    for (i = 1; i <= m; i++) {
      dp[i] = [0];
      for (j = 1; j <= n; j++) {
        dp[i][j] = a[i - 1] === b[j - 1]
          ? dp[i - 1][j - 1] + 1
          : Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
    var out = [];
    i = m; j = n;
    while (i > 0 && j > 0) {
      if (a[i - 1] === b[j - 1]) { out.unshift({ t: ' ', s: a[i - 1] }); i--; j--; }
      else if (dp[i - 1][j] >= dp[i][j - 1]) { out.unshift({ t: '-', s: a[--i] }); }
      else { out.unshift({ t: '+', s: b[--j] }); }
    }
    while (i > 0) out.unshift({ t: '-', s: a[--i] });
    while (j > 0) out.unshift({ t: '+', s: b[--j] });
    return out;
  }
  // 自检钩子
  window.__wbLcs = lcs;
  window.__wbDiffMap = DIFFP;
  function diffStat(d) {
    var add = 0, del = 0;
    for (var i = 0; i < d.length; i++) { if (d[i].t === '+') add++; else if (d[i].t === '-') del++; }
    return { add: add, del: del };
  }
  function nearestDiffCard(path) {
    var cards = qa('.file-attachment[data-wb-diff-path]');
    for (var i = cards.length - 1; i >= 0; i--) {
      if (cards[i].getAttribute('data-wb-diff-path') === path) return cards[i];
    }
    return null;
  }
  function diffRender(card, name, oldC, newC) {
    if (!card || card.querySelector('.wb-diff')) return;
    var d = lcs(oldC.split('\n'), newC.split('\n'));
    var st = diffStat(d);
    var wrap = document.createElement('div');
    wrap.className = 'wb-diff';
    wrap.innerHTML =
      '<button class="wb-diff-toggle" aria-expanded="false">改动' +
        '<span class="wb-diff-add">+' + st.add + '</span>' +
        '<span class="wb-diff-del">-' + st.del + '</span></button>' +
      '<div class="wb-diff-body hidden"></div>';
    var btn = q('.wb-diff-toggle', wrap), body = q('.wb-diff-body', wrap);
    var shown = false;
    btn.addEventListener('click', function () {
      if (!shown) {
        body.innerHTML = '<pre>' + d.slice(0, 800).map(function (l) {
          return '<span class="wb-dl-' + (l.t === '+' ? 'add' : l.t === '-' ? 'del' : 'eq') + '">' +
            (l.t === ' ' ? '  ' : l.t + ' ') + escHtml(l.s) + '</span>';
        }).join('\n') + '</pre>';
        shown = true;
      }
      var hidden = body.classList.toggle('hidden');
      btn.setAttribute('aria-expanded', hidden ? 'false' : 'true');
    });
    card.appendChild(wrap);
  }
  function diffRecord(fileData) {
    if (!fileData || !fileData.file_path) return;
    var path = String(fileData.file_path).replace(/\\/g, '/');
    var name = fileData.file_name || path.split('/').pop();
    if (!isTextName(name)) return;
    if ((fileData.file_size || 0) > 400000) return;
    var prev = DIFFP[path];
    if (!prev && Object.keys(DIFFP).length > 40) return;   // 上限保护
    try {
      fetch(fpDl(path)).then(function (r) { return r.text(); }).then(function (txt) {
        if (!txt || txt.indexOf('\u0000') >= 0) return;
        if (prev && prev.content !== txt) {
          var card = nearestDiffCard(path) || q('.file-attachment:last-of-type');
          diffRender(card, name, prev.content, txt);
        }
        DIFFP[path] = { name: name, content: txt };
      }).catch(function () {});
    } catch (e) {}
  }
  function hookDiffReview() {
    if (diffHooked) return;
    var proto = (typeof AgentBubble !== 'undefined' ? AgentBubble : window.AgentBubble);
    if (!proto || !proto.prototype) return;
    var orig = proto.prototype.addFileAttachment;
    if (typeof orig !== 'function') return;
    diffHooked = true;
    proto.prototype.addFileAttachment = function (fileData) {
      var before = document.querySelectorAll('.file-attachment').length;
      var ret = orig.apply(this, arguments);
      try {
        var all = document.querySelectorAll('.file-attachment');
        for (var i = Math.min(before, all.length); i < all.length; i++) {
          all[i].setAttribute('data-wb-diff-path', String(fileData.file_path || '').replace(/\\/g, '/'));
        }
        diffRecord(fileData);
      } catch (e) {}
      return ret;
    };
  }

  /* ════════ 24. 重复工具调用合并 ════════
     同一工具连着跑 3 次以上时，把前面几张卡收起来，留最后一张并标 ×N，避免刷屏。 */
  function runRepeatMerge() {
    var cards = qa('.am-tool', q('#messages'));
    if (cards.length < 3) return;
    var run = [], doneRuns = 0;
    function keyOf(el) {
      var s = el.className.replace(/wb-merged|is-\w+|wb-last/g, '');
      var txt = (el.textContent || '').slice(0, 40);
      return s + '|' + txt;
    }
    cards.forEach(function (el) { run.push({ el: el, k: keyOf(el) }); });
    var i = 0;
    while (i < run.length) {
      var j = i;
      while (j + 1 < run.length && run[j + 1].k === run[i].k) j++;
      var cnt = j - i + 1;
      if (cnt >= 3 && doneRuns < 12) {
        for (var m = i; m < j; m++) {
          run[m].el.classList.add('wb-tool-merged');
          run[m].el.setAttribute('data-wb-merge-hidden', '1');
        }
        var last = run[j].el;
        if (!last.querySelector('.wb-merge-badge')) {
          var b = document.createElement('span');
          b.className = 'wb-merge-badge';
          // 不显示次数（×N）：只给一个静默的省略号标记，悬停可见说明
          b.textContent = '···';
          b.title = T('已折叠 {n} 次相同调用', { n: cnt });
          last.appendChild(b);
        }
        doneRuns++;
      }
      i = j + 1;
    }
  }
  function initToolMerge() {
    window.__wbMerge = runRepeatMerge;   // 自检钩子
    runRepeatMerge();
    var mo = new MutationObserver(debounce(runRepeatMerge, 300));
    mo.observe(q('#messages') || document.body, { childList: true, subtree: true });
  }

  /* ═══════════ 挂载 ═══════════ */
  function boot() {
    initVirtualizer();
    initEmoji();
    patchLoading();
    initSessionVirt();
    fpHook();
    fpBtnInject();
    initCrumbs();
    tagActs();
    bindInput();
    bindSettingsKeys();
    enrichWelcome();
    tagSessionRows();
    hookBubbleFinalize();
    hookScrollPause();
    loadSessionsForPalette();
    watchSessionSwitch();
    ensureCounter();
    updCount();
    initTopMenus();
    initPaletteBtn();
    initSidebarCollapse();
    initSlash();
    initCtxBar();
    initQueue();
    initLazyHighlight();
    initToolMerge();
    hookDiffReview();

    var mo = new MutationObserver(debounce(function () {
      tagActs();
      tagSessionRows();
    }, 180));
    var watchees = [q('#messages'), q('#session-list')];
    watchees.forEach(function (t) { if (t) mo.observe(t, { childList: true, subtree: true }); });

    document.addEventListener('keydown', function (e) {
      var k = (e.key || '').toLowerCase();
      if (k === 'k' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        if (paletteOpen()) closePalette(); else openPalette();
        return;
      }
      if (e.key === 'Escape' && paletteOpen()) closePalette();
      if (e.key === 'Escape' && crZooming()) { crExit(); return; }
      // Ctrl/Cmd + Shift + F：会话内查找（避开浏览器原生 Ctrl+F）
      // ★ 桌面外壳（WebView2）已关闭浏览器加速键，那里 Ctrl+F 不会再被内核吃掉，
      //   直接接管给会话内查找；网页版仍保留 Ctrl+Shift+F，不抢浏览器原生查找。
      if (k === 'f' && (e.ctrlKey || e.metaKey)) {
        if (e.shiftKey || isDesktopShell()) { e.preventDefault(); fsToggle(); return; }
      }
      if (e.altKey && (k === 'f')) {
        e.preventDefault();
        if (FP.open) fpClose(); else fpOpen();
      }
      if (e.key === 'Escape' && FP.open) fpClose();
      // 焦点陷阱：Tab 只在当前打开的浮层里循环
      if (e.key === 'Tab') {
        var dlg = null;
        if (paletteOpen()) dlg = pal;
        else if (crZooming()) dlg = CR.cur;
        else if (FP.open && FP.drawer) dlg = FP.drawer.querySelector('.wb-panel');
        if (dlg) a11yTrap(dlg, e);
      }
    });

    // 调试/截图钩子：URL 带 palette=1 直接展开命令面板
    if (/[?&]palette=1/.test(location.search)) setTimeout(openPalette, 300);
    // 调试/截图钩子：?files=1 直接展开产物面板
    if (/[?&]files=1/.test(location.search)) setTimeout(function () {
      // 等历史渲染（addFileAttachment 收集）完成后再开
      setTimeout(fpOpen, 600);
    }, 200);

    // 调试钩子：?vzscroll=0..1 把聊天区滚到指定比例（验证折叠/还原）
    var mvs = location.search.match(/[?&]vzscroll=([\d.]+)/);
    if (mvs) setTimeout(function () {
      var a = VZ.area;
      if (!a) return;
      a.scrollTop = (a.scrollHeight - a.clientHeight) * Math.min(1, Math.max(0, parseFloat(mvs[1])));
    }, 1400);

    // 调试钩子：?diag=1 输出虚拟化状态到 #__wbdiag（headless dump 可读）
    if (/[?&]diag=1/.test(location.search)) {
      setInterval(function () {
        var d = q('#__wbdiag');
        if (!d) {
          d = document.createElement('div');
          d.id = '__wbdiag';
          d.style.display = 'none';
          document.body.appendChild(d);
        }
        var n = 0;
        for (var i = 0; i < VZ.items.length; i++) if (VZ.items[i].ph) n++;
        d.textContent = 'children=' + (VZ.box ? VZ.box.children.length : -1)
          + ' items=' + VZ.items.length + ' collapsed=' + n
          + ' ph=' + document.querySelectorAll('.wb-ph').length
          + ' st=' + Math.round(VZ.area ? VZ.area.scrollTop : -1)
          + ' near=' + (VZ.lastRun ? VZ.lastRun.near : '-');
      }, 700);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  window.WBShell = {
    version: '1.0.0',
    openPalette: openPalette,
    closePalette: closePalette
  };
})();
