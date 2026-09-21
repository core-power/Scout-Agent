/* ============================================================
   Scout Agent Motion Layer v2 —— 「运行轨道 Run Rail」
   ------------------------------------------------------------
   v1 只回答了"有没有动效"，v2 回答"用户看不看得懂这次运行"。

   六件事：
     1. Run Head    气泡顶部摘要条：第几步 / 已用多久 / 并行几项 / 步骤点阵
     2. Run Rail    左侧轨道线 + 节点圆点 + 焦点衰减（只有当前步是主角）
     3. 动词化      工具卡 = 「动词 + 宾语」，完成后 宾语 → 结果摘要
     4. 三档活着    <2.5s 安静 · 2.5s 起进度条 · 10s 起转琥珀（不臆造 ETA）
     5. 实时输出    行数徽章 + 折叠 + 底部渐隐，暗示"还在往下长"
     6. 收束        整轮结束后运行区自动折叠成一行，让答案成为主角

   实现方式仍是 monkey-patch AgentBubble.prototype，不动 index.html 业务代码；
   删掉 <script> 引用即完整回滚。

   性能：渲染帧合并（rAF）+ 长文本节流；计时用单一 rAF 循环 + 注册表，
        取代 v1 的 setInterval 全量 querySelectorAll。
   ============================================================ */

(function () {
  'use strict';

  if (typeof AgentBubble === 'undefined') {
    console.warn('[agent-motion] AgentBubble 未定义，动态层未加载');
    return;
  }

  var P = AgentBubble.prototype;

  var REDUCED = false;
  try { REDUCED = !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (e) {}

  var LONG_MS  = 2500;   // 超过此时长才显示进度条 / 耗时，避免短任务闪一下
  var SLOW_MS  = 10000;  // 超过此时长转琥珀
  var CLIP_LINES = 28;   // 实时输出超过此行数开始折叠
  var REASON_TAIL = 4000;

  // ── SVG ───────────────────────────────────────────────────
  var RING = '<svg viewBox="0 0 24 24" class="am-spin-svg" aria-hidden="true" style="width:12px;height:12px;display:block">' +
             '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" ' +
             'stroke-width="2.6" stroke-linecap="round"/></svg>';
  function ic(path, cls) {
    return '<svg viewBox="0 0 24 24" class="' + (cls || '') + '" fill="none" stroke="currentColor" ' +
           'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + path + '</svg>';
  }
  var CHECK = ic('<path d="M5 13l4 4L19 7"/>');
  var CROSS = ic('<path d="M6 6l12 12M18 6L6 18"/>');
  var BAN   = ic('<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/>');
  var CHEV  = ic('<path d="M9 6l6 6-6 6"/>', 'am-chev');

  // 状态图标直接接管 index.html 的 svg.tool-icon（保留 ml-auto，布局不乱）
  var STATE_SVG = {
    running: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/>',
    done:    '<path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/>',
    error:   '<path stroke-linecap="round" stroke-linejoin="round" d="M6 6l12 12M18 6L6 18"/>',
    cancel:  '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/>' +
             '<path stroke-linecap="round" d="M5.6 5.6l12.8 12.8"/>'
  };
  var STATE_CLS = { running: 'am-spin', done: 'am-done', error: 'am-fail', cancel: 'am-cancel' };

  function setStateIcon(card, state) {
    if (!card) return;
    var svg = card.querySelector('svg.tool-icon');
    if (!svg) return;
    svg.classList.remove('am-spin', 'am-done', 'am-fail', 'am-cancel', 'animate-spin');
    svg.classList.add(STATE_CLS[state] || '');
    svg.innerHTML = STATE_SVG[state] || STATE_SVG.running;
    if (state !== 'running') svg.classList.add('am-pop');
  }

  // ── 工具图标（线性、无 emoji）─────────────────────────────
  function ico(path) {
    return '<svg viewBox="0 0 24 24" class="am-tool-icon" fill="none" stroke="currentColor" ' +
           'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + path + '</svg>';
  }
  var TOOL_SVG = {
    web_search: ico('<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>'),
    web_fetch:  ico('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 3 2.5 15 0 18M12 3c-2.5 3-2.5 15 0 18"/>'),
    shell:      ico('<path d="M5 5l5 6-5 6"/><path d="M13 17h6"/>'),
    files:      ico('<path d="M4 7a2 2 0 012-2h4l2 2h6a2 2 0 012 2v8a2 2 0 01-2 2H6a2 2 0 01-2-2z"/>'),
    list:       ico('<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="4" cy="6" r=".9"/><circle cx="4" cy="12" r=".9"/><circle cx="4" cy="18" r=".9"/>'),
    memory:     ico('<path d="M9 4a5 5 0 00-5 5 4 4 0 001 7 4 4 0 006-3V4z"/><path d="M15 4a5 5 0 015 5 4 4 0 01-1 7 4 4 0 01-6-3V4z"/>'),
    code_exec:  ico('<path d="M9 8l-4 4 4 4M15 8l4 4-4 4"/>'),
    delegate:   ico('<path d="M4 12h10"/><path d="M11 8l4 4-4 4"/><path d="M18 5v14"/>'),
    image_gen:  ico('<rect x="4" y="5" width="16" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M5 17l4-4 4 4 2-2 4 3"/>'),
    vision:     ico('<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="2.6"/>'),
    scheduler:  ico('<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>'),
    browser:    ico('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 9h18"/><circle cx="6.5" cy="7" r=".6"/><circle cx="9" cy="7" r=".6"/>'),
    knowledge:  ico('<path d="M4 6a2 2 0 012-2h12v16H6a2 2 0 01-2-2z"/><path d="M8 6v12"/>'),
    mcp:        ico('<circle cx="8" cy="8" r="3.4"/><circle cx="16" cy="16" r="3.4"/><path d="M10.6 10.6l2.8 2.8"/>'),
    _default:   ico('<rect x="4" y="9" width="4" height="10" rx="1"/><rect x="10" y="4" width="4" height="15" rx="1"/><rect x="16" y="12" width="4" height="7" rx="1"/>')
  };
  function svgToolIcon(name) {
    for (var k in TOOL_SVG) {
      if (k !== '_default' && String(name).indexOf(k) >= 0) return TOOL_SVG[k];
    }
    return TOOL_SVG._default;
  }
  if (typeof window.getToolIcon === 'function') window.getToolIcon = svgToolIcon;

  // ── 动词化：工具名 → 人话 ─────────────────────────────────
  var VERBS = [
    [/web_search|search_web|search_engine|baidu|google/i, '搜索'],
    [/web_fetch|fetch_url|browse|visit|crawl/i,            '抓取网页'],
    [/grep|ripgrep|rg_search|find_in/i,                    '搜索代码'],
    [/read_file|file_read|read|cat|view_file|open_file/i,  '读取文件'],
    [/write_file|create_file|edit_file|patch|apply_diff|replace|str_replace/i, '编辑文件'],
    [/list_dir|ls|glob|find_files|tree|walk/i,             '列目录'],
    [/shell|bash|terminal|exec_command|run_command|cmd|command|run_/i, '执行命令'],
    [/python|code_exec|run_code|execute_code|calc|eval/i,  '执行代码'],
    [/memory|remember|recall|memorize/i,                   '读写记忆'],
    [/delegate|spawn|parallel_delegate|sub_agent/i,        '分派子任务'],
    [/image_gen|draw|paint|generate_image|text2img/i,      '生成图片'],
    [/vision|ocr|screenshot|look_at/i,                     '看图'],
    [/schedule|cron|reminder|remind/i,                     '设置计划'],
    [/browser|click|navigate|fill_form/i,                  '操作浏览器'],
    [/knowledge|rag|retrieve|doc_search/i,                 '检索知识库'],
    [/mcp/i,                                               '调用 MCP'],
    [/todo|plan|planning/i,                                '规划任务'],
    [/delete|remove|rm_/i,                                 '删除'],
    [/move|rename|mv_/i,                                   '移动'],
    [/upload|download/i,                                   '传输文件']
  ];
  function verbOf(name) {
    name = String(name || 'tool');
    for (var i = 0; i < VERBS.length; i++) {
      if (VERBS[i][0].test(name)) return VERBS[i][1];
    }
    return name.replace(/[_-]+/g, ' ');
  }

  // ── 宾语：从参数里挑出"对谁做" ───────────────────────────
  var OBJ_KEYS = ['path', 'file_path', 'file', 'filename', 'filepath', 'query', 'q',
                  'url', 'uri', 'command', 'cmd', 'script', 'pattern', 'glob', 'code',
                  'prompt', 'text', 'name', 'target', 'dir', 'directory', 'keywords',
                  'search', 'expr', 'content', 'title', 'id', 'topic'];
  function shorten(s) {
    s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
    if (!s) return '';
    if (/^https?:\/\//i.test(s)) s = s.replace(/^https?:\/\//i, '');
    if (!/\s/.test(s)) {
      var sep = s.indexOf('\\') >= 0 ? '\\' : (s.indexOf('/') >= 0 ? '/' : '');
      if (sep) {
        var parts = s.split(sep);
        if (parts.length > 3) s = '…' + sep + parts.slice(-2).join(sep);
      }
    }
    return s.length > 34 ? s.slice(0, 34) + '…' : s;
  }
  function objectOf(args) {
    if (!args || typeof args !== 'object') return '';
    var i, v;
    for (i = 0; i < OBJ_KEYS.length; i++) {
      v = args[OBJ_KEYS[i]];
      if (typeof v === 'string' && v.trim()) return shorten(v);
      if (v && typeof v === 'object') {
        try { var j = JSON.stringify(v); if (j && j.length < 42) return shorten(j); } catch (e) {}
      }
    }
    for (var k in args) {
      v = args[k];
      if (typeof v === 'string' && v.trim() && v.length < 90) return shorten(v);
    }
    return '';
  }

  function truncate(s, n) {
    s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n) + '…' : s;
  }

  // 走 i18n 词典的模板翻译：T('第 {n} 步', {n: 3})
  // 数字 + 中文拼出来的串无法被 MutationObserver 整段匹配，必须显式翻译。
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

  // ── 帧合并队列 ────────────────────────────────────────────
  var queue = new Map();
  function schedule(key, fn) {
    if (queue.has(key)) { queue.set(key, fn); return; }
    queue.set(key, fn);
    requestAnimationFrame(function () {
      var f = queue.get(key);
      queue.delete(key);
      if (f) { try { f(); } catch (e) { console.warn('[agent-motion]', e); } }
    });
  }
  function flush(key) {
    if (!queue.has(key)) return;
    var f = queue.get(key);
    queue.delete(key);
    try { f(); } catch (e) { console.warn('[agent-motion]', e); }
  }

  var seq = 0;
  function keyOf(inst, tag) {
    if (!inst.__amId) inst.__amId = ++seq;
    return tag + ':' + inst.__amId;
  }

  // ── 计时注册表（单一 rAF 驱动） ──────────────────────────
  var tickers = [];
  var runningCards = [];

  function addTicker(el, slow) {
    if (!el) return;
    el.dataset.amTs = String(Date.now());
    if (slow) el.dataset.amSlow = '1';
    if (tickers.indexOf(el) < 0) tickers.push(el);
  }
  function dropTicker(el) {
    var i = tickers.indexOf(el);
    if (i >= 0) tickers.splice(i, 1);
    if (el && el.dataset) { delete el.dataset.amTs; delete el.dataset.amSlow; }
  }

  function fmtDur(ms) {
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + 's';
    var m = Math.floor(s / 60), r = Math.floor(s % 60);
    return m + 'm' + (r < 10 ? '0' + r : r) + 's';
  }

  var lastTick = 0;
  function tick(ts) {
    requestAnimationFrame(tick);
    if (ts - lastTick < 250) return;
    lastTick = ts;
    var now = Date.now();
    var i, el, ms;
    for (i = tickers.length - 1; i >= 0; i--) {
      el = tickers[i];
      if (!el || !el.isConnected) { tickers.splice(i, 1); continue; }
      var t0 = parseInt(el.dataset.amTs || '0', 10);
      if (!t0) { tickers.splice(i, 1); continue; }
      ms = now - t0;
      el.textContent = fmtDur(ms);
      if (el.dataset.amSlow === '1') el.classList.toggle('am-slow', ms > SLOW_MS);
      var host = el.closest ? el.closest('.am-card') : null;
      if (host) {
        if (ms > LONG_MS) host.classList.add('am-long');
        if (ms > SLOW_MS) host.classList.add('am-slow');
      }
    }
    for (i = runningCards.length - 1; i >= 0; i--) {
      var c = runningCards[i];
      if (!c || !c.isConnected || c.dataset.amState !== 'running') { runningCards.splice(i, 1); }
    }
    liveLatency();
  }
  requestAnimationFrame(tick);

  function liveLatency() {
    try {
      if (typeof startTime === 'undefined' || !isProcessing) return;
      var el = document.getElementById('latency-badge');
      if (el && startTime) el.textContent = fmtDur(Date.now() - startTime);
    } catch (e) {}
  }

  // ── 滚动粘性 ──────────────────────────────────────────────
  var sticky = true;
  function getArea() { return document.getElementById('chat-area'); }
  function getBtn() { return document.getElementById('scroll-btn'); }

  function hideBtn() {
    var b = getBtn();
    if (!b) return;
    b.classList.add('hidden');
    var d = b.querySelector('.am-dot-badge');
    if (d && d.parentNode) d.parentNode.removeChild(d);
  }
  function showBtn() {
    var b = getBtn();
    if (!b) return;
    b.classList.remove('hidden');
    if (!b.querySelector('.am-dot-badge')) {
      var busy = false;
      try { busy = !!isProcessing; } catch (e) { busy = false; }
      if (busy) {
        var dot = document.createElement('span');
        dot.className = 'am-dot-badge';
        b.appendChild(dot);
      }
    }
  }
  function softScroll(force) {
    var a = getArea();
    if (!a) return;
    var near = a.scrollHeight - a.scrollTop - a.clientHeight < 120;
    if (near) sticky = true;
    if (near || (force && sticky)) { a.scrollTop = a.scrollHeight; hideBtn(); return; }
    showBtn();
  }
  function bindScrollArea() {
    var a = getArea();
    if (!a || a.__amBound) return;
    a.__amBound = 1;
    a.addEventListener('scroll', function () {
      var near = a.scrollHeight - a.scrollTop - a.clientHeight < 120;
      sticky = near;
      if (near) hideBtn();
    }, { passive: true });
  }
  window.scrollToBottom = softScroll;

  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target.closest('#send-btn, #stop-btn, #scroll-btn') : null;
    if (t) sticky = true;
    if (t && t.id === 'stop-btn') markAllCancelled();
  }, true);

  // ── 运行指示条 / 停止按钮状态 ────────────────────────────
  var topbar = null, fillTimer = null;
  function ensureTopbar() {
    if (topbar) return topbar;
    topbar = document.createElement('div');
    topbar.className = 'am-topbar';
    topbar.innerHTML = '<i></i>';
    document.body.appendChild(topbar);
    return topbar;
  }
  // 状态切换：运行(on) 无限循环进度；结束(off) 先"充满到头"再淡出——明确告知"这次结束了"
  function setActive(on) {
    var tb = ensureTopbar();
    var sb = document.getElementById('stop-btn');
    if (sb) sb.classList.toggle('am-live', !!on);
    if (on) {
      clearTimeout(fillTimer);
      tb.classList.remove('am-fill');
      tb.classList.add('am-on');
      return;
    }
    if (!tb.classList.contains('am-on')) return;      // 本来就没在跑
    tb.classList.add('am-fill');
    clearTimeout(fillTimer);
    fillTimer = setTimeout(function () {
      tb.classList.remove('am-on', 'am-fill');
    }, REDUCED ? 0 : 380);
  }

  // ── 卡片主体（用于运行/结束态差异）────────────────────────
  function bodyOf(inst) {
    if (!inst || !inst.processEl) return null;
    var b = inst.processEl.parentNode;
    if (b && b.classList) b.classList.add('am-body');
    return b;
  }

  // ── Run Head ──────────────────────────────────────────────
  function ensureHead(inst) {
    if (inst.__amHead && inst.__amHead.isConnected) return inst.__amHead;
    if (!inst.processEl) return null;
    var h = document.createElement('div');
    h.className = 'am-runhead';
    h.dataset.amState = 'running';
    h.innerHTML =
      '<span class="am-rh-mark">' + RING + '</span>' +
      '<span class="am-rh-text">准备中</span>' +
      '<span class="am-rh-dots" aria-hidden="true"></span>' +
      '<span class="am-rh-time"></span>';
    inst.processEl.insertBefore(h, inst.processEl.firstChild);
    inst.__amHead = h;
    addTicker(h.querySelector('.am-rh-time'));
    return h;
  }

  function renderDots(h, n) {
    var box = h.querySelector('.am-rh-dots');
    if (!box) return;
    var cur = parseInt(h.dataset.amDots || '-1', 10);
    if (cur === n) return;
    h.dataset.amDots = String(n);
    var max = 10, shown = Math.min(n, max), html = '';
    for (var i = 0; i < shown; i++) {
      var now = (i === shown - 1 && h.dataset.amState === 'running');
      html += '<i class="am-rh-dot' + (now ? ' is-now' : '') + '"></i>';
    }
    if (n > max) html += '<span class="am-rh-more">+' + (n - max) + '</span>';
    box.innerHTML = html;
  }

  function updateHead(inst, label) {
    var h = ensureHead(inst);
    if (!h) return;
    var st = h.dataset.amState;
    var n = inst.__amSteps || 0;
    var txt = h.querySelector('.am-rh-text');
    var mark = h.querySelector('.am-rh-mark');
    if (st === 'running') {
      var par = runningCards.length;
      txt.textContent = T('第 {n} 步', { n: Math.max(n, 1) }) +
        (label ? ' · ' + T(label) : '') +
        (par > 1 ? T(' · 并行 {n} 项', { n: par }) : '');
      mark.innerHTML = RING;
    } else if (st === 'done') {
      mark.innerHTML = CHECK;
    } else if (st === 'error') {
      mark.innerHTML = CROSS;
    } else if (st === 'cancel') {
      mark.innerHTML = BAN;
    }
    renderDots(h, n);
  }

  function finishHead(inst, state, extra) {
    var h = ensureHead(inst);
    if (!h || h.dataset.amState !== 'running') return;
    h.dataset.amState = state;
    var t = h.querySelector('.am-rh-time');
    if (t) dropTicker(t);
    var txt = h.querySelector('.am-rh-text');
    var n = inst.__amSteps || 0;
    var dur = fmtDur(Date.now() - (inst.__amStart || Date.now()));
    var word = state === 'done' ? '完成' : (state === 'error' ? '出错' : '已中断');
    if (txt) txt.textContent = T(word) + T(' · {n} 步 · {d}', { n: n, d: dur }) + (extra ? ' · ' + T(extra) : '');
    var mark = h.querySelector('.am-rh-mark');
    mark.innerHTML = state === 'done' ? CHECK : (state === 'error' ? CROSS : BAN);
    mark.classList.add('am-pop');
    renderDots(h, n);
    setActive(false);
  }

  // ── 焦点衰减 / 轨道节点 ───────────────────────────────────
  function focusNode(inst, node) {
    if (!node) return;
    if (inst.processEl) inst.processEl.classList.add('am-rail');
    var prev = inst.__amLive;
    if (prev && prev !== node && prev.classList) {
      prev.classList.remove('is-live');
      if (!prev.classList.contains('is-past')) prev.classList.add('is-past');
    }
    node.classList.add('am-node');
    node.classList.remove('is-past');
    node.classList.add('is-live');
    inst.__amLive = node;
    setActive(true);
  }

  // ── 工具卡装饰 ────────────────────────────────────────────
  function decorateToolCard(card, name, args) {
    if (!card) return null;
    var isNew = card.dataset.amDec !== '1';
    var sum = card.querySelector('summary');

    if (isNew) {
      card.dataset.amDec = '1';
      card.classList.add('am-card', 'am-tool', 'am-enter');
      if (!REDUCED) setTimeout(function () { card.classList.remove('am-enter'); }, 400);
      else card.classList.remove('am-enter');
    }

    // 动词 + 宾语
    if (sum) {
      var nameEl = sum.querySelector('.text-accent-text.font-semibold') ||
                   sum.querySelector('.font-semibold');
      if (nameEl) {
        nameEl.className = 'am-tv';
        nameEl.textContent = verbOf(name);
        nameEl.title = String(name || '');
      }
      var obj = objectOf(args);
      var to = sum.querySelector('.am-to');
      if (!to) {
        to = document.createElement('span');
        to.className = 'am-to';
        if (nameEl && nameEl.nextSibling) sum.insertBefore(to, nameEl.nextSibling);
        else sum.appendChild(to);
      }
      to.textContent = obj;
      to.classList.remove('am-out', 'am-err');

      if (!card.querySelector('.am-elapsed')) {
        var eta = document.createElement('span');
        eta.className = 'am-elapsed';
        var icon = sum.querySelector('svg.tool-icon');
        if (icon && icon.parentNode === sum) sum.insertBefore(eta, icon);
        else sum.appendChild(eta);
      }
      if (!sum.querySelector('.am-progress')) {
        var prog = document.createElement('span');
        prog.className = 'am-progress';
        prog.innerHTML = '<i></i>';
        sum.appendChild(prog);
      }
    }

    // 运行中
    card.dataset.amState = 'running';
    card.classList.remove('am-long', 'am-slow');
    card.dataset.amRunTs = String(Date.now());
    var eta2 = card.querySelector('.am-elapsed');
    if (eta2) { addTicker(eta2, true); eta2.textContent = '0.0s'; }
    setStateIcon(card, 'running');
    if (runningCards.indexOf(card) < 0) runningCards.push(card);
    var stl = card.querySelector('.tool-status');
    if (stl) stl.textContent = '';
    return isNew;
  }

  function resultText(card) {
    var r = card.querySelector('.tool-result');
    if (!r) return '';
    var pres = r.querySelectorAll('pre');
    var out = '';
    for (var i = 0; i < pres.length; i++) {
      var t = pres[i].textContent;
      if (t && t.trim()) out += t + '\n';
    }
    return out;
  }
  function summarize(card) {
    var txt = resultText(card);
    if (!txt.trim()) return '';
    var lines = txt.replace(/\n+$/, '').split('\n').filter(function (l) {
      var t = l.trim();
      return t && t !== 'output' && t !== 'error' && t !== 'input';
    });
    if (!lines.length) return '';
    return lines.length > 1 ? T('{n} 行输出', { n: lines.length }) : truncate(lines[0], 30);
  }

  function finishToolCard(card, state) {
    if (!card) return;
    if (card.dataset.amState === 'done' || card.dataset.amState === 'error' || card.dataset.amState === 'cancel') return;
    card.dataset.amState = state;
    card.classList.remove('am-long', 'am-slow');
    var i = runningCards.indexOf(card);
    if (i >= 0) runningCards.splice(i, 1);

    var ms = Date.now() - parseInt(card.dataset.amRunTs || '0', 10);
    var eta = card.querySelector('.am-elapsed');
    if (eta) { dropTicker(eta); eta.textContent = fmtDur(ms); eta.classList.remove('am-slow'); }

    setStateIcon(card, state);

    // 宾语 → 结果：一行讲完「对谁做了什么，得到什么」
    var to = card.querySelector('.am-to');
    if (to) {
      var info = state === 'done' ? summarize(card) : (state === 'error' ? '失败' : '已中断');
      to.classList.remove('am-out', 'am-err');
      if (state === 'done') to.classList.add('am-out');
      if (state === 'error' || state === 'cancel') to.classList.add('am-err');
      var had = to.textContent;
      to.textContent = (had ? had + ' ' : '') + '→ ' + (info || '完成');
    }
    var stl = card.querySelector('.tool-status');
    if (stl) stl.textContent = '';

    card.classList.remove('is-live');
    card.classList.add('is-past');
    if (state === 'done') card.classList.add('is-ok');
    if (state === 'error') card.classList.add('is-err');

    if (!REDUCED && state === 'done') {
      card.classList.add('am-sweep');
      setTimeout(function () { card.classList.remove('am-sweep'); }, 660);
    }
  }

  function markAllCancelled() {
    var list = document.querySelectorAll('details[data-tool-name][data-am-state="running"]');
    for (var i = 0; i < list.length; i++) finishToolCard(list[i], 'cancel');
    runningCards.length = 0;
    var h = document.querySelector('.am-runhead[data-am-state="running"]');
    if (h) { h.dataset.amState = 'cancel'; updateHead({ __amHead: h, __amSteps: parseInt(h.dataset.amDots || '0', 10) }, null); }
    setActive(false);
  }

  // ── 实时输出流 ────────────────────────────────────────────
  function trackStream(card) {
    if (!card) return;
    var s = card.querySelector('.tool-stream');
    if (!s) return;
    var pre = s.querySelector('pre');
    if (!pre) return;
    var lines = pre.textContent.split('\n').length;
    var prev = parseInt(card.dataset.amLines || '0', 10);
    card.dataset.amLines = String(lines);

    var bar = s.querySelector('.am-stream-bar');
    if (!bar) {
      bar = document.createElement('div');
      bar.className = 'am-stream-bar';
      bar.innerHTML = '<span class="am-sb-name">output</span>' +
                      '<span class="am-lines"></span>' +
                      '<span class="am-more" role="button" tabindex="0">展开全部</span>';
      s.insertBefore(bar, s.firstChild);
    }
    if (Math.abs(lines - prev) >= 3 || lines <= CLIP_LINES + 2) {
      var ln = bar.querySelector('.am-lines');
      if (ln) ln.textContent = T('· {n} 行', { n: lines });
    }
    if (lines > CLIP_LINES && !s.classList.contains('am-clipped') && !s.classList.contains('am-open')) {
      s.classList.add('am-clipped');
    }
  }

  document.addEventListener('click', function (e) {
    var m = e.target && e.target.closest ? e.target.closest('.am-more') : null;
    if (!m) return;
    var s = m.closest ? m.closest('.tool-stream') : null;
    if (!s) return;
    var open = s.classList.contains('am-open');
    s.classList.toggle('am-open', !open);
    s.classList.toggle('am-clipped', open);
    m.textContent = open ? '展开全部' : '收起';
    e.preventDefault();
  });

  // 用户手动展开过工具卡 → 结束后不再自动收起运行区
  document.addEventListener('toggle', function (e) {
    var card = e.target;
    if (card && card.tagName === 'DETAILS' && card.dataset && card.dataset.toolName) {
      card.dataset.amUserOpen = '1';
    }
  }, true);

  // ── 工具事件 ──────────────────────────────────────────────
  var _addTool = P.addTool;
  P.addTool = function (name, args, agentMeta, callId) {
    _addTool.apply(this, arguments);
    var card = this.currentToolEl;
    if (!card) return;
    var isNew = decorateToolCard(card, name, args);
    if (isNew) this.__amSteps = (this.__amSteps || 0) + 1;
    focusNode(this, card);
    updateHead(this, verbOf(name));
    softScroll(true);
  };

  var _updateTool = P.updateTool;
  P.updateTool = function (name, stage, message, metadata) {
    _updateTool.call(this, name, stage, message, metadata);
    var card = this.currentToolEl;
    if (metadata && metadata.call_id && this.findToolCardByCallId) {
      var m = this.findToolCardByCallId(metadata.call_id);
      if (m) card = m;
    }
    if (card && card.dataset && card.dataset.toolName) {
      if (stage === 'stream') {
        schedule('am:stream:' + (card.dataset.amUid || (card.dataset.amUid = ++seq)),
                 (function (c) { return function () { trackStream(c); }; })(card));
      } else if (stage === 'done' || stage === 'error') {
        var st = stage === 'done' ? 'done' : 'error';
        schedule('am:fin:' + (card.dataset.amUid || (card.dataset.amUid = ++seq)),
                 (function (c, s) { return function () { finishToolCard(c, s); syncHead(c); }; })(card, st));
      } else if (stage !== 'output') {
        var s2 = card.querySelector('.tool-status');
        if (s2 && message) s2.textContent = truncate(message, 36);
      }
    }
    softScroll();
  };

  function syncHead() {
    // 卡片状态变化后刷新摘要条上的并行数
    var hs = document.querySelectorAll('.am-runhead[data-am-state="running"]');
    for (var i = 0; i < hs.length; i++) {
      var txt = hs[i].querySelector('.am-rh-text');
      if (!txt) continue;
      txt.textContent = txt.textContent.replace(/ · 并行 \d+ 项/, '');
      if (runningCards.length > 1) txt.textContent += T(' · 并行 {n} 项', { n: runningCards.length });
    }
  }

  // ── 思考行 ────────────────────────────────────────────────
  function fmtChars(n) { return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n); }

  P.showThinking = function () {
    this._ensure();
    if (!this.processEl || this.thinkingEl) return;
    var el = document.createElement('div');
    el.className = 'thinking-row am-think am-enter';
    el.innerHTML =
      '<span class="am-rh-mark" style="display:inline-flex">' + RING + '</span>' +
      '<span class="am-think-label">正在推演</span>' +
      '<span class="am-dots" aria-hidden="true"><i></i><i></i><i></i></span>' +
      '<span class="am-think-meta"></span>';
    this.thinkingEl = el;
    this.processEl.appendChild(el);
    this._thinkStart = Date.now();
    var meta = el.querySelector('.am-think-meta');
    addTicker(meta);
    focusNode(this, el);
    updateHead(this, '思考');
    if (REDUCED) el.classList.remove('am-enter');
    softScroll(true);
  };

  P.hideThinking = function () {
    var el = this.thinkingEl;
    if (!el) return;
    flush(keyOf(this, 'reason'));
    this.thinkingEl = null;
    var meta = el.querySelector('.am-think-meta');
    if (meta) dropTicker(meta);
    if (!el.classList.contains('am-think')) {
      if (el.parentNode) el.parentNode.removeChild(el);
      return;
    }
    var ms = Date.now() - (this._thinkStart || Date.now());
    var n = (this._reasoningText || '').length;
    el.classList.remove('am-think');
    el.classList.add('am-think-fin', 'is-past');
    el.innerHTML =
      '<span style="display:inline-flex;color:rgb(var(--c-success))">' + CHECK + '</span>' +
      '<span class="am-think-label">' + T('已思考 {n}', { n: fmtDur(ms) }) +
      (n > 0 ? T(' · {n} 字', { n: fmtChars(n) }) : '') + '</span>';
    // 小结停留 1.1s 让眼睛捕捉到"想完了"，再淡出
    setTimeout(function () {
      el.classList.add('am-leaving');
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }, REDUCED ? 0 : 1100);
  };

  // ── 文本流：帧合并 + 长文本节流 ───────────────────────────
  function caretOf(inst) {
    if (!inst.__amCaret) {
      var s = document.createElement('span');
      s.className = 'am-caret';
      inst.__amCaret = s;
    }
    return inst.__amCaret;
  }
  function renderText(inst) {
    if (!inst.contentEl) return;
    var html = inst.text || '';
    var fences = html.match(/```/g);
    if (fences && fences.length % 2 === 1) html += '\n```';
    inst.contentEl.innerHTML = safeMarked(html);
    postProcessMarkdown(inst.contentEl);
    if (!inst.__amDoneOnce) inst.contentEl.appendChild(caretOf(inst));
    softScroll();
  }
  function dropCaret(inst) {
    var c = inst.__amCaret;
    if (c && c.parentNode) c.parentNode.removeChild(c);
  }

  var thr = {};
  function scheduleText(inst) {
    var self = inst;
    var len = (inst.text || '').length;
    var gap = len > 40000 ? 320 : (len > 12000 ? 140 : 0);
    var k = keyOf(inst, 'text');
    var now = Date.now();
    var st = thr[k] || (thr[k] = { t: 0, timer: null });
    var run = function () { st.t = Date.now(); schedule(k, function () { renderText(self); }); };
    if (now - st.t >= gap) { run(); return; }
    if (st.timer) return;
    st.timer = setTimeout(function () { st.timer = null; run(); }, gap - (now - st.t));
  }

  P.appendText = function (delta) {
    this._ensure();
    this.collapseReasoning();
    this.text = (this.text || '') + delta;
    scheduleText(this);
  };

  // ── 推理流 ────────────────────────────────────────────────
  P.addReasoning = function (content) {
    this._ensure();
    this._reasoningText = (this._reasoningText || '') + content;
    var self = this;
    schedule(keyOf(this, 'reason'), function () { self._amRenderReasoning(); });
  };
  P._amRenderReasoning = function () {
    var text = this._reasoningText || '';
    if (!text) return;
    if (!this._reasoningBlock) {
      this._reasoningBlock = document.createElement('div');
      this._reasoningBlock.className = 'thinking-block text-xs text-ink-3 leading-relaxed am-node';
      this.processEl.appendChild(this._reasoningBlock);
    }
    var html = text;
    var fences = html.match(/```/g);
    if (fences && fences.length % 2 === 1) html += '\n```';
    if (html.length > REASON_TAIL) html = '…（前面已折叠）\n\n' + html.slice(-REASON_TAIL);
    this._reasoningBlock.innerHTML = safeMarked(html);
    softScroll();
  };

  // ── 回合生命周期 ──────────────────────────────────────────
  var activeInst = null;

  var _ensure = P._ensure;
  P._ensure = function () {
    var created = !this.el;
    _ensure.call(this);
    if (created) {
      // 上一个回合没有正式 finalize（切会话 / 异常中断）时，先把它的摘要条收尾
      if (activeInst && activeInst !== this) finishHead(activeInst, 'done');
      activeInst = this;
      this.__amSteps = 0;
      this.__amLive = null;
      this.__amHead = null;
      this.__amStart = this._turnStart || Date.now();
      if (this.el) this.el.dataset.run = 'running';   // 运行态 → 见 CSS §10
      var bd = bodyOf(this);
      if (bd) bd.classList.remove('is-settling');
      if (this.processEl) {
        this.processEl.classList.add('am-rail');
        this.processEl.classList.remove('am-fin');
      }
      ensureHead(this);
      bindScrollArea();
      setActive(true);
    }
  };

  var _finalize = P.finalize;
  P.finalize = function (steps, usage) {
    flush(keyOf(this, 'text'));
    flush(keyOf(this, 'reason'));

    if (this.contentEl) {
      this.__amDoneOnce = true;
      dropCaret(this);
      if (this.text) {
        this.contentEl.innerHTML = safeMarked(this.text);
        postProcessMarkdown(this.contentEl);
      }
    }

    // 仍在 running 的卡（被中断 / 异常结尾）
    var list = document.querySelectorAll('details[data-tool-name][data-am-state="running"]');
    for (var i = 0; i < list.length; i++) finishToolCard(list[i], 'cancel');
    runningCards.length = 0;

    var extra = '';
    try {
      if (usage) {
        var tk = usage.total_tokens || usage.totalTokens || 0;
        if (tk) extra = (tk >= 1000 ? (tk / 1000).toFixed(1) + 'k' : tk) + ' tokens';
      }
    } catch (e) {}
    var scope = this.el || document;
    var hasErr = scope.querySelector('.am-card[data-am-state="error"]');
    var state = hasErr ? 'error' : 'done';

    var self = this;
    var el = this.el;

    // 所有工具卡先就地收敛（spinner → 勾/错），让用户看见最后一步落地
    _finalize.call(this, steps, usage);

    // 收束编排：停一拍 → 顶部条冲到 100% → 卡片由"运行态"切"结束态"
    var settle = function () {
      finishHead(self, state, extra);
      setActive(false);
      var b = el ? el.querySelector('.am-body') : null;
      if (b) {
        b.classList.add('is-settling');
        setTimeout(function () { b.classList.remove('is-settling'); }, REDUCED ? 0 : 520);
      }
      if (el) el.dataset.run = state;
      softScroll();
    };
    if (REDUCED) settle();
    else { softScroll(); setTimeout(settle, 260); }
  };

  // 注：回合收束由 index.html 原生的 activity-wrap（details 折叠）承担，
  //     这里不重复造折叠，避免双重收束。

  // ── 键盘：↑/↓ 在工具卡之间移动焦点 ───────────────────────
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    var cur = document.activeElement;
    if (!cur || cur.tagName !== 'SUMMARY') return;
    var card = cur.closest('details[data-tool-name]');
    if (!card) return;
    var all = Array.prototype.slice.call(
      document.querySelectorAll('#messages details[data-tool-name] > summary'));
    var i = all.indexOf(cur);
    if (i < 0) return;
    var next = all[i + (e.key === 'ArrowDown' ? 1 : -1)];
    if (next) { next.focus(); e.preventDefault(); }
  });

  // ── 挂载 ──────────────────────────────────────────────────
  function install() {
    bindScrollArea();
    var a = getArea();
    if (a) sticky = a.scrollHeight - a.scrollTop - a.clientHeight < 120;
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();

  window.ScoutMotion = {
    version: '2.0.0',
    setActive: setActive,
    isSticky: function () { return sticky; },
    running: function () { return runningCards.length; }
  };
})();
