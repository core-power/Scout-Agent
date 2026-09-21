/**
 * Scout Agent — 前端样式迁移脚本（一次性，可重复执行=幂等）
 * ============================================================
 * 做四件事：
 *
 *  1) 换掉样式加载方式
 *     删掉浏览器运行时 Tailwind（vendor/tailwind.js + tailwind.config 内联块）
 *     与 github-dark.min.css（代码高亮已并入 app.css 由变量驱动），
 *     改为引用构建产物 /static/css/app.css。
 *
 *  2) 语义化配色
 *     把 17 个色系家族 / 203 个硬编码颜色类收敛成 5 个语义色
 *     （accent / danger / warn / success / info / agent）+ 三级中性色
 *     （surface / ink / line），使明暗主题由 CSS 变量自动切换。
 *
 *  3) 删除各页内联 <style>
 *     组件样式已集中到 app.css 的 components 层。
 *     唯一的例外是 plugin-builder.html —— 它自带一整套独立设计系统，
 *     且 HTML/JS 里还写死了一批深色主题色值，由 E 段的专用逻辑处理。
 *
 *  4) 统一主题初始化
 *     删掉各页自己的主题判定（index 内联块 / 次级页写死的 class="dark"），
 *     改由 /static/theme.js 一处决定，否则主题开关只在首页生效。
 *
 *  5) 清理
 *     去掉由 Tailwind 运行时导致的 !important 级联补丁依赖
 *     （内联样式块被删除即自然消失），以及从来没生效过的死类。
 *
 * 用法：
 *   node migrate.mjs            # 实际执行
 *   node migrate.mjs --dry      # 只报告不落盘
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STATIC_DIR = path.resolve(__dirname, '../scout/web/static');
const DRY = process.argv.includes('--dry');

/* ══════════════════════════════════════════════════════════════════════════
   A. 中性色：逐一显式映射（这些承载「层次」语义，不能按色系一刀切）
   ══════════════════════════════════════════════════════════════════════════ */
const NEUTRAL_MAP = {
  // ── 任意值色（原实现手挑的 15 种近黑/近白，是「层次不可见」的根因）──
  'bg-[#0a0a0a]': 'bg-surface-0',        // 页面底
  'bg-[#0d0d0d]': 'bg-surface-1',        // 侧栏 / 面板
  'bg-[#111]': 'bg-surface-2',           // 卡片 / 弹窗
  'bg-[#161616]': 'bg-surface-2',
  'bg-[#1a1a1a]': 'bg-surface-3',        // 次级填充（全站第一大颜色类，172 处）
  'bg-[#222]': 'bg-surface-4',
  'bg-[#333]': 'bg-surface-5',
  'hover:bg-[#1a1a1a]': 'hover:bg-surface-3',
  'hover:bg-[#333]': 'hover:bg-surface-5',
  'dark:bg-[#1a1a1a]': 'bg-surface-3',
  // 工具卡 / 多 Agent 卡片底
  'bg-[#0d1117]': 'bg-surface-2',
  'bg-[#150d24]': 'bg-surface-3',
  'bg-[#140d22]': 'bg-surface-3',
  // 多 Agent 面板渐变（面板已由 .ma-panel 用 token 接管，这里退化为同色系）
  'from-[#1a1035]': 'from-agent-soft',
  'via-[#150d24]': 'via-agent-soft',
  'to-[#0d0a1a]': 'to-surface-2',

  // 更新横幅：原设计是「深绿浮窗，两主题一致」，但正因为背景恒为深色，
  // 才需要专门写 html:not(.dark) #update-banner 一整套规则把文字救回来
  // （见旧 index.html 第 429-439 行注释）。这里改成常规浮层，
  // 背景跟随主题、绿色只用在边框/图标/按钮上，文字 token 自然两主题可读。
  'bg-emerald-950/90': 'bg-surface-2',

  // ── 文字：slate / gray ──
  // 说明：slate-400/500/600 共 456 处原取值在近黑底上对比度只有 4.1:1 / 2.7:1，
  //      12px 正文用这个明度实际不可读。统一提到 ink-3（≈6.4:1）。
  'text-slate-200': 'text-ink-1',
  'text-slate-300': 'text-ink-2',
  'text-slate-400': 'text-ink-3',
  'text-slate-500': 'text-ink-3',
  'text-slate-600': 'text-ink-3',
  'text-slate-700': 'text-ink-4',
  'text-slate-800': 'text-ink-1',
  'hover:text-slate-200': 'hover:text-ink-1',
  'hover:text-slate-300': 'hover:text-ink-2',
  'group-hover:text-slate-300': 'group-hover:text-ink-2',
  'dark:text-slate-200': 'text-ink-1',
  'dark:text-slate-400': 'text-ink-3',
  'placeholder-slate-500': 'placeholder-ink-4',
  'placeholder-slate-600': 'placeholder-ink-4',

  'text-gray-100': 'text-ink-1',
  'text-gray-300': 'text-ink-2',
  'text-gray-400': 'text-ink-3',
  'text-gray-500': 'text-ink-3',
  'text-gray-600': 'text-ink-3',
  'text-gray-800': 'text-ink-1',

  // ── 背景：slate / gray ──
  'bg-slate-900': 'bg-surface-0',
  'bg-slate-800/95': 'bg-surface-2',
  'bg-slate-800/50': 'bg-surface-2',
  'bg-slate-800/30': 'bg-surface-1',
  'bg-slate-800': 'bg-surface-3',
  'bg-slate-700/50': 'bg-surface-3',
  'bg-slate-700/30': 'bg-surface-2',
  'bg-slate-700': 'bg-surface-4',
  'bg-slate-600': 'bg-surface-5',
  'bg-slate-500/20': 'bg-surface-4',
  'bg-slate-500/10': 'bg-surface-3',
  'bg-slate-500': 'bg-surface-5',
  'hover:bg-slate-700/50': 'hover:bg-surface-4',
  'hover:bg-slate-600': 'hover:bg-surface-5',
  'hover:bg-slate-500': 'hover:bg-surface-5',

  'bg-gray-900': 'bg-surface-0',
  'bg-gray-800': 'bg-surface-3',
  'bg-gray-700': 'bg-surface-4',
  'bg-gray-600': 'bg-surface-5',
  'bg-gray-50': 'bg-surface-1',
  'bg-gray-400': 'bg-surface-4',
  'hover:bg-gray-600': 'hover:bg-surface-5',

  // ── 边线 ──
  'border-slate-200': 'border-line-strong',
  'border-slate-600': 'border-line-strong',
  'border-slate-700': 'border-line',
  'border-slate-500/30': 'border-line-strong',
  'border-gray-300': 'border-line-strong',
  'border-gray-700': 'border-line',
  'border-white/5': 'border-line-soft',
  'border-white/10': 'border-line',
  'border-white/20': 'border-line-strong',
  'hover:border-white/20': 'hover:border-line-strong',
  'dark:border-white/10': 'border-line',

  // ── 半透明填充（原 white/5、white/10、white/[0.0x]）──
  'bg-white/5': 'bg-surface-3',
  'bg-white/10': 'bg-surface-4',
  'bg-white/[0.02]': 'bg-surface-3',
  'bg-white/[0.03]': 'bg-surface-3',
  'hover:bg-white/5': 'hover:bg-surface-3',
  'hover:bg-white/10': 'hover:bg-surface-4',
  'hover:bg-white/[0.02]': 'hover:bg-surface-3',
  'hover:bg-white/[0.03]': 'hover:bg-surface-3',
  'dark:hover:bg-white/5': 'hover:bg-surface-3',
  'hover:bg-black/5': 'hover:bg-surface-3',
  'group-hover:bg-black/20': 'group-hover:bg-scrim/20',

  // ── 遮罩 ──
  'bg-black/60': 'bg-scrim/60',
  'bg-black/40': 'bg-scrim/40',
  'bg-black/30': 'bg-scrim/30',
  'bg-black/0': 'bg-scrim/0',

  // ── 渐变遮罩 ──
  'from-slate-900/80': 'from-surface-0/85',
  'to-slate-900/80': 'to-surface-0/0',

  // ── 品牌色 → 主题感知的 accent ──
  // 原实现靠 html:not(.dark) .text-scout-400 { color:#0e7490 !important } 等
  // 十几条覆盖来保证浅色可读；换成 accent token 后这些覆盖全部不需要。
  'text-scout-300': 'text-accent-text',
  'text-scout-400': 'text-accent-text',
  'text-scout-500': 'text-accent-text',
  'text-scout-600': 'text-accent-text',
  'hover:text-scout-300': 'hover:text-accent-text',
  'hover:text-scout-400': 'hover:text-accent-text',
  'group-hover:text-scout-400': 'group-hover:text-accent-text',
  'bg-scout-500': 'bg-accent',
  'bg-scout-600': 'bg-accent',
  'hover:bg-scout-500': 'hover:bg-accent/85',
  'hover:bg-scout-600': 'hover:bg-accent/85',
  'bg-scout-500/5': 'bg-accent/5',
  'bg-scout-500/[0.03]': 'bg-accent/5',
  'bg-scout-500/10': 'bg-accent/10',
  'bg-scout-500/15': 'bg-accent/15',
  'bg-scout-500/20': 'bg-accent/20',
  'bg-scout-500/25': 'bg-accent/25',
  'bg-scout-500/30': 'bg-accent/30',
  'bg-scout-500/40': 'bg-accent/40',
  'hover:bg-scout-500/5': 'hover:bg-accent/10',
  'hover:bg-scout-500/20': 'hover:bg-accent/25',
  'hover:bg-scout-500/30': 'hover:bg-accent/35',
  'hover:bg-scout-500/40': 'hover:bg-accent/45',
  'hover:bg-scout-500/50': 'hover:bg-accent/55',
  'border-scout-500/20': 'border-accent/20',
  'border-scout-500/30': 'border-accent/30',
  'border-scout-500/50': 'border-accent/50',
  'hover:border-scout-500/30': 'hover:border-accent/35',
  'hover:border-scout-500/40': 'hover:border-accent/45',
  'focus:border-scout-400': 'focus:border-accent',
  'focus:border-scout-500/50': 'focus:border-accent/60',
  'focus:ring-scout-500': 'focus:ring-accent',
  'focus:ring-scout-500/20': 'focus:ring-accent/20',
  'focus:ring-scout-400/50': 'focus:ring-accent/50',
  'shadow-scout-500/20': 'shadow-accent/20',
  'hover:shadow-scout-500/30': 'hover:shadow-accent/30',
  // 渐变按钮退化为纯色实心（顺带解决浅色主题需要单独重写 linear-gradient 的问题）
  'from-scout-400': 'from-accent',
  'from-scout-500': 'from-accent',
  'from-scout-600': 'from-accent',
  'to-scout-400': 'to-accent',
  'to-scout-500': 'to-accent',
  'to-scout-600': 'to-accent',
  'to-scout-700': 'to-accent',
  'hover:from-scout-500': 'hover:from-accent/85',
  'hover:from-scout-600': 'hover:from-accent/85',
  'hover:to-scout-600': 'hover:to-accent/85',
  'hover:to-scout-700': 'hover:to-accent/85',

  // ── 字阶：把绕过字阶的任意值收回体系 ──
  'text-[9px]': 'text-tiny',
  'text-[10px]': 'text-tiny',
  'text-[11px]': 'text-2xs',
  'text-[13.5px]': 'text-sm',

  // ── 阴影：默认 Tailwind 大黑投影在深色底上会糊成一团，换成分层柔和投影 ──
  'shadow-2xl': 'shadow-modal',
  'shadow-xl': 'shadow-pop',
  'shadow-lg': 'shadow-card',
};

/* ══════════════════════════════════════════════════════════════════════════
   A2. 从来没有生效过的死类
   --------------------------------------------------------------------------
   index.html 的知识库预览弹窗写的是 `prose prose-invert prose-sm`
   —— Tailwind Typography 插件的标准用法。但这个项目从没引过该插件，
   Play CDN 也加载不了 npm 插件，所以这三个类一直是死的：正文根本没有排版。

   现在 `.prose` 由 app.css 承接（并入 .msg-content 那套 Markdown 规则），
   剩下两个修饰类没有任何意义，删掉 —— 留着会让下一个人误以为它们在起作用。
   ══════════════════════════════════════════════════════════════════════════ */
const DEAD_CLASSES = ['prose-invert', 'prose-sm'];

/* ══════════════════════════════════════════════════════════════════════════
   A3. 散落的任意值层级
   --------------------------------------------------------------------------
   全站 8 处 z-[60] / z-[100] / z-[120] / z-[200] / z-[9000] / z-[9998] /
   z-[9999]，各写各的数字，谁压谁全靠记。收敛成 tailwind.config.js 里
   那套具名层级（dropdown30 / overlay50 / modal60 / toast70 / update80）。

   注意 z-[9998]（图片查看器）给了 modal 而不是更小的值：它是在弹层里
   打开的，必须不低于 modal；给 toast 会盖住操作按钮。
   ══════════════════════════════════════════════════════════════════════════ */
const Z_MAP = {
  'z-[100]': 'z-modal',    // func-panel / fork-modal / settings-modal
  'z-[120]': 'z-modal',    // 动态创建的图片查看弹层
  'z-[200]': 'z-toast',    // automation 页的轻提示
  'z-[9000]': 'z-update',  // 更新提示横幅
  'z-[9998]': 'z-modal',   // 全屏图片查看器
  'z-[9999]': 'z-toast',   // 登录遮罩（要盖住 App，但不盖住提示）
};

/* ══════════════════════════════════════════════════════════════════════════
   A4. plugin-builder.html：独立设计系统 + 写死的深色色值
   --------------------------------------------------------------------------
   这个页面原先完全不加载 Tailwind，自带一套浅色设计系统
   （紫色渐变底 #667eea→#764ba2、白卡、#2d3748/#718096 灰阶、绿色主按钮
   #48bb78）。更麻烦的是 HTML 和 JS 里还散着一批写死的十六进制色，
   其中好几处是**深色主题的颜色**被用在白卡上：

     list.innerHTML = '<div style="color:#718096;…;background:rgba(255,255,255,0.04);…">'
     title.style.cssText = 'color:#e2e8f0;font-weight:500;flex:1;'

   #e2e8f0 是接近白的灰、rgba(255,255,255,0.04) 在白底上等于透明 ——
   也就是「搜索结果」那一块渲染出来基本看不见字。

   处理方式（只改样式，不动结构与逻辑）：
     1. 删掉整个 <style>：组件规则已搬进 app.css 的「插件构建页」段
     2. 注入 app.css + theme.js
     3. .container 改名 container-narrow —— app.css 里有定义，
        而 `.container` 会跟 Tailwind 内置的 container 工具类撞名
        （内置版只给 max-width，不居中）
     4. 写死的颜色 → CSS 变量，于是自动跟随明暗主题
     5. toast 补 .show —— app.css 的 .toast 是 opacity 0 → .show 淡入，
        原实现依赖 <style> 里的 slideIn 动画，那个 keyframes 已被删掉
   ══════════════════════════════════════════════════════════════════════════ */
const PB_COLOR_MAP = {
  // 渐变先整串换掉，否则会被拆成两个同色 stop 的退化渐变
  'linear-gradient(135deg,#38bdf8,#6366f1)': 'rgb(var(--c-accent))',
  'linear-gradient(135deg, #38bdf8, #6366f1)': 'rgb(var(--c-accent))',
  // 深色语境里被误用在白底上的值
  // （0.04 / 0.05 / 0.08 这几个白色叠加在白卡上等于什么都看不见，
  //   分别是搜索结果项背景、结果卡片背景、分隔线）
  'rgba(255,255,255,0.04)': 'rgb(var(--c-surface-3))',
  'rgba(255,255,255,0.05)': 'rgb(var(--c-surface-3))',
  'rgba(255,255,255,0.08)': 'rgb(var(--c-line))',
  'rgba(99,102,241,0.3)': 'rgb(var(--c-accent) / 0.35)',
  '#e2e8f0': 'rgb(var(--c-ink-1))',
  '#a0aec0': 'rgb(var(--c-ink-3))',
  '#718096': 'rgb(var(--c-ink-3))',
  '#2d3748': 'rgb(var(--c-ink-1))',
  '#4a5568': 'rgb(var(--c-ink-2))',
  '#818cf8': 'rgb(var(--c-accent))',
  '#6366f1': 'rgb(var(--c-accent))',
  '#38bdf8': 'rgb(var(--c-accent))',
  '#667eea': 'rgb(var(--c-accent))',
  '#48bb78': 'rgb(var(--c-success))',
  '#f56565': 'rgb(var(--c-danger))',
  '#cbd5e0': 'rgb(var(--c-line-strong))',
  '#f7fafc': 'rgb(var(--c-surface-3))',
  '#edf2f7': 'rgb(var(--c-surface-4))',
  '#fff':   'rgb(var(--c-on-solid))',
};

/* ══════════════════════════════════════════════════════════════════════════
   B. 语义色：按「色系家族 + 档位」整体收敛
   ══════════════════════════════════════════════════════════════════════════ */
const FAMILY_TO_SEMANTIC = {
  red: 'danger', rose: 'danger',
  orange: 'warn', amber: 'warn', yellow: 'warn',
  lime: 'success', green: 'success', emerald: 'success', teal: 'success',
  sky: 'info', blue: 'info',
  cyan: 'accent',
  indigo: 'agent', violet: 'agent', purple: 'agent', fuchsia: 'agent', pink: 'agent',
};

const UTILITY = 'bg|text|border|ring|from|via|to|divide|placeholder|shadow|fill|stroke|decoration|outline';
const PREFIX = 'hover|focus|focus-within|focus-visible|group-hover|dark|md|lg|sm|active|disabled';
const FAMS = Object.keys(FAMILY_TO_SEMANTIC).join('|');

/**
 * 色阶必须「长档位在前」，并挡住「短档位吃掉长档位前缀」。
 *
 * 这里踩过两个坑，都记下来，避免以后再犯：
 *
 *  ① 最初写成 (?:50|100|…|950) —— 短档位在前，于是 bg-emerald-500/10 里的
 *     `50` 先被匹配，剩下的 `0/10` 拼成了 bg-success0/10。
 *     一个静默产生的坏类名：HTML 里看着"迁过了"，样式其实全丢。
 *     修法：长档位在前（950 先于 50）。
 *
 *  ② 为了防 ① 又补了个 (?!/\d)，方向却是反的 —— 它断言「后面不是 /数字」，
 *     而 bg-emerald-600/40、bg-emerald-500/10 这类带透明度的写法恰好就是 /数字，
 *     于是整个匹配失败，彩色类名原样留在 HTML 里。
 *     比 ① 更隐蔽：类名看着"没被迁移"，其实是"没迁成功"。
 *     正确写法是 (?!\d)：只挡住「紧跟着数字」这一种情况（50 后面跟 0），
 *     后面接 /透明度 还是空格引号都不受影响。
 */
const LEVELS = '950|900|800|700|600|500|400|300|200|100|50';

const FAMILY_RE = new RegExp(
  `((?:(?:${PREFIX}):)*)(${UTILITY})-(?:${FAMS})-(?:${LEVELS})(?!\\d)`,
  'g'
);
const FAMILY_OF = new RegExp(`-(?:${FAMS})-`);

function collapseFamilies(text) {
  return text.replace(FAMILY_RE, (whole, pfx, util) => {
    const fam = whole.match(FAMILY_OF)[0].slice(1, -1);
    return `${pfx}${util}-${FAMILY_TO_SEMANTIC[fam]}`;
  });
}

/**
 * scout-* 兜底：显式映射表没覆盖到的变体（如 border-scout-400）在这里收口。
 * 文字类 → accent-text（浅色下才是可读的深青），其余 → accent。
 * accent-scout-500 是原生 accent-color 工具类，跳过。
 */
const SCOUT_RE = new RegExp(
  `((?:(?:${PREFIX}):)*)(bg|text|border|ring|from|via|to|divide|placeholder|shadow|fill|stroke|decoration|outline)-scout-(?:${LEVELS})(/\\d+)?`,
  'g'
);

function collapseScout(text) {
  return text.replace(SCOUT_RE, (_w, pfx, util, alpha = '') => {
    const target = util === 'text' ? 'accent-text' : 'accent';
    return `${pfx}${util}-${target}${alpha}`;
  });
}

/* ══════════════════════════════════════════════════════════════════════════
   C. text-white：必须区分「实心色块上的文字」与「正文/标题」
   ------------------------------------------------------------------------
   原实现就是因为没区分这两者，出现了
     「④ 实心深底按钮保持白字(修复 116 行 text-white 全局暗化的误伤)」
   这样一条专门往回补的规则。
   ══════════════════════════════════════════════════════════════════════════ */
const SOLID_FILL_RE = new RegExp(
  `(?:^|\\s)(?:bg-gradient-to-\\S+|bg-scout-\\d+|bg-accent|` +
  `bg-(?:${Object.keys(FAMILY_TO_SEMANTIC).join('|')})-(?:400|500|600|700|800|900|950)|` +
  `from-(?:${Object.keys(FAMILY_TO_SEMANTIC).join('|')})-)`
);

function fixTextWhite(html) {
  let changed = 0;

  const apply = (cls) => {
    const onSolid = SOLID_FILL_RE.test(cls);
    const target = onSolid ? 'text-on-solid' : 'text-ink-1';
    const hoverTarget = onSolid ? 'hover:text-on-solid/90' : 'hover:text-ink-1';
    return cls
      .replace(/\bhover:text-white\/\d+\b/g, hoverTarget)
      .replace(/\bhover:text-white\b/g, hoverTarget)
      .replace(/\btext-white\/\d+\b/g, `${target}/80`)
      .replace(/\btext-white\b/g, target);
  };

  // ① class="..." 属性
  html = html.replace(/class="([^"]*)"/g, (m, cls) => {
    if (!/text-white/.test(cls)) return m;
    const out = apply(cls);
    if (out !== cls) changed++;
    return `class="${out}"`;
  });

  // ② JS 里给 className 直接赋值的类名串
  //    automation.html 的 toast 就是这种写法：
  //      d.className = `… ${ok ? 'bg-emerald-600' : 'bg-red-600'} text-white`
  //    只处理属性会漏掉它 —— 校验脚本正是靠这一条发现漏网的。
  html = html.replace(
    /(\.className\s*=\s*)(`[^`]*`|'[^']*'|"[^"]*")/g,
    (m, head, lit) => {
      if (!/text-white/.test(lit)) return m;
      const out = apply(lit);
      if (out !== lit) changed++;
      return head + out;
    }
  );

  return { html, changed };
}

/* ══════════════════════════════════════════════════════════════════════════
   D. 头部资源替换
   ══════════════════════════════════════════════════════════════════════════ */
function rewriteHead(html) {
  const notes = [];
  const before = html;

  // 运行时 Tailwind → 删除
  html = html.replace(/[ \t]*<script\s+src="\/static\/vendor\/tailwind\.js"><\/script>\r?\n?/g,
    () => (notes.push('移除 vendor/tailwind.js（运行时 Tailwind）'), ''));
  html = html.replace(/[ \t]*<script>\s*tailwind\.config\s*=[\s\S]*?<\/script>\r?\n?/g,
    () => (notes.push('移除 tailwind.config 内联块'), ''));

  // 代码高亮样式已由 app.css 变量驱动
  html = html.replace(/[ \t]*<link\s+rel="stylesheet"\s+href="\/static\/vendor\/github-dark\.min\.css">\r?\n?/g,
    () => (notes.push('移除 github-dark.min.css（高亮改由 CSS 变量驱动）'), ''));

  // 注入构建产物
  if (!html.includes('/static/css/app.css')) {
    html = html.replace(/([ \t]*)<\/head>/,
      (_m, ind) => `${ind}<!-- 构建产物：frontend/src/app.css，改样式请改那里再 npm run build -->\n` +
                   `${ind}<link rel="stylesheet" href="/static/css/app.css">\n${ind}</head>`);
    notes.push('注入 app.css');
  }

  // 删除各页内联 <style>（plugin-builder.html 有自己的完整设计系统，另行处理）
  html = html.replace(/[ \t]*<style>[\s\S]*?<\/style>\r?\n?/g, () => (notes.push('删除内联 <style>'), ''));

  /* ── 主题初始化统一 ──────────────────────────────────────────────
     以前：index.html 内联一段 localStorage 判定，其余页面把 class="dark"
     写死在 <html> 上，monitor / plugin-config 干脆没设 ——
     于是主题开关只在首页生效（首页切浅色，进"系统监控"又变回深色）。

     现在统一由 /static/theme.js 决定，且必须在 <head> 里同步执行：
     主题类要在首次绘制前落到 <html>，否则会闪一下深色。 */

  // index.html 那段内联判定（顺带删掉它里面 add/remove 同一个类的死代码）
  html = html.replace(
    /[ \t]*<script>\s*\/\/ Restore theme before render to prevent flash[\s\S]*?<\/script>\r?\n?/g,
    () => (notes.push('内联主题判定块 → 改用共享 theme.js'), '')
  );

  // <html> 上写死的 dark 交给 theme.js 决定
  if (/\sclass="dark"/.test(html)) {
    html = html.replace(/(<html[^>]*?)\sclass="dark"/, '$1');
    notes.push('移除 <html> 上写死的 dark');
  }

  // 注入共享主题脚本：紧跟 <title>，先于任何 CSS / 业务脚本执行
  if (!html.includes('/static/theme.js')) {
    html = html.replace(/([ \t]*<title>[\s\S]*?<\/title>\r?\n?)/,
      (_m, titleLine) => `${titleLine}    <script src="/static/theme.js"></script>\n`);
    notes.push('注入 theme.js');
  }

  if (html !== before && !notes.length) notes.push('头部有改动');
  return { html, notes };
}

/* ══════════════════════════════════════════════════════════════════════════
   E. plugin-builder.html 的收敛（见 A4 段的说明）
   ══════════════════════════════════════════════════════════════════════════ */
function migratePluginBuilder(html) {
  // 1) 整段内联设计系统（含 body 的紫渐变、所有 .xxx 规则、slideIn keyframes）
  html = html.replace(/[ \t]*<style>[\s\S]*?<\/style>\r?\n?/, '');

  // 2) 注入构建产物与共享主题脚本
  if (!html.includes('/static/css/app.css')) {
    html = html.replace(/([ \t]*)<\/head>/,
      (_m, ind) =>
        `${ind}<script src="/static/theme.js"></script>\n` +
        `${ind}<!-- 构建产物：frontend/src/app.css，改样式请改那里再 npm run build -->\n` +
        `${ind}<link rel="stylesheet" href="/static/css/app.css">\n${ind}</head>`);
  }

  /* 3) 「搜索现成方案」原本也是 generate-btn（全宽主色 + 内联渐变），
        和上面的「生成插件」并排看是两个一模一样的主按钮，分不出主次。
        降为次级按钮，同时那串内联渐变也就一并去掉了。 */
  html = html.replace(
    /<button id="searchSkillsBtn" class="generate-btn" style="background:linear-gradient\(135deg,#38bdf8,#6366f1\);" onclick="searchWebSkills\(\)">/,
    '<button id="searchSkillsBtn" class="btn btn-secondary w-full mt-3" onclick="searchWebSkills()">'
  );

  // 4) 版式容器与页面底色
  html = html.replace('class="container"', 'class="container-narrow"');
  html = html.replace('class="header"', 'class="header card"');
  html = html.replace('<body>', '<body class="min-h-screen bg-surface-0 text-ink-1">');
  html = html.replace('<html lang="zh">', '<html lang="zh-CN">');

  /* 5) 写死的颜色 → CSS 变量。
        按长度倒序替换，避免 #fff 先把 #ffffff 之类的长值切一半。 */
  for (const [from, to] of Object.entries(PB_COLOR_MAP).sort((a, b) => b[0].length - a[0].length)) {
    html = html.split(from).join(to);
  }

  // 6) toast：靠 .show 淡入淡出（app.css 的 .toast 规则）
  html = html.replace(
    /setTimeout\(\(\) => \{\s*toast\.style\.animation = 'slideIn 0\.3s ease-out reverse';\s*setTimeout\(\(\) => toast\.remove\(\), 300\);\s*\}, 3000\);/,
    "requestAnimationFrame(() => toast.classList.add('show'));\n            setTimeout(() => {\n                toast.classList.remove('show');\n                setTimeout(() => toast.remove(), 300);\n            }, 3000);"
  );

  return html;
}

/* ══════════════════════════════════════════════════════════════════════════
   主流程
   ══════════════════════════════════════════════════════════════════════════ */
const keysByLength = Object.keys(NEUTRAL_MAP).sort((a, b) => b.length - a.length);

function migrate(html) {
  const stats = { neutral: 0, family: 0, white: 0, dead: 0, z: 0 };

  // 1) 中性色（按长度倒序，保证 hover:/dark: 等带前缀的变体先命中）
  for (const k of keysByLength) {
    const re = new RegExp(k.replace(/[[\]/.]/g, (c) => '\\' + c), 'g');
    const n = (html.match(re) || []).length;
    if (n) { html = html.replace(re, NEUTRAL_MAP[k]); stats.neutral += n; }
  }

  // 1.5) 死类清理（连同前面的空白一起删，避免留下连续空格）
  for (const cls of DEAD_CLASSES) {
    const re = new RegExp(`[ \\t]*\\b${cls}\\b`, 'g');
    const n = (html.match(re) || []).length;
    if (n) { html = html.replace(re, ''); stats.dead += n; }
  }

  // 1.6) 任意值层级 → 具名层级 token
  for (const [from, to] of Object.entries(Z_MAP)) {
    const re = new RegExp(from.replace(/[[\]]/g, (c) => '\\' + c), 'g');
    const n = (html.match(re) || []).length;
    if (n) { html = html.replace(re, to); stats.z += n; }
  }

  // 2) 语义色家族收敛（长档位优先，避免 500 被 50 提前吃掉）
  const beforeFam = html;
  html = collapseFamilies(html);
  if (html !== beforeFam) stats.family = 1;

  // 3) scout-* 品牌色兜底（显式映射表未覆盖的变体在此收口）
  html = collapseScout(html);

  // 4) text-white 分流
  const tw = fixTextWhite(html);
  html = tw.html;
  stats.white = tw.changed;

  return { html, stats };
}

const files = fs.readdirSync(STATIC_DIR).filter((f) => f.endsWith('.html')).sort();
console.log(`\n${'='.repeat(78)}\nScout 前端样式迁移${DRY ? '（演练模式，不落盘）' : ''}\n${'='.repeat(78)}\n`);

const summary = [];
for (const f of files) {
  const p = path.join(STATIC_DIR, f);
  const src = fs.readFileSync(p, 'utf8');

  if (f === 'plugin-builder.html') {
    const out = migratePluginBuilder(src);
    const delta = out.length - src.length;
    console.log(`  ${f.padEnd(22)} 独立设计系统收敛 · 体积 ${delta > 0 ? '+' : ''}${delta}`);
    console.log(`  ${' '.repeat(22)} 删内联 <style> | 注入 app.css + theme.js | 写死的色值 → CSS 变量 | toast 补 .show`);

    /* 防漏网：这个页面是唯一有写死色值的地方，替代表再全也可能漏。
       与其让某个色值悄悄留在暗处（上一版就是这样漏掉了 0.05 / 0.08
       两个白色叠加 —— 在白底上是不可见的），不如每次跑都报出来。
       注意排除 rgb(var(--c-xxx)) —— 那是已经换好的，不是漏网。 */
    const leftover = [...new Set(
      (out.match(/#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)/g) || []).filter((s) => !s.includes('var('))
    )];
    if (leftover.length) {
      console.log(`  ${' '.repeat(22)} ⚠ 仍有未替换的色值：${leftover.join(' ')}`);
    }

    summary.push({ f, note: '并入全站 token 体系', delta });
    if (!DRY) fs.writeFileSync(p, out, 'utf8');
    continue;
  }

  const head = rewriteHead(src);
  const mig = migrate(head.html);
  const out = mig.html;

  const delta = out.length - src.length;
  console.log(
    `  ${f.padEnd(22)} 中性色 ${String(mig.stats.neutral).padStart(4)} · ` +
    `white 分流 ${String(mig.stats.white).padStart(3)} · ` +
    `死类 ${String(mig.stats.dead).padStart(2)} · ` +
    `层级 ${String(mig.stats.z).padStart(2)} · ` +
    `体积 ${delta > 0 ? '+' : ''}${delta}`
  );
  console.log(`  ${' '.repeat(22)} ${head.notes.join(' | ')}`);

  summary.push({ f, ...mig.stats, delta });

  if (!DRY) fs.writeFileSync(p, out, 'utf8');
}

console.log(`\n共处理 ${summary.length} 个页面。\n`);
