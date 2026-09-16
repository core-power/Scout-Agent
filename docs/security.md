# Scout Agent 安全策略

> 最后更新：2026-08-27

本文档描述 Scout Agent 的安全模型，覆盖 shell 命令执行、文件访问、Webhook 回调与外部请求等关键链路。公开部署前请务必阅读。

---

## 1. 路径安全常量（统一来源）

定义于 `scout/security/policy.py`，供 shell / file / web 等工具共享引用：

- **`SYSTEM_DIRS`（黑名单，禁止访问）**：`/etc /usr /bin /sbin /lib /lib64 /boot /sys /proc /dev /var /root`
- **`ALLOWED_PATH_PREFIXES`（白名单，允许访问）**：`/tmp /home /data /opt /srv /mnt /media /workspace`

个人版策略为“放宽但受控”：允许在用户主目录、临时目录与常见项目/数据目录内自由操作，严格禁止触碰系统关键目录。

---

## 2. Shell 命令执行策略

实现：`scout/tools/builtin/shell/__init__.py`

### 2.1 三层校验（`_validate_command`）

| 层 | 内容 | 说明 |
|---|---|---|
| 0 | `DANGEROUS_PATTERNS` 硬拦截 | `rm -rf /`、`dd`、`mkfs`、关机/重启、fork 炸弹、`curl/wget | sh`、重启/停止 scout 服务、读取敏感系统文件/SSH 密钥/历史命令。**不受 auto_approve 影响，永远生效**。命中后引导用户在终端手动执行。 |
| 1 | `SHELL_META` 注入/编码攻击模式 | `$(...)`、反引号 `` `...` ``、`${...}`、`curl|sh` / `wget|sh`。 |
| 2 | 命令白名单 `SAFE_COMMANDS` | 按 basename 匹配，未列出的命令直接拒绝。 |

随后对 **command 与 args 同时**执行：

- 注入模式 `INJECTION_PATTERNS`：`$(...)`、反引号、`${var}`、`$var`、`\x` 十六进制转义、八进制转义
- 换行符 `\n` / `\r` 拦截（禁止多行命令走私）
- 路径遍历 `..` 检测
- 绝对路径命中 `SYSTEM_DIRS` 拦截

### 2.2 Shell 元字符策略（个人版放宽）

> 2026-08-11 起放宽：**不再全面拦截所有元字符**，仅针对性拦截注入/编码攻击模式。

- **允许**：管道 `|`、重定向 `>` `<`、分号 `;`、逻辑符 `&` 等正常用法。此时执行路径降级为 `bash -c`，且参数中的注入模式已在校验阶段拦截。
- **拦截**：`$(...)`、反引号、`${...}`、`curl|sh`、换行走私。

### 2.3 执行方式

- 无元字符：`create_subprocess_exec`（非 shell），参数经 `shlex.quote`。
- 含元字符：`bash -c`，普通参数 quote、元字符参数保持原样以支持管道/重定向。
- cwd 白名单校验：不允许在系统目录下执行，仅允许主目录/临时目录/项目目录。
- 超时上限 120s，输出超长自动截断。

---

## 2.4 沙箱运行模式（2026-08-27 强化）

环境变量控制：

| 变量 | 取值 | 说明 |
|---|---|---|
| `SCOUT_SANDBOX_MODE` | `off`（默认）/ `non-main` / `all` | 沙箱开关，与 sandbox_mode 注解配合 |
| `SCOUT_SANDBOX_REQUIRE_DOCKER` | `0`（默认）/ `1` | `1` 时沙箱开启但 Docker 不可用 → **硬失败**，不再静默降级本地执行 |

`_check_docker()` 已从"仅检查二进制"升级为"二进制 + `docker info` daemon 探测"，并进程级缓存避免子代理重复阻塞；容器创建失败路径同样遵循 require_docker（失败抛错 vs 显著 `logger.error` 告警）。

## 3. 文件工具路径沙箱

实现：`scout/tools/builtin/files/unified.py`

`file` 工具的全部 7 个操作（read / write / list / replace / insert / delete / edit）在动作前统一经过 `_resolve_path()`：

1. `expanduser` + `abspath` 归一化，防 `..` 相对路径穿越；
2. 命中 `SYSTEM_DIRS` → 拒绝；
3. 未命中 `ALLOWED_PATH_PREFIXES` → 拒绝。

与 shell 工具的路径策略保持一致，避免通过 file 工具绕过 shell 的访问限制。

---

## 4. Webhook 回调安全

| 平台 | 机制 |
|---|---|
| 微信公众号（wechatmp） | GET 校验签名；POST 消息体 AES-256-CBC 解密（PKCS7），XML 解析后入队 |
| 企业微信（wechatcom） | 同上，`wecom_crypto.py` 实现加解密与签名校验（pycryptodome/cryptography 双兼容） |
| QQ 开放平台 | JSON 事件 + `X-Token` 请求头校验 |
| 飞书 / 微信 / 企业微信外部联系人 | 遵循平台规范签名校验 |

所有平台回调均验证签名/Token，拒绝伪造请求；解密失败或签名不匹配直接返回错误，不进入消息队列。

---

## 5. 外部请求 SSRF 防护

实现：`scout/tools/builtin/web/__init__.py` 与 `scout/a2a/client.py`

`web_fetch` / `_fetch_direct` 复用 `check_url_ssrf()`：

- 拒绝内网/回环地址（127.0.0.0/8、10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、169.254.0.0/16、`localhost`、`::1` 等）
- 拒绝 SSRF 常用规避写法（十进制/十六进制/八进制 IP、短域名、`[::]` 等）

---

## 6. 浏览器 Cookie / 下载路径防护

实现：`scout/tools/builtin/browser/__init__.py`

`save_cookies` / `load_cookies` / `download` 三处文件路径均经 `_safe_join(base_dir, name, fallback)`：

- 拒绝绝对路径与 `..` 路径穿越；
- 拒绝空名 / `.` / `..` 等特殊名；
- 非法输入回退到安全默认名。

---

## 7. 密钥与敏感数据

- 所有 API Key、凭证与个人数据通过 `.gitignore` 排除在仓库之外（见项目根 `.gitignore`）。
- 演示配置仅保留占位值，不包含真实凭证。
- 平台回调密钥（公众号/企业微信 Token、EncodingAESKey 等）仅在环境变量或配置文件中读取，不写入日志。

---

## 8. 补充说明

- 危险命令拦截是**硬拦截**，即使 `auto_approve=True` 也不会放行；`needs_approval()` 仅决定“是否需要用户确认”，`check_command_block()` 负责执行前的强制拦截。
- 各平台适配器（微信/公众号/企业微信/飞书/QQ/Discord）的安全细节见对应源码注释与 `tests/` 下的单测。
