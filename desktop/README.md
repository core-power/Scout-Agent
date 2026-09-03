# Scout Agent 绿色版桌面程序（Windows）

无需安装、无需注册、解压即用的 Windows 桌面程序。

## 一、使用（最终用户）

### 方式 A：绿色版 zip（推荐）
`dist/ScoutDesktop-win64.zip` 解压到任意 Windows 10/11 机器（可放 U 盘/移动硬盘），双击 `启动Scout.bat` 即用。

### 方式 B：PyInstaller exe
`dist/ScoutDesktop/` 整个文件夹拷贝到目标机器，双击 `ScoutAgent.exe` 启动。

两种方式通用特性：
- **免安装**：不写注册表、不做文件关联、不设开机自启
- **数据独立于程序**：会话/记忆/配置/API Key 存于 `%APPDATA%\Scout`（Windows 用户数据目录），覆盖升级程序不丢配置；旧目录（盘符根 `.scout` / 程序旁 `data/`）首次启动自动迁移
- **端口自适应**：8848 被占用自动 +1，无需手动改配置
- **WebView2 渲染**：使用 Windows 自带 Edge 内核，无需额外运行时（Win10/11 预装）
- **兜底降级**：无 pywebview 环境时自动打开系统浏览器
- **首次使用**：打开界面后到**设置**页配置 LLM API Key（或编辑 `config/.env`）；本地嵌入模型（约 114MB）首次使用时在界面内自动下载

## 二、构建（开发者）

### 方式 A：绿色版 zip（推荐，Linux/macOS/Windows 均可，无需 Wine）

原理：Windows embeddable Python + `pip download --platform win_amd64` 交叉下载全部依赖 wheel，组装便携目录后打包 zip。

```bash
# 任意系统，无需 Python 环境安装（只需 pip + 网络）
python3 tools/build_windows_portable.py
# 产物: dist/ScoutDesktop-win64.zip
```

参数：`--out` 输出目录、`--wheels-dir` wheel 缓存（避免重复下载）、`--no-zip` 只组装目录。

优点：不依赖 Wine/PyInstaller，构建快、体积小（不含本地模型，约 150-250MB）。

### 方式 B：PyInstaller exe（在 Windows 上执行）

> PyInstaller 不支持跨平台交叉编译，Windows 上跑 `desktop\build.bat` 即可。

```bat
cd scout-agent
desktop\build.bat
```

构建产物：`dist\ScoutDesktop\`（含 `ScoutAgent.exe` + 依赖）。

## 三、开发调试（跨平台）

launcher 逻辑与平台无关，可在任意机器验证：

```bash
python desktop/launcher.py --no-gui --port 9000   # 仅启动服务
# 浏览器打开 http://127.0.0.1:9000/chat
```

## 四、目录结构

```
desktop/
├── launcher.py            # 启动器（便携数据目录 / 内嵌服务 / 窗口）
├── build.bat              # Windows 一键构建（PyInstaller exe）
├── scout_desktop.spec     # PyInstaller 打包配置
├── scout.ico              # exe 图标（tools/gen_win_icon.py 生成）
├── requirements-desktop.txt  # 桌面最小依赖集（纯 ASCII）
├── Dockerfile.win         # （备选）Wine 交叉打包镜像，网络可用时可用
└── README.md

tools/
├── build_windows_portable.py  # 绿色版 zip 构建脚本（主路径，跨平台）
├── gen_win_icon.py            # exe 图标生成
└── gen_pwa_icons.py           # PWA 图标生成
```

## 五、常见问题

| 问题 | 解决 |
|---|---|
| 双击后黑窗无反应 | 绿色版带控制台便于看日志，等 5-10 秒；或浏览器访问 http://127.0.0.1:8848/chat |
| 提示缺 msvcp140.dll 等 | 安装微软 VC++ 运行库（vc_redist.x64.exe），Win10/11 通常已自带 |
| 杀软误报 | 绿色版无数字签名属正常现象，添加信任即可；正式分发建议申请代码签名证书 |
| 数据迁移 | 拷贝程序文件夹 + `%APPDATA%\Scout` 数据目录到新机器即可，无需重装 |
