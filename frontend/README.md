# frontend —— Scout 前端样式构建

这个目录只做一件事：**把全站样式编译成一个 CSS 文件**。

```
frontend/src/app.css   ──(tailwindcss 编译)──▶   scout/web/static/css/app.css
       ↑ 样式源码（改样式只改这里）                    ↑ 构建产物（入库，别再手改）
```

## 为什么要有构建这一步

原来每个页面的 `<head>` 里都挂着一个 397KB 的 `vendor/tailwind.js`（Tailwind 浏览器运行时版），
它在页面加载后才现场扫描 DOM 生成样式，由此带来两个后果：

1. **必然闪一帧**，而且首屏样式是"补"上去的；
2. 更麻烦的是布局 —— 旧 `index.html` 的注释里就写着
   「高度链退化是因为 CDN 运行时未生成」，为了绕开它，布局被迫用
   `id` 选择器 + `!important` 硬钉住，累计 260 个 `!important` 和
   210 条 `html:not(.dark) … !important` 主题补丁。

改成构建期编译后：样式在首帧前就绪，产物 80KB 左右，而且**所有颜色都能走 CSS 变量**，
深浅主题自动切换，不再需要成百上千条覆盖规则。

## 日常怎么用

### 改样式

```bash
cd frontend
npm install          # 只需第一次（只装 tailwindcss 一个包）
npm run build        # 编译 → ../scout/web/static/css/app.css
```

改完 `src/app.css` 一定要 `npm run build`。**忘了构建是这里唯一容易踩的坑**：
`desktop/build.bat` 会检查产物是否存在、是否比源码旧，但开发时肉眼是看不出来的。

边改边看用 `npm run watch`（自动重新编译）。

### 提交

构建产物 `scout/web/static/css/app.css` 是**入库的**。这样打包机不需要 Node，
PyInstaller 也只是把整个 `static/` 目录拷进去（见 `desktop/scout_desktop.spec` 的 `datas`），
打包流程零改动。所以：**改完样式，源码和产物要一起提交。**

## 目录说明

| 文件 | 作用 |
| --- | --- |
| `src/app.css` | 全站样式唯一入口。分三层：`@layer base`（设计 token + 重置）、`@layer components`（组件）、`@layer utilities`（动画） |
| `tailwind.config.js` | 把 CSS 变量映射成 Tailwind 色板。改颜色体系只改这里和 `src/app.css` 的 token 段 |
| `migrate.mjs` | 一次性迁移脚本：把散落各页的硬编码颜色/内联 `<style>`/主题判定收敛成 token 体系。可重复执行（幂等） |
| `verify-classes.mjs` | 校验脚本：查「旧配色残留」和「HTML 用到但 CSS 里没有的类名」两类回归 |

### 两个脚本怎么用

改完样式、构建之前，跑一遍：

```bash
node migrate.mjs         # 把还散着的硬编码颜色/层级/死类收敛掉（改前先看一眼输出）
node verify-classes.mjs  # 必须收敛到「旧配色残留 0 处，缺失样式类 0 个」
```

`verify-classes.mjs` 的「缺失样式类」这一项值得单独说：它会报出
「HTML 里写了某个类名，但编译产物里没有对应规则」的情况，也就是
**类名还在、样式没了** —— 界面上表现为某个控件突然没边框/没背景色，
是最难靠肉眼发现的一类回归。

## 设计 token

所有颜色都在 `src/app.css` 的 `:root`（浅色）和 `html.dark`（深色）里定义，
`tailwind.config.js` 只负责把它们映射成 Tailwind 类名。

| 组 | 类名 | 用途 |
| --- | --- | --- |
| 背景层次 | `surface-0` … `surface-5` | 0 页面底 / 1 侧栏面板 / 2 卡片弹窗 / 3 次级填充 / 4 输入徽章 / 5 强交互 |
| 文字层次 | `ink-1` … `ink-4` | 1 主 / 2 次 / 3 辅助 / 4 占位禁用 |
| 边线 | `line` / `line-soft` / `line-strong` | 常规 / 更弱 / 更强 |
| 主色 | `accent` / `accent-text` / `accent-soft` | 实心填充 / 文字图标（浅色下用更深的青） / 浅底 |
| 实心块上的文字 | `on-solid` | 两个主题都保持白 |
| 遮罩 | `scrim` | 弹层背景 |
| 语义状态 | `danger` / `warn` / `success` / `info` / `agent`（各有 `-soft` 变体） | 状态色 |
| 品牌色阶 | `scout-50` … `scout-900` | 历史命名，保留原取值，页面里已写好的 `bg-scout-500/15` 不用改 |

## 几个容易踩的坑

**1. `text-white` 要分流。** 直接写 `text-white` 会导致浅色主题下白字白底。
按语义选：
- 实心色块上的文字（按钮、彩色徽章）→ `text-on-solid`（两主题保持白）
- 普通背景上的正文/标题 → `text-ink-1`（浅色下自动变深）

**2. 色阶重写要用「长档位优先」。** 匹配 `50|100|…|950` 时如果短档位在前，
`bg-emerald-500/10` 里的 `50` 会先命中，剩下的 `0/10` 拼成 `bg-success0/10`
—— 一个静默产生的坏类名。同时别加 `(?!/\d)` 这类守卫：它会把
`bg-emerald-600/40` 这种带透明度的正常写法整个挡掉。正确写法是
长档位在前 + `(?!\d)`。

**3. Tailwind 选择器要转义。** `.`、`/`、`[`、`]` 在以类名做选择器时都要加反斜杠
（`bg-surface-3\/15`、`max-w-\[90%\]`）。`verify-classes.mjs` 里的 `escapeSel()`
就是干这个的。

**4. 不要把样式写回 HTML。** 内联 `<style>`、`style="color:#xxx"`、
`style.cssText = 'color:#718096'` 都属于要消灭的形态 —— 它们不跟随主题。
`plugin-builder.html` 就是这么坏掉的：JS 里给白底卡片写上 `color:#e2e8f0`
（近白），文字基本看不见。需要动态样式时用 `var(--c-ink-3)` 这类变量，
它会自动跟随明暗主题。

**5. 改完样式，`sw.js` 的 `CACHE_VERSION` 要加一。** 浏览器只在 `sw.js`
字节变化时才更新 Service Worker；不改版本号会出现「新页面配旧缓存」。
