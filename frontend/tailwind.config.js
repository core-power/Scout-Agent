/**
 * Scout Agent — Tailwind 构建期配置
 * ============================================================
 * 为什么要构建期编译（替代原先的 vendor/tailwind.js 浏览器运行时）：
 *   1. 运行时版本每次启动都要现场扫描 DOM 生成样式，必然闪一帧；
 *      更严重的是它曾直接造成布局 Bug —— 旧 index.html 的注释里写着
 *      「高度链退化是因为 CDN 运行时未生成」，为绕开它只能用 id 选择器
 *      + !important 把布局硬钉住。
 *   2. 构建产物可压到几十 KB，且样式在首帧前就绪。
 *
 * 设计 token 策略：
 *   所有需要区分明暗主题的颜色都走 CSS 变量（见 src/app.css 的 :root / html.dark），
 *   这里只做变量 → Tailwind 色板的映射。这样组件里写 `bg-surface-2`
 *   在深色是近黑、在浅色是近白，不需要再维护成百上千条
 *   `html:not(.dark) .bg-[#xxxxxx] { ... !important }` 覆盖规则。
 *
 * 构建：npm run build   （产物输出到 ../scout/web/static/css/app.css）
 */

/** 把 CSS 变量映射成 Tailwind 颜色，保留 /15 这类透明度修饰符能力 */
const v = (name) => `rgb(var(--c-${name}) / <alpha-value>)`;

/** 语义色：每个都不带数字档位，避免再出现 14 个色系家族并存 */
const semantic = (name) => ({
  DEFAULT: v(name),
  soft: v(`${name}-soft`),
});

/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    '../scout/web/static/*.html',
    '../scout/web/static/**/*.js',
  ],
  theme: {
    extend: {
      colors: {
        // ── 背景层次：0 页面 / 1 侧栏面板 / 2 卡片弹窗 / 3 次级填充 / 4 输入徽章 / 5 强交互 ──
        surface: {
          0: v('surface-0'),
          1: v('surface-1'),
          2: v('surface-2'),
          3: v('surface-3'),
          4: v('surface-4'),
          5: v('surface-5'),
        },
        // ── 四级文字：1 主 / 2 次 / 3 辅助 / 4 占位禁用 ──
        ink: {
          1: v('ink-1'),
          2: v('ink-2'),
          3: v('ink-3'),
          4: v('ink-4'),
        },
        // ── 三级边线 ──
        line: {
          DEFAULT: v('line'),
          soft: v('line-soft'),
          strong: v('line-strong'),
        },
        // ── 主色（青色系）。accent=实心填充，accentText=文字/图标用，accentSoft=浅底 ──
        accent: {
          DEFAULT: v('accent'),
          text: v('accent-text'),
          soft: v('accent-soft'),
        },
        // ── 实心色块上的文字：两个主题都保持白 ──
        'on-solid': v('on-solid'),
        // ── 遮罩 ──
        scrim: {
          DEFAULT: v('scrim'),
          soft: v('scrim-soft'),
        },
        // ── 语义状态色 ──
        danger: semantic('danger'),
        warn: semantic('warn'),
        success: semantic('success'),
        info: semantic('info'),
        agent: semantic('agent'),   // 多 Agent / 推演面板专用（原 violet 家族）

        // ── 品牌色阶：保留原有 scout-* 命名与取值，页面里的 bg-scout-500/15 等无需改动 ──
        scout: {
          50: '#ecfeff', 100: '#cffafe', 200: '#a5f3fc', 300: '#67e8f9',
          400: '#22d3ee', 500: '#06b6d4', 600: '#0891b2', 700: '#0e7490',
          800: '#155e75', 900: '#164e63',
        },
      },

      fontFamily: {
        // 原实现写 'Inter' 但从未加载该字体（无 @font-face、无外链），
        // 实际一直走系统字体。这里改成真实可用的栈，并显式声明中文回退。
        sans: [
          'system-ui', '-apple-system', 'Segoe UI', 'Roboto',
          'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei',
          'Noto Sans SC', 'sans-serif',
        ],
        mono: [
          'ui-monospace', 'SFMono-Regular', 'Cascadia Mono', 'Consolas',
          'Menlo', 'monospace',
        ],
      },

      // ── 字阶：补上原实现缺失的档位，并把 10px 从任意值里解放出来 ──
      fontSize: {
        '2xs': ['11px', { lineHeight: '16px' }],
        tiny: ['10px', { lineHeight: '14px' }],
      },

      borderRadius: {
        // 收敛原有的 3/4/6/8/9/10/12 混杂取值
        tag: '6px',
        card: '10px',
        panel: '14px',
      },

      boxShadow: {
        // 原实现用 Tailwind 默认 shadow-lg/xl/2xl（纯黑大范围投影），
        // 在深色底上会把控件糊成一团。改为分层、带主题感知的柔和投影。
        soft: '0 1px 2px rgb(var(--c-shadow) / 0.06)',
        card: '0 1px 3px rgb(var(--c-shadow) / 0.08), 0 1px 2px rgb(var(--c-shadow) / 0.04)',
        pop: '0 4px 12px rgb(var(--c-shadow) / 0.10), 0 2px 4px rgb(var(--c-shadow) / 0.06)',
        modal: '0 16px 48px rgb(var(--c-shadow) / 0.20), 0 4px 12px rgb(var(--c-shadow) / 0.10)',
      },

      zIndex: {
        // 原实现散落 z-[100]/z-[9000]/z-[9998]/z-[9999]，这里收成明确层次
        dropdown: '30',
        drawer: '40',
        overlay: '50',
        modal: '60',
        toast: '70',
        update: '80',
      },

      transitionTimingFunction: {
        swift: 'cubic-bezier(0.22, 1, 0.36, 1)',
      },
    },
  },
  plugins: [],
};
