/**
 * Scout Agent — 前端样式校验
 * ============================================================
 * 迁移/重构后最容易出的两类问题，这里都挡住：
 *
 *   1) 配色迁移漏网
 *      仍有旧色系家族（slate/gray/red/…）、任意值十六进制色
 *      或已废弃的 scout-* 文字色残留在 HTML 里。
 *
 *   2) 类名无对应样式
 *      把 HTML 里用到的 class 逐个拿去编译产物 app.css 里找，
 *      找不到的列出来 —— 这类"类名还在、样式没了"是最隐蔽的回归，
 *      界面上表现为某个控件突然没有边框/背景/文字颜色。
 *
 * 用法： node verify-classes.mjs
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STATIC_DIR = path.resolve(__dirname, '../scout/web/static');
const CSS_PATH = path.join(STATIC_DIR, 'css/app.css');

const css = fs.readFileSync(CSS_PATH, 'utf8');
const htmlFiles = fs.readdirSync(STATIC_DIR).filter((f) => f.endsWith('.html')).sort();

/* Tailwind 选择器转义规则：非 [A-Za-z0-9_-] 的字符前面加反斜杠 */
const escapeSel = (t) => t.replace(/[^a-zA-Z0-9_-]/g, (c) => '\\' + c);

/* 非 Tailwind 的类名白名单：组件类、JS 状态钩子、i18n、高亮 —— 允许不出现在工具类中 */
const KNOWN_NON_UTILITY = new Set([
  'msg-content', 'msg-text', 'slide-in', 'scroll-btn', 'typing-dot', 'tab-active', 'tab-btn',
  'code-copy-btn', 'composer-tool', 'composer-model', 'composer-send', 'composer-stop',
  'composer-shell', 'dot', 'tool-btn', 'tool-result-pre', 'thinking-block', 'thinking-header',
  'attachment-item', 'session-item', 'example-card', 'example-card-title', 'example-card-desc',
  'ma-panel', 'ma-stages', 'ma-plan', 'agent-process', 'activity-wrap', 'reasoning-count',
  'card', 'card-flat', 'badge', 'toast', 'btn', 'btn-primary', 'btn-secondary', 'btn-ghost',
  'field', 'switch', 'knob', 'on', 'toggle-switch', 'modal-overlay', 'modal-panel',
  'empty-state', 'spinner', 'skeleton', 'bar-tip', 'editor-container', 'code-editor',
  'page-header', 'page-title', 'section-title', 'ico', 'ico-lg', 'ico-sm',
  'span-bar', 'trace-card', 'detail-box', 'url-box', 'bar-chart', 'bar',
  'stat-card', 'metric', 'metric-value', 'metric-label', 'metric-sub',
  'metric-accent', 'metric-success', 'metric-warn', 'metric-agent', 'progress-bar',
  'container-narrow', 'main-card', 'back-btn', 'requirement-input', 'plugin-name-input',
  'example-buttons', 'example-btn', 'generate-btn', 'action-buttons', 'loading',
  'code-section', 'info-box', 'plugin-card', 'template-card', 'header', 'container',
  'show', 'active', 'open', 'error', 'success', 'info', 'warning', 'hidden-js',
  'divider', 'tabular', 'truncate-1',
  // 事件类型 / 观测色带
  'ev-created', 'ev-modified', 'ev-deleted', 'ev-type',
  'span-type-llm', 'span-type-tool', 'span-type-tool-error', 'span-type-reflection',
  'span-type-conversation', 'span-type-goal', 'span-type-heal',
  // 代码高亮（由 .hljs-* 规则覆盖，名字本身不需要独立选择器）
  'hljs', 'mode-check', 'tool-icon', 'tool-name', 'tool-status', 'tool-args', 'tool-result',
  'tool-call-display', 'tool-result-display', 'reasoning-body', 'sub-tool-name', 'sub-tool-msg',
  'sub-time', 'sub-reasoning', 'sub-tools', 'sub-result', 'ma-plan-status', 'ma-plan-items',
  'ma-subgrid', 'ma-sub-cards', 'ma-summary', 'ma-summary-body',
  'ma-stage-plan', 'ma-stage-exec', 'ma-stage-fin', 'breathe', 'scout-radar',
  'update-banner', 'login-overlay', 'sidebar-overlay', 'func-panel', 'fork-modal',
  'settings-modal', 'toast-container', 'input', 'chat-area', 'main-col', 'sidebar-scroll',
  // 纯 JS 选择器钩子 —— 只在 querySelector / classList 里当锚点用，
  // 原本就没有对应的 CSS 规则（外观全交给同伴的工具类），不该报缺失。
  'agent-status-badge', 'agent-step-badge', 'agent-content', 'tool-detail',
  'ma-panel-status', 'sub-status', 'mode-option', 'settings-panel',
  'period-btn', 'search-skills-box',
  'mention-item', 'chat-model-check', 'tool-count-badge', 'sub-tool-row',
  'tool-stream', 'ma-plan-text', 'sub-card', 'usage-stats',
  'reflection-hint', 'suggestions-row', 'file-attachment',
  'search-engine-item', 'se-type', 'se-name', 'se-enabled', 'se-remove',
  'se-url', 'se-apikey',
]);

/* 需要报错的旧类特征 */
const LEGACY_PATTERNS = [
  [/(?:^|[\s"'])[a-z-]*\[#[0-9a-fA-F]{3,8}\]/, '任意值十六进制色'],
  [/(?:^|[\s"'])(?:bg|text|border|ring|from|via|to|divide|placeholder|shadow|fill|stroke)-(?:slate|gray|zinc|neutral|stone)-\d{2,3}/, '旧中性色系(slate/gray/zinc/…)'],
  [/(?:^|[\s"'])(?:bg|text|border|ring|from|via|to|divide|placeholder|shadow|fill|stroke)-(?:red|rose|orange|amber|yellow|lime|green|emerald|teal|sky|blue|cyan|indigo|violet|purple|fuchsia|pink)-\d{2,3}/, '旧语义色系(red/blue/violet/…)'],
  [/(?:^|[\s"'])(?:bg|text|border|from|to|via)-scout-(?:300|400|500|600)(?!\d)/, '旧品牌色文字/填充（应改用 accent token）'],
  [/\btext-white\b/, '未分流的 text-white'],
  [/\btext-\[(?:9|10|11|13\.5)px\]/, '绕过字阶的任意值字号'],
];

/* ── 模板表达式 ${…} 整体剔除（含嵌套花括号）──
   这是提取器最大的假阳性来源。JS 里到处是这种写法：

     <div class="… ${ok ? 'bg-emerald-600' : 'bg-red-600'} text-on-solid">

   不剔除的话，按空白切分就会得到 'bg-emerald-600'} 、? 、: 、'text-success'
   这类根本不是类名的 token，把「缺失样式」的报表淹掉。 */
function stripTemplates(s) {
  let out = '';
  for (let i = 0; i < s.length; i++) {
    if (s[i] === '$' && s[i + 1] === '{') {
      let depth = 1;
      i += 2;
      while (i < s.length && depth > 0) {
        if (s[i] === '{') depth++;
        else if (s[i] === '}') depth--;
        i++;
      }
      i--;                       // 循环头还会 i++，这里先退一格
    } else {
      out += s[i];
    }
  }
  return out;
}

/* 合法类名的形状。
   容易写窄的地方：
     · 负向工具类   -ml-2 / hover:-translate-y-1   → 允许开头是 -
     · 任意值方括号 max-w-[90%] / w-[calc(100%-2rem)] / grid-cols-[repeat(2,minmax(0,1fr))]
                    → 方括号里几乎什么字符都可能出现，% ( ) , = # 都要放行
     · !important   !mt-0（v3 写法，! 在开头）
   同时这里排除引号、问号、花括号、$ —— 那些一定是 JS 语法碎片，不是类名。 */
const TOKEN_OK = /^!?-?[a-zA-Z0-9][a-zA-Z0-9_:/[\].\-&>*+~%#(),=]*$/;

/* 提取时被判定为"不像类名"而丢弃的 token，单独报出来，
   免得下面的过滤悄悄吞掉真实的类名。 */
const dropped = new Set();

function classTokensOf(html) {
  const set = new Set();

  /* 从一段文本里收类名。
     先剔除 ${…}，再在第一个引号处截断 —— 这一步专门对付这种写法：

       '<svg class="chat-model-check w-3 h-3 flex-shrink-0' + (active ? ' …' : ' hidden') + '">'

     属性正则 `class="([^"]*)"` 会一路吃到下一个双引号，把中间的
     `+ (active ?` 、比较值、变量名全都圈进来。真正的类名只会出现在
     第一个引号之前，后面的都是 JS，截断即可。 */
  const add = (raw) => {
    let s = stripTemplates(raw ?? '');
    const q = s.search(/['"`]/);
    if (q !== -1) s = s.slice(0, q);
    for (const t of s.split(/\s+/)) {
      if (!t) continue;
      if (TOKEN_OK.test(t)) set.add(t);
      else dropped.add(t);
    }
  };

  // ① HTML 属性（三种引号都要收，JS 模板里常写成 class=`…`）
  for (const m of html.matchAll(/\bclass\s*=\s*(?:"([^"]*)"|'([^']*)'|`([^`]*)`)/g))
    add(m[1] ?? m[2] ?? m[3]);

  // ② JS 直接赋值：el.className = `…`
  for (const m of html.matchAll(/\.className\s*=\s*(?:"([^"]*)"|'([^']*)'|`([^`]*)`)/g))
    add(m[1] ?? m[2] ?? m[3]);

  // ③ JS：setAttribute('class', '…')
  for (const m of html.matchAll(/setAttribute\(\s*['"]class['"]\s*,\s*(?:"([^"]*)"|'([^']*)'|`([^`]*)`)/g))
    add(m[1] ?? m[2] ?? m[3]);

  // ④ JS：classList.add/remove/toggle('a', 'b')
  //    只认"整个参数就是一个字符串字面量"的参数。
  //    否则 classList.toggle('hidden', t !== 'event') 里的 'event'
  //    （一个比较值，不是类名）也会被收进来。
  for (const m of html.matchAll(/classList\.(?:add|remove|toggle)\(([^)]*)\)/g)) {
    for (const arg of splitTopLevelArgs(m[1])) {
      const lit = arg.trim().match(/^(?:"([^"]*)"|'([^']*)'|`([^`]*)`)$/);
      if (lit) add(lit[1] ?? lit[2] ?? lit[3]);
    }
  }

  return set;
}

/* 按顶层逗号切分参数列表：括号 / 方括号 / 引号内的逗号不算分隔符。
   classList.add('a', cond ? 'b' : 'c') 这种也要切在正确的位置。 */
function splitTopLevelArgs(s) {
  const out = [];
  let buf = '';
  let depth = 0;
  let quote = null;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (quote) {
      if (c === quote && s[i - 1] !== '\\') quote = null;
      buf += c;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') { quote = c; buf += c; continue; }
    if (c === '(' || c === '[' || c === '{') depth++;
    if (c === ')' || c === ']' || c === '}') depth--;
    if (c === ',' && depth === 0) { out.push(buf); buf = ''; continue; }
    buf += c;
  }
  if (buf) out.push(buf);
  return out;
}

let problems = 0;
const missingByFile = {};
const legacyByFile = {};

console.log(`\n${'='.repeat(78)}\nScout 前端样式校验  (app.css ${(css.length / 1024).toFixed(1)} KB)\n${'='.repeat(78)}`);

for (const f of htmlFiles) {
  const html = fs.readFileSync(path.join(STATIC_DIR, f), 'utf8');

  // ── 检查 1：旧类残留 ──
  for (const [re, label] of LEGACY_PATTERNS) {
    const hits = [...new Set([...html.matchAll(new RegExp(re.source, 'g'))].map((m) => m[0].trim()))];
    if (hits.length) {
      (legacyByFile[f] ??= []).push({ label, hits });
      problems += hits.length;
    }
  }

  // ── 检查 2：类名无样式 ──
  const missing = [];
  for (const t of classTokensOf(html)) {
    if (KNOWN_NON_UTILITY.has(t)) continue;
    if (/^\$\{|^\{\{|^data-|^js-/.test(t)) continue;   // 模板占位
    if (!css.includes('.' + escapeSel(t))) missing.push(t);
  }
  if (missing.length) missingByFile[f] = missing;
}

/* ── 输出 ── */
console.log('\n【检查 1】旧配色残留');
let legacyTotal = 0;
for (const [f, items] of Object.entries(legacyByFile)) {
  console.log(`\n  ✗ ${f}`);
  for (const { label, hits } of items) {
    legacyTotal += hits.length;
    console.log(`      ${label}: ${hits.length} 处`);
    console.log(`        ${hits.slice(0, 8).join('  ')}${hits.length > 8 ? ' …' : ''}`);
  }
}
if (!legacyTotal) console.log('  ✓ 无残留');

console.log('\n【检查 2】HTML 用到但 app.css 里没有的类');
let missTotal = 0;
for (const [f, list] of Object.entries(missingByFile)) {
  missTotal += list.length;
  console.log(`\n  ✗ ${f}  (${list.length})`);
  console.log(`      ${list.slice(0, 60).join('  ')}${list.length > 60 ? ' …' : ''}`);
}
if (!missTotal) console.log('  ✓ 全部类名都有对应样式');

/* 参考信息，不计入问题数：提取阶段被判为"不像类名"而丢掉的 token。
   正常状态下应该只剩被切断的 JS 片段；如果冒出看似正常的类名，
   说明 TOKEN_OK 写窄了，需要放宽。 */
if (dropped.size) {
  const list = [...dropped];
  console.log(`\n【参考】提取时丢弃的非类名 token（${list.length} 个，不算问题）`);
  console.log(`      ${list.slice(0, 40).join('  ')}${list.length > 40 ? ' …' : ''}`);
}

console.log(`\n${'─'.repeat(78)}`);
console.log(`合计：旧配色残留 ${legacyTotal} 处，缺失样式类 ${missTotal} 个`);
console.log(`${'─'.repeat(78)}\n`);

process.exitCode = legacyTotal + missTotal > 0 ? 1 : 0;
