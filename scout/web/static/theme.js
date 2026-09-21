/**
 * Scout 主题初始化 —— 全站唯一入口
 * ============================================================
 * 以前每个页面各写一套：
 *   · index.html      内联了一段 localStorage 判定（还带一段 add/remove
 *                     同一个类的死代码）
 *   · 其余 9 个页面    把 class="dark" 直接写死在 <html> 上
 *   · monitor.html / plugin-config.html   干脆没设
 * 结果是主题开关只在首页生效 —— 从首页切到浅色，点进「系统监控」又变回深色。
 *
 * 用法（必须放在 <head> 里、同步执行，不能加 defer / async）：
 *     <script src="/static/theme.js"></script>
 * 主题类要在首次绘制之前落到 <html> 上，否则会先闪一下深色再变浅。
 *
 * 取值规则沿用原实现：只有显式存过 'light' 才走浅色，其余一律深色。
 * （桌面端以深色为主，没存过就跟随系统偏好会让人以为设置丢了。）
 */
(function () {
    var dark = true;
    try {
        dark = localStorage.getItem('theme') !== 'light';
    } catch (e) {
        // 隐私模式 / file:// 下 localStorage 会抛异常，保持默认深色
    }
    document.documentElement.classList.toggle('dark', dark);
})();
