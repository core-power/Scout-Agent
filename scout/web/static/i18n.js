// Scout UI i18n 词典 — 中文/英文界面切换 (2026-08-09)
// 用法: <span data-i18n="key">中文</span> 或 JS 里 I18N.t('key')
// 切换: toggleUILang()

const I18N_DICT = {
    // ── 通用按钮 ──
    "保存并生效": { zh: "保存并生效", en: "Save & Apply" },
    "保存配置": { zh: "保存配置", en: "Save Config" },
    "取消": { zh: "取消", en: "Cancel" },
    "删除": { zh: "删除", en: "Delete" },
    "添加": { zh: "添加", en: "Add" },
    "刷新": { zh: "刷新", en: "Refresh" },
    "创建": { zh: "创建", en: "Create" },
    "设置": { zh: "设置", en: "Settings" },
    "配置": { zh: "配置", en: "Config" },
    "管理 →": { zh: "管理 →", en: "Manage →" },
    "测试连接": { zh: "测试连接", en: "Test Connection" },
    "返回列表": { zh: "返回列表", en: "Back to List" },
    "复制": { zh: "复制", en: "Copy" },
    "✓ 已复制": { zh: "✓ 已复制", en: "✓ Copied" },
    "加载中...": { zh: "加载中...", en: "Loading..." },
    "加载失败": { zh: "加载失败", en: "Load Failed" },

    // ── 登录界面 ──
    "登录以继续": { zh: "登录以继续", en: "Log in to continue" },
    "用户名": { zh: "用户名", en: "Username" },
    "密码": { zh: "密码", en: "Password" },
    "输入用户名": { zh: "输入用户名", en: "Enter username" },
    "输入密码": { zh: "输入密码", en: "Enter password" },
    "登录": { zh: "登录", en: "Log in" },
    "首次登录将自动创建账户": { zh: "首次登录将自动创建账户", en: "First login auto-creates an account" },

    // ── 侧边栏 / 顶部 ──
    "新对话": { zh: "新对话", en: "New Chat" },
    "历史会话": { zh: "历史会话", en: "History" },
    "今天": { zh: "今天", en: "Today" },
    "昨天": { zh: "昨天", en: "Yesterday" },
    "最近 7 天": { zh: "最近 7 天", en: "Last 7 days" },
    "本月": { zh: "本月", en: "This month" },
    "更早": { zh: "更早", en: "Earlier" },
    "定时任务": { zh: "定时任务", en: "Scheduler" },
    "切换主题": { zh: "切换主题", en: "Toggle theme" },
    "记忆库": { zh: "记忆库", en: "Memory" },
    "知识管理": { zh: "知识管理", en: "Knowledge" },
    "知识页面": { zh: "知识页面", en: "Knowledge Pages" },

    // ── 欢迎屏示例 ──
    "文件操作": { zh: "文件操作", en: "File Ops" },
    "列出当前目录下的文件": { zh: "列出当前目录下的文件", en: "List files in current directory" },
    "记忆保存": { zh: "记忆保存", en: "Memory Save" },
    "记住我叫小马": { zh: "记住我叫小马", en: "Remember my name is Xiaoma" },
    "网络搜索": { zh: "网络搜索", en: "Web Search" },
    "搜索最新 AI Agent 项目": { zh: "搜索最新 AI Agent 项目", en: "Search latest AI Agent projects" },
    "代码执行": { zh: "代码执行", en: "Code Exec" },
    "运行 Python 代码": { zh: "运行 Python 代码", en: "Run Python code" },
    "记忆回忆": { zh: "记忆回忆", en: "Memory Recall" },
    "搜索保存的记忆": { zh: "搜索保存的记忆", en: "Search saved memories" },
    "网页抓取": { zh: "网页抓取", en: "Web Fetch" },
    "获取网页内容": { zh: "获取网页内容", en: "Fetch web content" },

    // ── 设置面板 Tab ──
    "模型配置": { zh: "模型配置", en: "Model" },
    "Agent 行为": { zh: "Agent 行为", en: "Agent" },
    "安全策略": { zh: "安全策略", en: "Security" },
    "渠道管理": { zh: "渠道管理", en: "Channels" },
    "服务版本": { zh: "服务版本", en: "Version" },
    "服务商": { zh: "服务商", en: "Provider" },
    "对话模型": { zh: "对话模型", en: "Model" },

    // ── 设置·多模型类型独立厂商（第9轮补充） ──
    "主服务商": { zh: "主服务商", en: "Main provider" },
    "跟随主服务商": { zh: "跟随主服务商", en: "Follow main provider" },
    "(可选独立服务商)": { zh: "(可选独立服务商)", en: "(optional independent provider)" },
    "(文本对话与工具调用)": { zh: "(文本对话与工具调用)", en: "(text chat & tool use)" },
    "(使用主服务商的 API Key)": { zh: "(使用主服务商的 API Key)", en: "(uses main provider's API Key)" },

    // ── 设置·Agent行为 ──
    "最大迭代次数": { zh: "最大迭代次数", en: "Max Turns" },
    "深度思考": { zh: "深度思考", en: "Deep Thinking" },
    "运行模式": { zh: "运行模式", en: "Agent Mode" },
    "回复语言": { zh: "回复语言", en: "Language" },
    "🌐 跟随用户输入语言": { zh: "🌐 跟随用户输入语言", en: "🌐 Follow user's language" },
    "🇨🇳 始终中文": { zh: "🇨🇳 始终中文", en: "🇨🇳 Always Chinese" },
    "🇺🇸 始终英文": { zh: "🇺🇸 始终英文", en: "🇺🇸 Always English" },
    "长期记忆": { zh: "长期记忆", en: "Long-term Memory" },
    "会话持久化": { zh: "会话持久化", en: "Session Persistence" },
    "上下文治理": { zh: "上下文治理", en: "Context Management" },
    "技能系统": { zh: "技能系统", en: "Skills" },
    "工作空间": { zh: "工作空间", en: "Workspace" },
    "事件总线": { zh: "事件总线", en: "Event Bus" },
    "自动审批工具执行": { zh: "自动审批工具执行", en: "Auto-approve tools" },
    "沙箱模式": { zh: "沙箱模式", en: "Sandbox Mode" },
    "关闭（直接执行）": { zh: "关闭（直接执行）", en: "Off (direct exec)" },
    "非主会话沙箱": { zh: "非主会话沙箱", en: "Non-main sessions" },
    "全部沙箱（Docker 隔离）": { zh: "全部沙箱（Docker 隔离）", en: "All (Docker isolated)" },
    "开启沙箱模式": { zh: "开启沙箱模式", en: "Enable sandbox mode" },
    "继续开启": { zh: "继续开启", en: "Enable" },
    "沙箱提示-all": {
        zh: "开启后：所有工具命令（含主会话）将在 Docker 容器内隔离执行——无网络，且内存/CPU/进程数受限。依赖联网的命令（如下载、外网接口）在沙箱内会失败；每次执行都会临时启动容器，响应略慢。",
        en: "When enabled, ALL tool commands (including your main session) run inside Docker containers — no network, limited memory/CPU/processes. Commands that need the internet (downloads, external APIs) will fail inside the sandbox; each run spins up a container, so responses get slightly slower.",
    },
    "沙箱提示-nonmain": {
        zh: "开启后：仅「委派子任务 / 深度任务」在 Docker 容器内隔离执行（无网络，资源受限）；你直接发起对话的命令仍在本地执行，不受沙箱保护。",
        en: "When enabled, only delegated/deep tasks run inside Docker containers (no network, limited resources); commands you run directly in conversation still execute locally and are NOT sandboxed.",
    },

    // ── 设置·渠道 ──
    "已配置的渠道": { zh: "已配置的渠道", en: "Configured Channels" },
    "暂无配置的渠道": { zh: "暂无配置的渠道", en: "No channels configured" },
    "添加平台": { zh: "添加平台", en: "Add Platform" },

    // ── 设置·会话/Web ──
    "会话恢复设置": { zh: "会话恢复设置", en: "Session Restore" },
    "恢复上次会话": { zh: "恢复上次会话", en: "Restore last session" },
    "使用上次模型": { zh: "使用上次模型", en: "Use last model" },
    "使用主模型": { zh: "使用主模型", en: "Use main model" },
    "Web 服务配置": { zh: "Web 服务配置", en: "Web Server" },
    "监听地址": { zh: "监听地址", en: "Host" },
    "监听端口": { zh: "监听端口", en: "Port" },

    // ── 状态 ──
    "已连接": { zh: "已连接", en: "Connected" },
    "已断开": { zh: "已断开", en: "Disconnected" },
    "正在思考...": { zh: "正在思考...", en: "Thinking..." },
    "正在凝萃中...": { zh: "正在凝萃中...", en: "Distilling..." },
    "解析中...": { zh: "解析中...", en: "Parsing..." },

    // ── 额外补充 ──
    "中 / EN": { zh: "中 / EN", en: "EN / 中" },
    "快速": { zh: "快速", en: "Fast" },
    "思考": { zh: "思考", en: "Think" },
    "当前版本": { zh: "当前版本", en: "Current Version" },
    "版本说明": { zh: "版本说明", en: "Release Notes" },
    "初始稳定版本发布": { zh: "初始稳定版本发布", en: "Initial stable release" },
    "获取 Key →": { zh: "获取 Key →", en: "Get Key →" },
    "(文生图)": { zh: "(文生图)", en: "(text-to-image)" },
    "搜索": { zh: "搜索", en: "Search" },
    "清空": { zh: "清空", en: "Clear" },
    "发送": { zh: "发送", en: "Send" },
    "停止": { zh: "停止", en: "Stop" },
    "已复制": { zh: "已复制", en: "Copied" },
    "复制代码": { zh: "复制代码", en: "Copy code" },
    "暂无": { zh: "暂无", en: "None" },
    "无": { zh: "无", en: "None" },
    "从未": { zh: "从未", en: "Never" },
    "启用": { zh: "启用", en: "Enable" },
    "禁用": { zh: "禁用", en: "Disable" },
    "重新激活": { zh: "重新激活", en: "Reactivate" },
    "需要确认": { zh: "需要确认", en: "Needs approval" },
    "运行状态": { zh: "运行状态", en: "Status" },
    "上次运行": { zh: "上次运行", en: "Last Run" },
    "调度类型": { zh: "调度类型", en: "Schedule Type" },
    "固定间隔(秒)": { zh: "固定间隔(秒)", en: "Interval (sec)" },
    "一次性": { zh: "一次性", en: "One-time" },
    "Cron 表达式": { zh: "Cron 表达式", en: "Cron Expression" },
    "AI 任务描述": { zh: "AI 任务描述", en: "AI Task" },
    "任务名称": { zh: "任务名称", en: "Task Name" },
    "创建定时任务": { zh: "创建定时任务", en: "Create Task" },
    "添加任务": { zh: "添加任务", en: "Add Task" },
    "添加": { zh: "添加", en: "Add" },
    "全部类别": { zh: "全部类别", en: "All Categories" },
    "知识库为空": { zh: "知识库为空", en: "Knowledge base empty" },
    "知识库为空，上传文件开始构建": { zh: "知识库为空，上传文件开始构建", en: "Empty. Upload files to build" },
    "记忆总数": { zh: "记忆总数", en: "Total Memories" },
    "追踪总数": { zh: "追踪总数", en: "Total Traces" },
    "追踪详情": { zh: "追踪详情", en: "Trace Details" },
    "系统状态": { zh: "系统状态", en: "System Status" },
    "索引状态": { zh: "索引状态", en: "Index Status" },
    "活跃目标": { zh: "活跃目标", en: "Active Goals" },
    "活跃度": { zh: "活跃度", en: "Activity" },
    "代码": { zh: "代码", en: "Code" },
    "保存的 Checkpoints": { zh: "保存的 Checkpoints", en: "Saved Checkpoints" },
    "预览内容将显示在这里...": { zh: "预览内容将显示在这里...", en: "Preview will appear here..." },
    "选择文件": { zh: "选择文件", en: "Choose File" },
    "上传": { zh: "上传", en: "Upload" },
    "正常提取": { zh: "正常提取", en: "Extracted" },
    "提取中...": { zh: "提取中...", en: "Extracting..." },
    "相似记忆去重阈值（0-1）": { zh: "相似记忆去重阈值（0-1）", en: "Dedup threshold (0-1)" },
    "回溯时间窗口（小时）": { zh: "回溯时间窗口（小时）", en: "Lookback window (hours)" },
    "启用星夜凝萃": { zh: "启用星夜凝萃", en: "Enable Starlight" },
    "立即执行一次星夜凝萃，从近期对话中提取记忆": { zh: "立即执行一次星夜凝萃，从近期对话中提取记忆", en: "Run Starlight now to extract memories" },
    "每日执行时间（时，0-23）": { zh: "每日执行时间（时，0-23）", en: "Daily run hour (0-23)" },
    "自动提取长期记忆": { zh: "自动提取长期记忆", en: "Auto-extract memories" },
    "每天凌晨自动回顾对话，提取关键信息（偏好、决策、事实）并存入记忆库": { zh: "每天凌晨自动回顾对话，提取关键信息（偏好、决策、事实）并存入记忆库", en: "Daily review of conversations to extract key info" },
    "😃 表情": { zh: "😃 表情", en: "😃 Emoji" },
    "附件": { zh: "附件", en: "Attach" },
    "输入消息...": { zh: "输入消息...", en: "Type a message..." },
    "发送消息": { zh: "发送消息", en: "Send message" },
    "模型": { zh: "模型", en: "Model" },
    "对话": { zh: "对话", en: "Chat" },
    "历史": { zh: "历史", en: "History" },
    "文件": { zh: "文件", en: "File" },
    "图片": { zh: "图片", en: "Image" },
    "视频": { zh: "视频", en: "Video" },
    "音频": { zh: "音频", en: "Audio" },
    "正在生成...": { zh: "正在生成...", en: "Generating..." },
    "正在上传...": { zh: "正在上传...", en: "Uploading..." },
    "已上传": { zh: "已上传", en: "Uploaded" },
    "你": { zh: "你", en: "You" },
    "复制": { zh: "复制", en: "Copy" },
    "回复": { zh: "回复", en: "Reply" },
    "重新生成": { zh: "重新生成", en: "Regenerate" },
    "删除消息": { zh: "删除消息", en: "Delete message" },
    "删除会话": { zh: "删除会话", en: "Delete session" },
    "新建会话": { zh: "新建会话", en: "New session" },
    "清除会话": { zh: "清除会话", en: "Clear session" },
    "清空会话": { zh: "清空会话", en: "Clear session" },
    // ── Toast/动态提示 ──
    "保存失败": { zh: "保存失败", en: "Save failed" },
    "保存失败，请重试": { zh: "保存失败，请重试", en: "Save failed, retry" },
    "保存成功": { zh: "保存成功", en: "Saved" },
    "删除失败": { zh: "删除失败", en: "Delete failed" },
    "删除失败，请重试": { zh: "删除失败，请重试", en: "Delete failed, retry" },
    "加载失败": { zh: "加载失败", en: "Load failed" },
    "停止失败": { zh: "停止失败", en: "Stop failed" },
    "停止失败，请重试": { zh: "停止失败，请重试", en: "Stop failed, retry" },
    "创建失败": { zh: "创建失败", en: "Create failed" },
    "添加失败": { zh: "添加失败", en: "Add failed" },
    "添加成功": { zh: "添加成功", en: "Added" },
    "配置已保存": { zh: "配置已保存", en: "Config saved" },
    "配置已更新": { zh: "配置已更新", en: "Config updated" },
    "渠道启动成功": { zh: "渠道启动成功", en: "Channel started" },
    "渠道已停止": { zh: "渠道已停止", en: "Channel stopped" },
    "渠道已删除": { zh: "渠道已删除", en: "Channel deleted" },
    "渠道配置保存成功": { zh: "渠道配置保存成功", en: "Channel config saved" },
    "任务已删除": { zh: "任务已删除", en: "Task deleted" },
    "任务已添加": { zh: "任务已添加", en: "Task added" },
    "状态已更新": { zh: "状态已更新", en: "Status updated" },
    "目标已创建": { zh: "目标已创建", en: "Goal created" },
    "目标已删除": { zh: "目标已删除", en: "Goal deleted" },
    "Checkpoint 已删除": { zh: "Checkpoint 已删除", en: "Checkpoint deleted" },
    "Checkpoint 已恢复": { zh: "Checkpoint 已恢复", en: "Checkpoint restored" },
    "页面已删除": { zh: "页面已删除", en: "Page deleted" },
    "知识页面保存成功": { zh: "知识页面保存成功", en: "Knowledge page saved" },
    "知识页面创建成功": { zh: "知识页面创建成功", en: "Knowledge page created" },
    "记忆未找到": { zh: "记忆未找到", en: "Memory not found" },
    "网络错误，请重试": { zh: "网络错误，请重试", en: "Network error, retry" },
    "连接失败": { zh: "连接失败", en: "Connection failed" },
    "登录失败": { zh: "登录失败", en: "Login failed" },
    "登录中...": { zh: "登录中...", en: "Logging in..." },
    "⏳ 测试中...": { zh: "⏳ 测试中...", en: "⏳ Testing..." },
    "运行中": { zh: "运行中", en: "Running" },
    "🟢 运行中": { zh: "🟢 运行中", en: "🟢 Running" },
    "✅ 运行中": { zh: "✅ 运行中", en: "✅ Running" },
    "禁用": { zh: "禁用", en: "Disabled" },
    "未找到相关内容": { zh: "未找到相关内容", en: "No relevant content" },
    "未找到相关记忆": { zh: "未找到相关记忆", en: "No relevant memories" },
    "暂无历史会话": { zh: "暂无历史会话", en: "No history" },
    "暂无记忆": { zh: "暂无记忆", en: "No memories" },
    "暂无定时任务": { zh: "暂无定时任务", en: "No tasks" },
    "暂无活跃目标": { zh: "暂无活跃目标", en: "No active goals" },
    "暂无追踪数据": { zh: "暂无追踪数据", en: "No trace data" },
    "暂无 checkpoint": { zh: "暂无 checkpoint", en: "No checkpoints" },
    "搜索失败": { zh: "搜索失败", en: "Search failed" },
    "请填写页面路径": { zh: "请填写页面路径", en: "Please fill page path" },
    "请填写页面路径和内容": { zh: "请填写页面路径和内容", en: "Please fill path and content" },
    "请填写完整的任务信息": { zh: "请填写完整的任务信息", en: "Please fill task info" },
    "请输入搜索关键词": { zh: "请输入搜索关键词", en: "Enter search keyword" },
    "请先加载要删除的页面": { zh: "请先加载要删除的页面", en: "Load the page first" },
    "确定删除这条记忆？": { zh: "确定删除这条记忆？", en: "Delete this memory?" },
    "确定要删除这个任务吗？": { zh: "确定要删除这个任务吗？", en: "Delete this task?" },
    "确定要删除这个目标及其所有任务吗？": { zh: "确定要删除这个目标及其所有任务吗？", en: "Delete this goal and its tasks?" },
    "确定要删除这个 checkpoint 吗？": { zh: "确定要删除这个 checkpoint 吗？", en: "Delete this checkpoint?" },
    "确定要恢复这个 checkpoint 吗？": { zh: "确定要恢复这个 checkpoint 吗？", en: "Restore this checkpoint?" },
    "删除这条消息及之后的所有消息？": { zh: "删除这条消息及之后的所有消息？", en: "Delete this message and those after?" },
    "删除会话": { zh: "删除会话", en: "Delete session" },
    "删除目标": { zh: "删除目标", en: "Delete goal" },
    "删除任务": { zh: "删除任务", en: "Delete task" },
    "删除知识页面": { zh: "删除知识页面", en: "Delete knowledge page" },
    "粗体文本": { zh: "粗体文本", en: "Bold text" },
    "链接文本": { zh: "链接文本", en: "Link text" },
    "列表项": { zh: "列表项", en: "List item" },
    "列表": { zh: "列表", en: "List" },
    "代码块": { zh: "代码块", en: "Code block" },
    "根目录": { zh: "根目录", en: "Root" },
    "空": { zh: "空", en: "Empty" },
    "(空)": { zh: "(空)", en: "(empty)" },
    "错误": { zh: "错误", en: "Error" },
    "自定义 Token": { zh: "自定义 Token", en: "Custom Token" },
    "事件数": { zh: "事件数", en: "Events" },
    "会话数": { zh: "会话数", en: "Sessions" },
    "Agent 数": { zh: "Agent 数", en: "Agents" },
    "记忆数": { zh: "记忆数", en: "Memories" },
    "创建时间": { zh: "创建时间", en: "Created" },
    "目标标题": { zh: "目标标题", en: "Goal title" },
    "目标描述 (可选)": { zh: "目标描述 (可选)", en: "Goal description (optional)" },
    "任务标题": { zh: "任务标题", en: "Task title" },
    "任务描述 (可选)": { zh: "任务描述 (可选)", en: "Task description (optional)" },
    "输入消息... (Enter 发送, Shift+Enter 换行)": { zh: "输入消息... (Enter 发送, Shift+Enter 换行)", en: "Type a message... (Enter to send, Shift+Enter for newline)" },
    "例如：每天早上 8 点发送今日新闻摘要": { zh: "例如：每天早上 8 点发送今日新闻摘要", en: "e.g. Send today's news summary at 8am daily" },
    "搜索对话内容...": { zh: "搜索对话内容...", en: "Search conversations..." },
    "搜索记忆内容...": { zh: "搜索记忆内容...", en: "Search memories..." },
    "搜索知识内容...": { zh: "搜索知识内容...", en: "Search knowledge..." },
    "添加附件": { zh: "添加附件", en: "Add attachment" },
    "已停止": { zh: "已停止", en: "Stopped" },
    "已启用": { zh: "已启用", en: "Enabled" },

    // ── 设置面板 option 选项 ──
    "阿里云 DashScope (通义千问)": { zh: "阿里云 DashScope (通义千问)", en: "Alibaba DashScope (Tongyi)" },
    "DeepSeek (深度求索)": { zh: "DeepSeek (深度求索)", en: "DeepSeek" },
    "智谱 BigModel (GLM)": { zh: "智谱 BigModel (GLM)", en: "Zhipu BigModel (GLM)" },
    "Moonshot (Kimi)": { zh: "Moonshot (Kimi)", en: "Moonshot (Kimi)" },
    "火山引擎 (豆包)": { zh: "火山引擎 (豆包)", en: "Volcano (Doubao)" },
    "OpenAI (GPT)": { zh: "OpenAI (GPT)", en: "OpenAI (GPT)" },
    "Anthropic Claude": { zh: "Anthropic Claude", en: "Anthropic Claude" },
    "Google Gemini": { zh: "Google Gemini", en: "Google Gemini" },
    "OpenRouter (聚合平台)": { zh: "OpenRouter (聚合平台)", en: "OpenRouter (Aggregator)" },
    "🧠 本地离线模型 (bge-small-zh-v1.5 · 无需API · 推荐)": { zh: "🧠 本地离线模型 (bge-small-zh-v1.5 · 无需API · 推荐)", en: "🧠 Local offline (bge-small-zh-v1.5 · no API · recommended)" },
    "不启用向量检索（纯文本检索）": { zh: "不启用向量检索（纯文本检索）", en: "Disable vector search (text only)" },
    "不启用图像生成": { zh: "不启用图像生成", en: "Disable image generation" },
    "不启用视觉理解": { zh: "不启用视觉理解", en: "Disable vision" },
    "关闭（直接执行）": { zh: "关闭（直接执行）", en: "Off (direct exec)" },
    "非主会话沙箱": { zh: "非主会话沙箱", en: "Non-main sessions" },
    "全部沙箱（Docker 隔离）": { zh: "全部沙箱（Docker 隔离）", en: "All (Docker isolated)" },

    // ── 设置面板描述文字 (helper text) ──
    "开启后 Agent 在每次工具调用前输出推理过程": { zh: "开启后 Agent 在每次工具调用前输出推理过程", en: "Agent outputs reasoning before each tool call" },
    "决策者模型负责思考+工具选择，执行者模型生成最终回复，节省 token": { zh: "决策者模型负责思考+工具选择，执行者模型生成最终回复，节省 token", en: "Thinker does reasoning+tools, executor generates reply, saves tokens" },
    "建议：决策者用强模型（如 qwen-max），执行者用便宜模型（如 qwen-turbo）": { zh: "建议：决策者用强模型（如 qwen-max），执行者用便宜模型（如 qwen-turbo）", en: "Tip: use a strong model for thinker, cheap model for executor" },
    "主模型 403/429/超时时自动切换，留空则不启用": { zh: "主模型 403/429/超时时自动切换，留空则不启用", en: "Auto-fallback on 403/429/timeout; leave empty to disable" },
    "对话前召回相关记忆，自动保存重要信息": { zh: "对话前召回相关记忆，自动保存重要信息", en: "Recall relevant memories before chat, auto-save important info" },
    "SQLite 存储，重启后恢复对话": { zh: "SQLite 存储，重启后恢复对话", en: "SQLite storage, restores chat after restart" },
    "自动压缩+剪枝，防止 token 溢出": { zh: "自动压缩+剪枝，防止 token 溢出", en: "Auto compress+prune to prevent token overflow" },
    "SKILL.md 触发匹配，自动加载技能": { zh: "SKILL.md 触发匹配，自动加载技能", en: "SKILL.md triggers matching, auto-loads skills" },
    "读取 AGENT.md/USER.md/RULE.md 增强身份": { zh: "读取 AGENT.md/USER.md/RULE.md 增强身份", en: "Reads AGENT.md/USER.md/RULE.md for identity" },
    "发布/订阅，模块间松耦合通信": { zh: "发布/订阅，模块间松耦合通信", en: "Pub/sub, loosely coupled communication" },
    "关闭后危险操作需要人工确认": { zh: "关闭后危险操作需要人工确认", en: "When off, dangerous ops need manual approval" },
    "未知用户需配对码审批后才能使用": { zh: "未知用户需配对码审批后才能使用", en: "Unknown users need pairing approval" },
    "启动时使用上次会话的模型配置": { zh: "启动时使用上次会话的模型配置", en: "Use last session's model config on startup" },
    "启动时自动加载上次关闭的会话": { zh: "启动时自动加载上次关闭的会话", en: "Auto-load last closed session on startup" },
    "0.0.0.0 表示监听所有网卡，127.0.0.1 仅本机访问": { zh: "0.0.0.0 表示监听所有网卡，127.0.0.1 仅本机访问", en: "0.0.0.0 listens on all NICs, 127.0.0.1 local only" },
    "⚠️ 修改后需重启服务生效": { zh: "⚠️ 修改后需重启服务生效", en: "⚠️ Restart service to apply" },
    "支持飞书、微信、Telegram 等主流平台，配置后可直接在这些平台上与 Scout 对话": { zh: "支持飞书、微信、Telegram 等主流平台，配置后可直接在这些平台上与 Scout 对话", en: "Supports Feishu/WeChat/Telegram etc., chat with Scout on these platforms" },
    "ReAct: 单 Agent 循环思考行动 · Multi-Agent: 主 Agent 分解任务并委派子代理": { zh: "ReAct: 单 Agent 循环思考行动 · Multi-Agent: 主 Agent 分解任务并委派子代理", en: "ReAct: single agent loop · Multi-Agent: main agent delegates to sub-agents" },
    "选择 Agent 回复时使用的语言（跟随用户 = 用户说什么语言就用什么语言）": { zh: "选择 Agent 回复时使用的语言（跟随用户 = 用户说什么语言就用什么语言）", en: "Choose reply language (follow user = match user's input)" },
    "决策者模型（思考+工具选择）": { zh: "决策者模型（思考+工具选择）", en: "Thinker model (reasoning + tools)" },
    "执行者模型（生成回复）": { zh: "执行者模型（生成回复）", en: "Executor model (generates reply)" },
    "备用模型（主模型失败时自动降级）": { zh: "备用模型（主模型失败时自动降级）", en: "Fallback model (auto-degrade on failure)" },
    "危险命令检测": { zh: "危险命令检测", en: "Dangerous command detection" },
    "✅ rm -rf / → 拦截": { zh: "✅ rm -rf / → 拦截", en: "✅ rm -rf / → blocked" },
    "✅ dd if= → 拦截": { zh: "✅ dd if= → 拦截", en: "✅ dd if= → blocked" },
    "✅ mkfs → 拦截": { zh: "✅ mkfs → 拦截", en: "✅ mkfs → blocked" },
    "✅ curl | sh → 拦截": { zh: "✅ curl | sh → 拦截", en: "✅ curl | sh → blocked" },
    "... 共 13 种危险模式": { zh: "... 共 13 种危险模式", en: "... 13 dangerous patterns total" },
    "DM 配对授权": { zh: "DM 配对授权", en: "DM Pairing Authorization" },
    "启用配对码授权": { zh: "启用配对码授权", en: "Enable pairing code auth" },
    "✅ Docker 可用": { zh: "✅ Docker 可用", en: "✅ Docker available" },
    "活跃沙箱: 0 个": { zh: "活跃沙箱: 0 个", en: "Active sandboxes: 0" },
    "Docker 未安装": { zh: "Docker 未安装", en: "Docker not installed" },
    // ── channels 页 ──
    "连接外部聊天平台，让 Scout 在更多地方为您服务": { zh: "连接外部聊天平台，让 Scout 在更多地方为您服务", en: "Connect external chat platforms to use Scout everywhere" },
    "飞书": { zh: "飞书", en: "Feishu" },
    "微信": { zh: "微信", en: "WeChat" },
    "微信公众号": { zh: "微信公众号", en: "WeChat Official" },
    "企业微信": { zh: "企业微信", en: "WeCom" },
    "企微群机器人": { zh: "企微群机器人", en: "WeCom Group Bot" },
    "微信客服": { zh: "微信客服", en: "WeChat Customer Service" },
    "个人微信": { zh: "个人微信", en: "Personal WeChat" },
    "钉钉": { zh: "钉钉", en: "DingTalk" },
    // ── version 页 ──
    "支持多模型配置（DashScope、OpenAI 等）": { zh: "支持多模型配置（DashScope、OpenAI 等）", en: "Multi-model support (DashScope, OpenAI, etc.)" },
    "集成记忆系统、工具调用、插件扩展": { zh: "集成记忆系统、工具调用、插件扩展", en: "Memory, tools, plugins integrated" },
    "支持多渠道接入（飞书、微信、Telegram 等）": { zh: "支持多渠道接入（飞书、微信、Telegram 等）", en: "Multi-channel (Feishu, WeChat, Telegram, etc.)" },
    "提供 Web UI 和 RESTful API": { zh: "提供 Web UI 和 RESTful API", en: "Web UI and RESTful API" },
    "检查更新": { zh: "检查更新", en: "Check for updates" },
    "查看所有版本": { zh: "查看所有版本", en: "View all versions" },
    "✅ Docker 可用 | 活跃沙箱: ": { zh: "✅ Docker 可用 | 活跃沙箱: ", en: "✅ Docker available | Active sandboxes: " },
    " 个": { zh: " 个", en: "" },
    "无法获取沙箱状态": { zh: "无法获取沙箱状态", en: "Unable to get sandbox status" },
    "⚠️ Docker 未安装 — 沙箱不可用，所有命令将在本地直接执行": { zh: "⚠️ Docker 未安装 — 沙箱不可用，所有命令将在本地直接执行", en: "⚠️ Docker not installed — sandbox unavailable, commands run locally" },
    "✅ Docker 可用 | 活跃沙箱: 0 个": { zh: "✅ Docker 可用 | 活跃沙箱: 0 个", en: "✅ Docker available | Active sandboxes: 0" },
    "使用主模型": { zh: "使用主模型", en: "Use main model" },
    "不启用": { zh: "不启用", en: "Disabled" },
    "✏️ 自定义模型名…": { zh: "✏️ 自定义模型名…", en: "✏️ Custom model name…" },
    "自定义模型名": { zh: "自定义模型名", en: "Custom model name" },
    "(自定义模型)": { zh: "(自定义模型)", en: "(custom model)" },
    "(自定义)": { zh: "(自定义)", en: "(custom)" },
    "视觉理解模型 ": { zh: "视觉理解模型 ", en: "Vision Model " },
    "视觉理解模型": { zh: "视觉理解模型", en: "Vision Model" },
    "图像生成模型 ": { zh: "图像生成模型 ", en: "Image Model " },
    "图像生成模型": { zh: "图像生成模型", en: "Image Model" },
    "Embedding 模型 ": { zh: "Embedding 模型 ", en: "Embedding Model " },
    "Embedding 模型": { zh: "Embedding 模型", en: "Embedding Model" },
    "(图片识别、OCR、图表分析)": { zh: "(图片识别、OCR、图表分析)", en: "(image recognition, OCR, charts)" },
    "(文生图)": { zh: "(文生图)", en: "(text-to-image)" },
    "(记忆向量化检索)": { zh: "(记忆向量化检索)", en: "(memory vector search)" },
    "理解": { zh: "理解", en: "Understanding" },
    "输入自定义模型名，回车确认（Esc 取消）": { zh: "输入自定义模型名，回车确认（Esc 取消）", en: "Enter custom model name, Enter to confirm (Esc to cancel)" },
    "已配置": { zh: "已配置", en: "Configured" },
    "本地无记忆，重新输入后可自动记忆": { zh: "本地无记忆，重新输入后可自动记忆", en: "No local memory; re-enter to auto-remember" },
    "✅ 已记忆 (": { zh: "✅ 已记忆 (", en: "✅ Remembered (" },
    "✅ 已记忆 ": { zh: "✅ 已记忆 ", en: "✅ Remembered " },
    "✅ 已配置 (": { zh: "✅ 已配置 (", en: "✅ Configured (" },
    "✅ 已配置 ": { zh: "✅ 已配置 ", en: "✅ Configured " },
    " — 本地无记忆，重新输入后可自动记忆": { zh: " — 本地无记忆，重新输入后可自动记忆", en: " — no local memory; re-enter to auto-remember" },
    ") — 本地无记忆，重新输入后可自动记忆": { zh: ") — 本地无记忆，重新输入后可自动记忆", en: ") — no local memory; re-enter to auto-remember" },
    "未配置 API Key": { zh: "未配置 API Key", en: "API Key not configured" },
    "⚠️ 未配置 API Key": { zh: "⚠️ 未配置 API Key", en: "⚠️ API Key not configured" },
    "\" 吗？": { zh: "\" 吗？", en: "\"? " },
    "\" 吗？此操作不可撤销。": { zh: "\" 吗？此操作不可撤销。", en: "\"? This action cannot be undone." },
    "\" 找到 ": { zh: "\" 找到 ", en: "\" found " },
    "⚠️ 该服务商未配置 API Key，请在上方\"服务商 API 配置\"中填写": { zh: "⚠️ 该服务商未配置 API Key，请在上方\"服务商 API 配置\"中填写", en: "⚠️ No API Key for this provider; fill it in the \"Provider API Config\" section above" },
    "」吗？\n将删除 ~/.scout/skills/": { zh: "」吗？\n将删除 ~/.scout/skills/", en: "」? \nwill delete ~/.scout/skills/" },
    "搜索 \"": { zh: "搜索 \"", en: "Search \"" },
    "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。": { zh: "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。", en: "For \"branching by reverting to a message\": only copies up to the specified sequence number, then restarts from the branch." },
    "确定要停止渠道 \"": { zh: "确定要停止渠道 \"", en: "Stop channel \"" },
    "确定要删除任务 \"": { zh: "确定要删除任务 \"", en: "Delete task \"" },
    "确定要删除渠道 \"": { zh: "确定要删除渠道 \"", en: "Delete channel \"" },
    "确定要删除知识页面 \"": { zh: "确定要删除知识页面 \"", en: "Delete knowledge page \"" },
    "过滤器JSON(可选)，如 {\"source\":\"cron:*\"}": { zh: "过滤器JSON(可选)，如 {\"source\":\"cron:*\"}", en: "Filter JSON (optional), e.g. {\"source\":\"cron:*\"}" },
    "验证规则JSON(可选)，如 [{\"type\":\"contains\",\"value\":[\"完成\"]}]": { zh: "验证规则JSON(可选)，如 [{\"type\":\"contains\",\"value\":[\"完成\"]}]", en: "Verify rule JSON (optional), e.g. [{\"type\":\"contains\",\"value\":[\"done\"]}]" },

    " 个失败": { zh: " 个失败", en: " failed" },
    " 个已保存，": { zh: " 个已保存，", en: " saved, " },
    "\" 吗？": { zh: "\" 吗？", en: "\"? " },
    "\" 吗？此操作不可撤销。": { zh: "\" 吗？此操作不可撤销。", en: "\"? This action cannot be undone." },
    "\" 找到 ": { zh: "\" 找到 ", en: "\" found " },
    "(集中配置各服务商的 API Key 与 Base URL)": { zh: "(集中配置各服务商的 API Key 与 Base URL)", en: "(Centralized API Key & Base URL config for all providers)" },
    "Base URL": { zh: "Base URL", en: "Base URL" },
    "⚠️ 该服务商未配置 API Key，请在上方\"服务商 API 配置\"中填写": { zh: "⚠️ 该服务商未配置 API Key，请在上方\"服务商 API 配置\"中填写", en: "⚠️ No API Key for this provider; fill it in the \"Provider API Config\" section above" },
    "✅ 该服务商已配置 API Key": { zh: "✅ 该服务商已配置 API Key", en: "✅ API Key configured for this provider" },
    "」吗？\n将删除 ~/.scout/skills/": { zh: "」吗？\n将删除 ~/.scout/skills/", en: "」? \nwill delete ~/.scout/skills/" },
    "保存服务商配置": { zh: "保存服务商配置", en: "Save Provider Config" },
    "在此统一配置各服务商的 API Key 与 Base URL，文本/视觉/图像模型模块只需选择服务商和模型即可自动使用对应凭据。": { zh: "在此统一配置各服务商的 API Key 与 Base URL，文本/视觉/图像模型模块只需选择服务商和模型即可自动使用对应凭据。", en: "Configure API Keys & Base URLs for all providers here; the text/vision/image sections only need to select a provider and model." },
    "已保存，留空则不修改": { zh: "已保存，留空则不修改", en: "Saved; leave blank to keep unchanged" },
    "搜索 \"": { zh: "搜索 \"", en: "Search \"" },
    "服务商 API 配置": { zh: "服务商 API 配置", en: "Provider API Config" },
    "服务商配置已保存": { zh: "服务商配置已保存", en: "Provider config saved" },
    "未填写任何服务商凭据": { zh: "未填写任何服务商凭据", en: "No provider credentials entered" },
    "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。": { zh: "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。", en: "For \"branching by reverting to a message\": only copies up to the specified sequence number, then restarts from the branch." },
    "确定要停止渠道 \"": { zh: "确定要停止渠道 \"", en: "Stop channel \"" },
    "确定要删除任务 \"": { zh: "确定要删除任务 \"", en: "Delete task \"" },
    "确定要删除渠道 \"": { zh: "确定要删除渠道 \"", en: "Delete channel \"" },
    "确定要删除知识页面 \"": { zh: "确定要删除知识页面 \"", en: "Delete knowledge page \"" },
    "过滤器JSON(可选)，如 {\"source\":\"cron:*\"}": { zh: "过滤器JSON(可选)，如 {\"source\":\"cron:*\"}", en: "Filter JSON (optional), e.g. {\"source\":\"cron:*\"}" },
    "验证规则JSON(可选)，如 [{\"type\":\"contains\",\"value\":[\"完成\"]}]": { zh: "验证规则JSON(可选)，如 [{\"type\":\"contains\",\"value\":[\"完成\"]}]", en: "Verify rule JSON (optional), e.g. [{\"type\":\"contains\",\"value\":[\"done\"]}]" },

    "\" 吗？": { zh: "\" 吗？", en: "\"? " },
    "\" 吗？此操作不可撤销。": { zh: "\" 吗？此操作不可撤销。", en: "\"? This action cannot be undone." },
    "\" 找到 ": { zh: "\" 找到 ", en: "\" found " },
    "SMTP 主机 (如 smtp.qq.com)": { zh: "SMTP 主机 (如 smtp.qq.com)", en: "SMTP host (e.g. smtp.qq.com)" },
    "SMTP 密码/授权码": { zh: "SMTP 密码/授权码", en: "SMTP password / auth code" },
    "SMTP 账号": { zh: "SMTP 账号", en: "SMTP account" },
    "事件名，如: tool.complete / file": { zh: "事件名，如: tool.complete / file", en: "Event name, e.g. tool.complete / file" },
    "任务描述（必填）——收到请求时 Agent 执行的任务。支持 {{event.xxx}} 占位符引用请求体字段。": { zh: "任务描述（必填）——收到请求时 Agent 执行的任务。支持 {{event.xxx}} 占位符引用请求体字段。", en: "Task description (required) - task executed by Agent when request received. Supports {{event.xxx}} placeholders referencing request body fields." },
    "任务描述（给 Agent 执行）": { zh: "任务描述（给 Agent 执行）", en: "Task description (for Agent to execute)" },
    "任务模板，支持 {{event.xxx}} 占位符。如: 处理新文件 {{event.file_path}}": { zh: "任务模板，支持 {{event.xxx}} 占位符。如: 处理新文件 {{event.file_path}}", en: "Task template supporting {{event.xxx}} placeholders. E.g. handle new file {{event.file_path}}" },
    "例如：创建一个插件，当用户说“帮助”时显示帮助信息": { zh: "例如：创建一个插件，当用户说“帮助”时显示帮助信息", en: "E.g. create a plugin that shows help when the user says \"help\"" },
    "冷却秒数(防抖)，默认0": { zh: "冷却秒数(防抖)，默认0", en: "Cooldown seconds (debounce), default 0" },
    "删除该触发器？": { zh: "删除该触发器？", en: "Delete this trigger?" },
    "名称（可选，如 部署通知）": { zh: "名称（可选，如 部署通知）", en: "Name (optional, e.g. deploy notification)" },
    "名称，如: PR审查": { zh: "名称，如: PR审查", en: "Name, e.g. PR review" },
    "插件名称（小写字母和下划线，如：my_plugin）": { zh: "插件名称（小写字母和下划线，如：my_plugin）", en: "Plugin name (lowercase letters and underscores, e.g. my_plugin)" },
    "搜索 \"": { zh: "搜索 \"", en: "Search \"" },
    "收件人邮箱": { zh: "收件人邮箱", en: "Recipient email" },
    "查看配置": { zh: "查看配置", en: "View config" },
    "测试触发": { zh: "测试触发", en: "Test trigger" },
    "渠道名 (如 telegram)": { zh: "渠道名 (如 telegram)", en: "Channel name (e.g. telegram)" },
    "生成的代码将显示在这里...": { zh: "生成的代码将显示在这里...", en: "Generated code will be shown here..." },
    "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。": { zh: "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。", en: "For \"branching by reverting to a message\": only copies up to the specified sequence number, then restarts from the branch." },
    "目标 ID / 群组": { zh: "目标 ID / 群组", en: "Target ID / group" },
    "确定要停止渠道 \"": { zh: "确定要停止渠道 \"", en: "Stop channel \"" },
    "确定要删除任务 \"": { zh: "确定要删除任务 \"", en: "Delete task \"" },
    "确定要删除渠道 \"": { zh: "确定要删除渠道 \"", en: "Delete channel \"" },
    "确定要删除知识页面 \"": { zh: "确定要删除知识页面 \"", en: "Delete knowledge page \"" },
    "确认删除该 Webhook？": { zh: "确认删除该 Webhook？", en: "Delete this Webhook?" },
    "确认清空全部推送历史？": { zh: "确认清空全部推送历史？", en: "Clear all push history?" },
    "确认清空死信队列？": { zh: "确认清空死信队列？", en: "Clear dead-letter queue?" },
    "确认移除该监听目录？": { zh: "确认移除该监听目录？", en: "Remove this watch directory?" },
    "端口 (465)": { zh: "端口 (465)", en: "Port (465)" },
    "绝对路径，如 /data/inbox": { zh: "绝对路径，如 /data/inbox", en: "Absolute path, e.g. /data/inbox" },
    "编辑配置": { zh: "编辑配置", en: "Edit config" },
    "调度，如: 每30分钟 / 每天09:00": { zh: "调度，如: 每30分钟 / 每天09:00", en: "Schedule, e.g. every 30 minutes / daily 09:00" },
    "过滤器JSON(可选)，如 {\"source\":\"cron:*\"}": { zh: "过滤器JSON(可选)，如 {\"source\":\"cron:*\"}", en: "Filter JSON (optional), e.g. {\"source\":\"cron:*\"}" },
    "重新加载": { zh: "重新加载", en: "Reload" },
    "验证规则JSON(可选)，如 [{\"type\":\"contains\",\"value\":[\"完成\"]}]": { zh: "验证规则JSON(可选)，如 [{\"type\":\"contains\",\"value\":[\"完成\"]}]", en: "Verify rule JSON (optional), e.g. [{\"type\":\"contains\",\"value\":[\"done\"]}]" },

    "\" 吗？": { zh: "\" 吗？", en: "\"? " },
    "\" 吗？此操作不可撤销。": { zh: "\" 吗？此操作不可撤销。", en: "\"? This action cannot be undone." },
    "\" 找到 ": { zh: "\" 找到 ", en: "\" found " },
    "分叉此会话": { zh: "分叉此会话", en: "Fork this session" },
    "搜索 \"": { zh: "搜索 \"", en: "Search \"" },
    "点击查看大图": { zh: "点击查看大图", en: "Click to view full image" },
    "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。": { zh: "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。", en: "For \"branching by reverting to a message\": only copies up to the specified sequence number, then restarts from the branch." },
    "确定要停止渠道 \"": { zh: "确定要停止渠道 \"", en: "Stop channel \"" },
    "确定要删除任务 \"": { zh: "确定要删除任务 \"", en: "Delete task \"" },
    "确定要删除渠道 \"": { zh: "确定要删除渠道 \"", en: "Delete channel \"" },
    "确定要删除知识页面 \"": { zh: "确定要删除知识页面 \"", en: "Delete knowledge page \"" },
    "编辑标题": { zh: "编辑标题", en: "Edit title" },

    "\" 吗？": { zh: "\" 吗？", en: "\"? " },
    "\" 吗？此操作不可撤销。": { zh: "\" 吗？此操作不可撤销。", en: "\"? This action cannot be undone." },
    "\" 找到 ": { zh: "\" 找到 ", en: "\" found " },
    "API Key（可选；searxng / duckduckgo 无需）": { zh: "API Key（可选；searxng / duckduckgo 无需）", en: "API Key (optional; not needed for searxng / duckduckgo)" },
    "URL（留空用默认端点；Google 需带 ?cx=你的ID）": { zh: "URL（留空用默认端点；Google 需带 ?cx=你的ID）", en: "URL (leave empty for default endpoint; Google needs ?cx=yourID)" },
    "任务与追踪": { zh: "任务与追踪", en: "Tasks & Tracking" },
    "任务描述，如：总结最新消息": { zh: "任务描述，如：总结最新消息", en: "Task description, e.g. summarize the latest messages" },
    "再次输入密码": { zh: "再次输入密码", en: "Re-enter password" },
    "切换界面语言 / Toggle UI language": { zh: "切换界面语言 / Toggle UI language", en: "Toggle UI language" },
    "删除此源": { zh: "删除此源", en: "Delete this source" },
    "加载选中页面": { zh: "加载选中页面", en: "Load selected page" },
    "名称，如 本地 SearXNG": { zh: "名称，如 本地 SearXNG", en: "Name, e.g. local SearXNG" },
    "命令": { zh: "命令", en: "Command" },
    "在这里输入 Markdown 内容...": { zh: "在这里输入 Markdown 内容...", en: "Enter Markdown content here..." },
    "工具与监控": { zh: "工具与监控", en: "Tools & Monitoring" },
    "引用": { zh: "引用", en: "Quote" },
    "搜索 \"": { zh: "搜索 \"", en: "Search \"" },
    "斜体": { zh: "斜体", en: "Italic" },
    "新建页面": { zh: "新建页面", en: "New page" },
    "查看详情": { zh: "查看详情", en: "View details" },
    "标题": { zh: "标题", en: "Heading" },
    "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。": { zh: "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。", en: "For \"branching by reverting to a message\": only copies up to the specified sequence number, then restarts from the branch." },
    "留空 = 复制全部消息": { zh: "留空 = 复制全部消息", en: "Leave empty = copy all messages" },
    "留空则自动生成 (会话标题 + fork)": { zh: "留空则自动生成 (会话标题 + fork)", en: "Leave empty to auto-generate (session title + fork)" },
    "登录用户名": { zh: "登录用户名", en: "Login username" },
    "确定要停止渠道 \"": { zh: "确定要停止渠道 \"", en: "Stop channel \"" },
    "确定要删除任务 \"": { zh: "确定要删除任务 \"", en: "Delete task \"" },
    "确定要删除渠道 \"": { zh: "确定要删除渠道 \"", en: "Delete channel \"" },
    "确定要删除知识页面 \"": { zh: "确定要删除知识页面 \"", en: "Delete knowledge page \"" },
    "类别标签 (可选)": { zh: "类别标签 (可选)", en: "Category label (optional)" },
    "粗体": { zh: "粗体", en: "Bold" },
    "至少 6 位": { zh: "至少 6 位", en: "At least 6 characters" },
    "访问次数": { zh: "访问次数", en: "Access count" },
    "输入要记住的内容...": { zh: "输入要记住的内容...", en: "Enter content to remember..." },
    "重要性": { zh: "重要性", en: "Importance" },
    "链接": { zh: "链接", en: "Link" },

    " | 上次运行: ": { zh: " | 上次运行: ", en: " | Last run: " },
    "\" 吗？": { zh: "\" 吗？", en: "\"? " },
    "\" 吗？此操作不可撤销。": { zh: "\" 吗？此操作不可撤销。", en: "\"? This action cannot be undone." },
    "\" 找到 ": { zh: "\" 找到 ", en: "\" found " },
    "任务: ": { zh: "任务: ", en: "Tasks: " },
    "分叉": { zh: "分叉", en: "Branch" },
    "创建: ": { zh: "创建: ", en: "Created: " },
    "向量模型": { zh: "向量模型", en: "Vector model" },
    "启用登录密码": { zh: "启用登录密码", en: "Enable login password" },
    "子分支:": { zh: "子分支:", en: "Child branch:" },
    "导入失败": { zh: "导入失败", en: "Import failed" },
    "导入成功": { zh: "导入成功", en: "Import successful" },
    "尚未运行过凝萃": { zh: "尚未运行过凝萃", en: "Distillation has not run yet" },
    "工具: ": { zh: "工具: ", en: "Tools: " },
    "工具配置": { zh: "工具配置", en: "Tool configuration" },
    "已注册工具": { zh: "已注册工具", en: "Registered tools" },
    "开启后访问 Web 界面需要输入用户名和密码登录；默认关闭": { zh: "开启后访问 Web 界面需要输入用户名和密码登录；默认关闭", en: "When enabled, accessing the Web UI requires a username and password login; disabled by default" },
    "强制提取": { zh: "强制提取", en: "Force extraction" },
    "总成本": { zh: "总成本", en: "Total cost" },
    "执行步骤 (": { zh: "执行步骤 (", en: "Execution steps (" },
    "拖拽文件到此处，或": { zh: "拖拽文件到此处，或", en: "Drag files here, or" },
    "插件": { zh: "插件", en: "Plugin" },
    "搜索 \"": { zh: "搜索 \"", en: "Search \"" },
    "搜索引擎源（支持多个）": { zh: "搜索引擎源（支持多个）", en: "Search engine sources (multiple)" },
    "支持 PDF、DOCX、Markdown、TXT、CSV、JSON、HTML、代码文件": { zh: "支持 PDF、DOCX、Markdown、TXT、CSV、JSON、HTML、代码文件", en: "Supports PDF, DOCX, Markdown, TXT, CSV, JSON, HTML, and code files" },
    "数据": { zh: "数据", en: "Data" },
    "文档": { zh: "文档", en: "Document" },
    "新会话标题": { zh: "新会话标题", en: "New session title" },
    "无血缘记录": { zh: "无血缘记录", en: "No lineage records" },
    "暂无 Webhook": { zh: "暂无 Webhook", en: "No webhooks" },
    "最小对话数（低于此数跳过提取）": { zh: "最小对话数（低于此数跳过提取）", en: "Minimum conversations (below this, extraction is skipped)" },
    "最近追踪": { zh: "最近追踪", en: "Recent traces" },
    "未修改": { zh: "未修改", en: "Unmodified" },
    "构建知识图谱...": { zh: "构建知识图谱...", en: "Building knowledge graph..." },
    "步骤: ": { zh: "步骤: ", en: "Step: " },
    "消息: ": { zh: "消息: ", en: "Message: " },
    "点击选择": { zh: "点击选择", en: "click to select" },
    "状态: ": { zh: "状态: ", en: "Status: " },
    "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。": { zh: "用于\"回退到某条消息再分叉\"：只复制到指定序号为止，之后从分支重新开始。", en: "For \"branching by reverting to a message\": only copies up to the specified sequence number, then restarts from the branch." },
    "登录认证": { zh: "登录认证", en: "Login authentication" },
    "确定要停止渠道 \"": { zh: "确定要停止渠道 \"", en: "Stop channel \"" },
    "确定要删除任务 \"": { zh: "确定要删除任务 \"", en: "Delete task \"" },
    "确定要删除渠道 \"": { zh: "确定要删除渠道 \"", en: "Delete channel \"" },
    "确定要删除知识页面 \"": { zh: "确定要删除知识页面 \"", en: "Delete knowledge page \"" },
    "确认密码": { zh: "确认密码", en: "Confirm password" },
    "编排中...": { zh: "编排中...", en: "Orchestrating..." },
    "耗时: ": { zh: "耗时: ", en: "Duration: " },
    "血缘关系": { zh: "血缘关系", en: "Lineage" },
    "血缘加载失败: ": { zh: "血缘加载失败: ", en: "Lineage load failed: " },
    "调度: ": { zh: "调度: ", en: "Schedule: " },
    "进度: ": { zh: "进度: ", en: "Progress: " },
    "配置至少一个启用的源后": { zh: "配置至少一个启用的源后", en: "After configuring at least one enabled source" },
    "预算: ": { zh: "预算: ", en: "Budget: " },

    "\" 吗？": { zh: "\" 吗？", en: "\"? " },
    "\" 吗？此操作不可撤销。": { zh: "\" 吗？此操作不可撤销。", en: "\"? This action cannot be undone." },
    "\" 找到 ": { zh: "\" 找到 ", en: "\" found " },
    "任务描述 (可选):": { zh: "任务描述 (可选):", en: "Task description (optional):" },
    "任务标题:": { zh: "任务标题:", en: "Task title:" },
    "搜索 \"": { zh: "搜索 \"", en: "Search \"" },
    "目标描述 (可选):": { zh: "目标描述 (可选):", en: "Target description (optional):" },
    "目标标题:": { zh: "目标标题:", en: "Target title:" },
    "确定要停止渠道 \"": { zh: "确定要停止渠道 \"", en: "Stop channel \"" },
    "确定要删除任务 \"": { zh: "确定要删除任务 \"", en: "Delete task \"" },
    "确定要删除渠道 \"": { zh: "确定要删除渠道 \"", en: "Delete channel \"" },
    "确定要删除知识页面 \"": { zh: "确定要删除知识页面 \"", en: "Delete knowledge page \"" },
    "请输入搜索关键词:": { zh: "请输入搜索关键词:", en: "Enter a search keyword:" },

    "\" 吗？": { zh: "\" 吗？", en: "\"? " },
    "\" 吗？此操作不可撤销。": { zh: "\" 吗？此操作不可撤销。", en: "\"? This action cannot be undone." },
    "\" 找到 ": { zh: "\" 找到 ", en: "\" found " },
    "⏳ 运行中...": { zh: "⏳ 运行中...", en: "⏳ Running..." },
    "⚡ 无委派子任务（主代理直接执行）": { zh: "⚡ 无委派子任务（主代理直接执行）", en: "⚡ No delegated subtasks (main agent executes directly)" },
    "✅ 完成": { zh: "✅ 完成", en: "✅ Done" },
    "✅ 已完成": { zh: "✅ 已完成", en: "✅ Completed" },
    "❌ 失败": { zh: "❌ 失败", en: "❌ Failed" },
    "❌ 请先填写自定义模型名或选择预设模型": { zh: "❌ 请先填写自定义模型名或选择预设模型", en: "❌ Please enter a custom model name or select a preset model" },
    "两次输入的密码不一致": { zh: "两次输入的密码不一致", en: "Passwords do not match" },
    "完成": { zh: "完成", en: "Done" },
    "密码长度至少 6 位": { zh: "密码长度至少 6 位", en: "Password must be at least 6 characters" },
    "已保存": { zh: "已保存", en: "Saved" },
    "搜索 \"": { zh: "搜索 \"", en: "Search \"" },
    "未保存": { zh: "未保存", en: "Unsaved" },
    "未知错误": { zh: "未知错误", en: "Unknown error" },
    "确定要停止渠道 \"": { zh: "确定要停止渠道 \"", en: "Stop channel \"" },
    "确定要删除任务 \"": { zh: "确定要删除任务 \"", en: "Delete task \"" },
    "确定要删除渠道 \"": { zh: "确定要删除渠道 \"", en: "Delete channel \"" },
    "确定要删除知识页面 \"": { zh: "确定要删除知识页面 \"", en: "Delete knowledge page \"" },
    "网络错误": { zh: "网络错误", en: "Network error" },
    "请输入用户名": { zh: "请输入用户名", en: "Please enter a username" },
    "首次使用，请设置账户": { zh: "首次使用，请设置账户", en: "First time use, please set up an account" },

    " (累计 ": { zh: " (累计 ", en: " (cumulative " },
    " 个子任务，并行委派：": { zh: " 个子任务，并行委派：", en: " subtasks, delegating in parallel: " },
    " 个工具 ": { zh: " 个工具 ", en: " tools " },
    " 个文件已导入": { zh: " 个文件已导入", en: " files imported" },
    " 个文件：": { zh: " 个文件：", en: " files: " },
    " 字符": { zh: " 字符", en: " characters" },
    " 条结果：": { zh: " 条结果：", en: " results: " },
    " 页": { zh: " 页", en: " pages" },
    "\" 吗？": { zh: "\" 吗？", en: "\"? " },
    "\" 吗？此操作不可撤销。": { zh: "\" 吗？此操作不可撤销。", en: "\"? This action cannot be undone." },
    "\" 找到 ": { zh: "\" 找到 ", en: "\" found " },
    ") 解析 ": { zh: ") 解析 ", en: ") parsing " },
    "... 还有 ": { zh: "... 还有 ", en: "... and " },
    "· 有新版本 ": { zh: "· 有新版本 ", en: "· New version " },
    "⚠️ 渲染失败: ": { zh: "⚠️ 渲染失败: ", en: "⚠️ Render failed: " },
    "✅ 完成 · ": { zh: "✅ 完成 · ", en: "✅ Done · " },
    "✅ 成功导入 ": { zh: "✅ 成功导入 ", en: "✅ Imported " },
    "❌ 错误: ": { zh: "❌ 错误: ", en: "❌ Error: " },
    "」相关内容": { zh: "」相关内容", en: "\"" },
    "任务分解为 ": { zh: "任务分解为 ", en: "Task breakdown: " },
    "凝萃失败: ": { zh: "凝萃失败: ", en: "Distillation failed: " },
    "子任务 ": { zh: "子任务 ", en: "Subtask " },
    "当前登录用户：": { zh: "当前登录用户：", en: "Current user: " },
    "执行工具 ": { zh: "执行工具 ", en: "Running tool " },
    "搜索 \"": { zh: "搜索 \"", en: "Search \"" },
    "暂无 MCP 服务器": { zh: "暂无 MCP 服务器", en: "No MCP servers" },
    "最后访问": { zh: "最后访问", en: "Last accessed" },
    "未找到「": { zh: "未找到「", en: "No results for \"" },
    "标记完成": { zh: "标记完成", en: "Mark complete" },
    "步": { zh: "步", en: " steps" },
    "步骤 ": { zh: "步骤 ", en: "Step " },
    "确定要停止渠道 \"": { zh: "确定要停止渠道 \"", en: "Stop channel \"" },
    "确定要删除任务 \"": { zh: "确定要删除任务 \"", en: "Delete task \"" },
    "确定要删除渠道 \"": { zh: "确定要删除渠道 \"", en: "Delete channel \"" },
    "确定要删除知识页面 \"": { zh: "确定要删除知识页面 \"", en: "Delete knowledge page \"" },
    "缓存 ": { zh: "缓存 ", en: "Cache " },
    "调度器: ": { zh: "调度器: ", en: "Scheduler: " },
    "部分成功：": { zh: "部分成功：", en: "Partial success: " },
    "（密码已设置，修改后立即生效）": { zh: "（密码已设置，修改后立即生效）", en: " (password set, effective immediately)" },

    " 个目标": { zh: " 个目标", en: " targets" },
    " 超过 20MB 限制": { zh: " 超过 20MB 限制", en: " exceeds the 20MB limit" },
    "\" 吗？": { zh: "\" 吗？", en: "\"? " },
    "\" 吗？此操作不可撤销。": { zh: "\" 吗？此操作不可撤销。", en: "\"? This action cannot be undone." },
    "启动失败: ": { zh: "启动失败: ", en: "Start failed: " },
    "启动失败，请重试": { zh: "启动失败，请重试", en: "Start failed, please retry" },
    "已自动识别 ": { zh: "已自动识别 ", en: "Auto-detected " },
    "确定删除「": { zh: "确定删除「", en: "Delete \"" },
    "确定要停止渠道 \"": { zh: "确定要停止渠道 \"", en: "Stop channel \"" },
    "确定要删除任务 \"": { zh: "确定要删除任务 \"", en: "Delete task \"" },
    "确定要删除渠道 \"": { zh: "确定要删除渠道 \"", en: "Delete channel \"" },
    "确定要删除知识页面 \"": { zh: "确定要删除知识页面 \"", en: "Delete knowledge page \"" },
    "请填写 ": { zh: "请填写 ", en: "Please fill in " },

    " 条）": { zh: " 条）", en: " items)" },
    " 模板": { zh: " 模板", en: " template" },
    " 次 · 最近 ": { zh: " 次 · 最近 ", en: " times · recent " },
    " 次工具调用": { zh: " 次工具调用", en: " tool calls" },
    " 次运行": { zh: " 次运行", en: " runs" },
    " 步": { zh: " 步", en: " steps" },
    "(无消息)": { zh: "(无消息)", en: "(no message)" },
    "(未命名)": { zh: "(未命名)", en: "(unnamed)" },
    "24h 对话 ": { zh: "24h 对话 ", en: "24h conversations " },
    "✕ 删除": { zh: "✕ 删除", en: "✕ Delete" },
    "上游 ": { zh: "上游 ", en: "Upstream " },
    "事件 ": { zh: "事件 ", en: "Event " },
    "事件载荷": { zh: "事件载荷", en: "Event payload" },
    "仅顶层": { zh: "仅顶层", en: "Top-level only" },
    "作者: ": { zh: "作者: ", en: "Author: " },
    "修改": { zh: "修改", en: "Modify" },
    "停用": { zh: "停用", en: "Disable" },
    "关键词: ": { zh: "关键词: ", en: "Keywords: " },
    "内容：": { zh: "内容：", en: "Content:" },
    "去抖 ": { zh: "去抖 ", en: "Debounce " },
    "复制 URL": { zh: "复制 URL", en: "Copy URL" },
    "已加载 ": { zh: "已加载 ", en: "Loaded " },
    "已推送": { zh: "已推送", en: "Pushed" },
    "已过滤": { zh: "已过滤", en: "Filtered" },
    "恢复": { zh: "恢复", en: "Restore" },
    "成功率 ": { zh: "成功率 ", en: "Success rate " },
    "执行中...": { zh: "执行中...", en: "Running..." },
    "执行事件流（": { zh: "执行事件流（", en: "Event flow (" },
    "技能": { zh: "技能", en: "Skill" },
    "拒绝": { zh: "拒绝", en: "Rejected" },
    "新增": { zh: "新增", en: "Add" },
    "无 span 数据": { zh: "无 span 数据", en: "No span data" },
    "暂无描述": { zh: "暂无描述", en: "No description" },
    "最近 ": { zh: "最近 ", en: "recent " },
    "未推送": { zh: "未推送", en: "Not pushed" },
    "未通过": { zh: "未通过", en: "failed" },
    "正则: ": { zh: "正则: ", en: "Regex: " },
    "状态 ": { zh: "状态 ", en: "Status " },
    "触发 ": { zh: "触发 ", en: "Triggered " },
    "调用 ": { zh: "调用 ", en: "Called " },
    "轮询 ": { zh: "轮询 ", en: "Poll every " },
    "输入:": { zh: "输入:", en: "Input:" },
    "输出:": { zh: "输出:", en: "Output:" },
    "运行": { zh: "运行", en: "Run" },
    "近7天共 ": { zh: "近7天共 ", en: "Last 7 days: " },
    "递归": { zh: "递归", en: "Recursive" },
    "通过": { zh: "通过", en: "passed" },
    "错误: ": { zh: "错误: ", en: "Error: " },
    "验证证据（": { zh: "验证证据（", en: "Verification evidence (" },
    "）": { zh: "）", en: ")" },
    "👁️ 详情": { zh: "👁️ 详情", en: "👁️ Details" },
    "📧 邮件✓": { zh: "📧 邮件✓", en: "📧 Email✓" },

    " 配置": { zh: " 配置", en: " Config" },

    "加载失败: ": { zh: "加载失败: ", en: "Load failed: " },

    "%)": { zh: "%)", en: "%)" },
    "(成功率": { zh: "(成功率", en: "(success rate" },
    "」？": { zh: "」？", en: "」?" },
    "删除定时任务「": { zh: "删除定时任务「", en: "Delete scheduled task 「" },
    "运行结果: ": { zh: "运行结果: ", en: "Run result: " },

    "- 优先级 (0-100)": { zh: "- 优先级 (0-100)", en: "- priority (0-100)" },
    "- 关键词列表": { zh: "- 关键词列表", en: "- keyword list" },
    "- 响应映射": { zh: "- 响应映射", en: "- response mapping" },
    "- 是否启用插件": { zh: "- 是否启用插件", en: "- whether the plugin is enabled" },
    "」吗？\n将删除 ~/.scout/skills/": { zh: "」吗？\n将删除 ~/.scout/skills/", en: "」? \nwill delete ~/.scout/skills/" },

    " 轮": { zh: " 轮", en: " rounds" },
    "该会话没有追踪数据": { zh: "该会话没有追踪数据", en: "No trace data for this session" },
    "轮": { zh: "轮", en: " rounds" },

    " - 优先级 (0-100)": { zh: " - 优先级 (0-100)", en: " - priority (0-100)" },
    " - 关键词列表": { zh: " - 关键词列表", en: " - keyword list" },
    " - 响应映射": { zh: " - 响应映射", en: " - response mapping" },
    " - 是否启用插件": { zh: " - 是否启用插件", en: " - whether the plugin is enabled" },
    "1. 可以编辑生成的代码进行自定义调整": { zh: "1. 可以编辑生成的代码进行自定义调整", en: "1. You can edit the generated code for customization" },
    "2. 插件名称只能包含小写字母、数字和下划线": { zh: "2. 插件名称只能包含小写字母、数字和下划线", en: "2. Plugin name may only contain lowercase letters, digits and underscores" },
    "3. 保存后的插件会自动启用，可在插件管理页面查看": { zh: "3. 保存后的插件会自动启用，可在插件管理页面查看", en: "3. Saved plugins auto-enable; view them in Plugin Management" },
    "已添加监听目录": { zh: "已添加监听目录", en: "Watch directory added" },
    "添加失败: ": { zh: "添加失败: ", en: "Add failed: " },

    " 次": { zh: " 次", en: " runs" },
    " 目录": { zh: " 目录", en: " directory" },
    "24h 对话": { zh: "24h 对话", en: "24h conversations" },
    "⚡缓存": { zh: "⚡缓存", en: "⚡ cached" },
    "✗ 配置格式错误:": { zh: "✗ 配置格式错误:", en: "✗ Invalid config format:" },
    "」吗？": { zh: "」吗？", en: "」?" },
    "上次执行": { zh: "上次执行", en: "last run" },
    "下次执行": { zh: "下次执行", en: "next run" },
    "事件名称": { zh: "事件名称", en: "event name" },
    "事件处理": { zh: "事件处理", en: "event processing" },
    "事件数据": { zh: "事件数据", en: "event data" },
    "保存": { zh: "保存", en: "save" },
    "全部": { zh: "全部", en: "all" },
    "内容": { zh: "内容", en: "content" },
    "分钟": { zh: "分钟", en: " min" },
    "卸载": { zh: "卸载", en: "uninstall" },
    "名称": { zh: "名称", en: "name" },
    "否": { zh: "否", en: "No" },
    "启用状态": { zh: "启用状态", en: "enabled" },
    "响应": { zh: "响应", en: "response" },
    "在线": { zh: "在线", en: "online" },
    "大小": { zh: "大小", en: "size" },
    "天": { zh: "天", en: "d" },
    "失败": { zh: "失败", en: "failed" },
    "字段": { zh: "字段", en: "field" },
    "安装": { zh: "安装", en: "install" },
    "将删除 ~/.scout/skills/": { zh: "将删除 ~/.scout/skills/", en: "will delete ~/.scout/skills/" },
    "小时": { zh: "小时", en: "h" },
    "工具类型:": { zh: "工具类型:", en: "Tool types:" },
    "已复制到剪贴板": { zh: "已复制到剪贴板", en: "copied to clipboard" },
    "已完成": { zh: "已完成", en: "completed" },
    "已终止": { zh: "已终止", en: "terminated" },
    "已跳过": { zh: "已跳过", en: "skipped" },
    "忽略": { zh: "忽略", en: "ignore" },
    "总会话数:": { zh: "总会话数:", en: "Total sessions:" },
    "成功": { zh: "成功", en: "success" },
    "成功率": { zh: "成功率", en: "Success rate" },
    "成本": { zh: "成本", en: "Cost" },
    "执行": { zh: "执行", en: "execute" },
    "推送": { zh: "推送", en: "push" },
    "描述": { zh: "描述", en: "description" },
    "提交": { zh: "提交", en: "submit" },
    "摘要": { zh: "摘要", en: "summary" },
    "操作": { zh: "操作", en: "actions" },
    "敏感词": { zh: "敏感词", en: "sensitive word" },
    "数量": { zh: "数量", en: "count" },
    "日期": { zh: "日期", en: "date" },
    "时间": { zh: "时间", en: "time" },
    "是": { zh: "是", en: "Yes" },
    "暂无数据": { zh: "暂无数据", en: "No data" },
    "更新时间": { zh: "更新时间", en: "updated" },
    "最低": { zh: "最低", en: "min" },
    "最高": { zh: "最高", en: "max" },
    "本地无记忆": { zh: "本地无记忆", en: "no local memory" },
    "查看": { zh: "查看", en: "view" },
    "核心": { zh: "核心", en: " cores" },
    "次": { zh: "次", en: " runs" },
    "次 · 最近": { zh: "次 · 最近", en: " runs · recent" },
    "次运行/天": { zh: "次运行/天", en: "runs/day" },
    "正在加载": { zh: "正在加载", en: "Loading" },
    "步 ·": { zh: "步 ·", en: " steps ·" },
    "测试": { zh: "测试", en: "test" },
    "目录": { zh: "目录", en: "directory" },
    "确定": { zh: "确定", en: "OK" },
    "确定要卸载技能「": { zh: "确定要卸载技能「", en: "Uninstall skill 「" },
    "确认": { zh: "确认", en: "confirm" },
    "离线": { zh: "离线", en: "offline" },
    "等待中": { zh: "等待中", en: "pending" },
    "类型": { zh: "类型", en: "type" },
    "编辑": { zh: "编辑", en: "edit" },
    "花费": { zh: "花费", en: "spent" },
    "触发": { zh: "触发", en: "trigger" },
    "详情": { zh: "详情", en: "details" },
    "请求失败": { zh: "请求失败", en: "Request failed" },
    "调用": { zh: "调用", en: "calls" },
    "路径": { zh: "路径", en: "path" },
    "输入": { zh: "输入", en: "input" },
    "输出": { zh: "输出", en: "output" },
    "运行时间:": { zh: "运行时间:", en: "Uptime:" },
    "进行中": { zh: "进行中", en: "running" },
    "通知": { zh: "通知", en: "notify" },
    "通知中心": { zh: "通知中心", en: "Notification Center" },
    "重新输入后可自动记忆": { zh: "重新输入后可自动记忆", en: "re-enter to auto-remember" },
    "重试": { zh: "重试", en: "retry" },
    "键": { zh: "键", en: "key" },
    "预览": { zh: "预览", en: "preview" },

    " 分钟": { zh: " 分钟", en: " min" },
    " 条": { zh: " 条", en: " entries" },
    " 核心": { zh: " 核心", en: " cores" },
    "0 核心": { zh: "0 核心", en: "0 cores" },
    "AI 插件生成器 - Scout": { zh: "AI 插件生成器 - Scout", en: "AI Plugin Builder - Scout" },
    "AI 正在生成插件代码，请稍候...": { zh: "AI 正在生成插件代码，请稍候...", en: "AI is generating plugin code, please wait..." },
    "AI 生成的插件": { zh: "AI 生成的插件", en: "AI-generated plugin" },
    "CPU 使用率": { zh: "CPU 使用率", en: "CPU Usage" },
    "CPU 使用率 (%)": { zh: "CPU 使用率 (%)", en: "CPU Usage (%)" },
    "EventBus · 模块间松耦合通信": { zh: "EventBus · 模块间松耦合通信", en: "EventBus · loose-coupling inter-module communication" },
    "IM 渠道目标": { zh: "IM 渠道目标", en: "IM channel target" },
    "JSON 格式错误": { zh: "JSON 格式错误", en: "Invalid JSON format" },
    "SKILL.md 内容已复制": { zh: "SKILL.md 内容已复制", en: "SKILL.md content copied" },
    "Webhook 管理 · Scout Agent": { zh: "Webhook 管理 · Scout Agent", en: "Webhook Management · Scout Agent" },
    "auto — 只读放行，写操作需白名单（推荐）": { zh: "auto — 只读放行，写操作需白名单（推荐）", en: "auto — read-only allowed, writes require whitelist (recommended)" },
    "created 新增": { zh: "created 新增", en: "created" },
    "deleted 删除": { zh: "deleted 删除", en: "deleted" },
    "event 触发器": { zh: "event 触发器", en: "event trigger" },
    "modified 修改": { zh: "modified 修改", en: "modified" },
    "never — 全部放行（危险命令仍硬拦截）": { zh: "never — 全部放行（危险命令仍硬拦截）", en: "never — allow all (dangerous commands still blocked)" },
    "prompt — 白名单外拒绝+通知（保守）": { zh: "prompt — 白名单外拒绝+通知（保守）", en: "prompt — reject non-whitelisted + notify (conservative)" },
    "shell 命令正则白名单（逗号分隔）": { zh: "shell 命令正则白名单（逗号分隔）", en: "shell command regex whitelist (comma-separated)" },
    "writes — 文件写放行，高危需白名单": { zh: "writes — 文件写放行，高危需白名单", en: "writes — file writes allowed, high-risk needs whitelist" },
    "· 状态": { zh: "· 状态", en: "· status" },
    "← 返回首页": { zh: "← 返回首页", en: "← Back to Home" },
    "← 选择一个会话查看执行时间线": { zh: "← 选择一个会话查看执行时间线", en: "← Select a session to view the execution timeline" },
    "⏳ 安装中...": { zh: "⏳ 安装中...", en: "⏳ Installing..." },
    "⏳ 搜索中...": { zh: "⏳ 搜索中...", en: "⏳ Searching..." },
    "⏳ 生成中...": { zh: "⏳ 生成中...", en: "⏳ Generating..." },
    "⚡ 触发方式": { zh: "⚡ 触发方式", en: "⚡ Trigger type" },
    "✅ 已安装": { zh: "✅ 已安装", en: "✅ Installed" },
    "✓ 已保存": { zh: "✓ 已保存", en: "✓ Saved" },
    "✓ 配置格式正确": { zh: "✓ 配置格式正确", en: "✓ Config format is valid" },
    "✗ 配置格式错误: ": { zh: "✗ 配置格式错误: ", en: "✗ Invalid config format: " },
    "✨ 生成插件代码": { zh: "✨ 生成插件代码", en: "✨ Generate plugin code" },
    "❌ 停止": { zh: "❌ 停止", en: "❌ Stopped" },
    "➕ 创建插件": { zh: "➕ 创建插件", en: "➕ Create Plugin" },
    "下午好！希望您今天过得愉快！": { zh: "下午好！希望您今天过得愉快！", en: "Good afternoon! Hope you have a nice day!" },
    "个可复用方案": { zh: "个可复用方案", en: "reusable solutions" },
    "事件处理失败的记录": { zh: "事件处理失败的记录", en: "Records of failed event processing" },
    "事件总线 · Scout Agent": { zh: "事件总线 · Scout Agent", en: "Event Bus · Scout Agent" },
    "事件流": { zh: "事件流", en: "Event stream" },
    "事件类型：": { zh: "事件类型：", en: "Event type:" },
    "事件触发器": { zh: "事件触发器", en: "Event trigger" },
    "事件触发（订阅 EventBus 事件）": { zh: "事件触发（订阅 EventBus 事件）", en: "Event trigger (subscribe to EventBus events)" },
    "今日": { zh: "今日", en: "Today" },
    "从网上搜到的 Skill（SKILL.md）安装到这里，对话时自动触发": { zh: "从网上搜到的 Skill（SKILL.md）安装到这里，对话时自动触发", en: "Skills found online (SKILL.md) install here and trigger automatically in conversation" },
    "仓库根地址": { zh: "仓库根地址", en: "Repository root URL" },
    "任务完成": { zh: "任务完成", en: "Task completed" },
    "任务异常": { zh: "任务异常", en: "Task error" },
    "优先级: ": { zh: "优先级: ", en: "Priority: " },
    "作用域: ": { zh: "作用域: ", en: "Scope: " },
    "作者：": { zh: "作者：", en: "Author:" },
    "你好": { zh: "你好", en: "Hello" },
    "使用 SSL/TLS": { zh: "使用 SSL/TLS", en: "Use SSL/TLS" },
    "保存偏好": { zh: "保存偏好", en: "Save preferences" },
    "保存后插件会自动重新加载": { zh: "保存后插件会自动重新加载", en: "Plugins auto-reload after saving" },
    "保存失败：": { zh: "保存失败：", en: "Save failed:" },
    "保存策略": { zh: "保存策略", en: "Save policy" },
    "保存配置失败": { zh: "保存配置失败", en: "Failed to save config" },
    "保存配置失败:": { zh: "保存配置失败:", en: "Failed to save config:" },
    "先输入功能描述，再搜索现成方案": { zh: "先输入功能描述，再搜索现成方案", en: "Enter a feature description first, then search existing solutions" },
    "全年": { zh: "全年", en: "Full year" },
    "全部事件": { zh: "全部事件", en: "All events" },
    "关键词-响应映射配置": { zh: "关键词-响应映射配置", en: "Keyword-response mapping config" },
    "关键词插件": { zh: "关键词插件", en: "Keyword plugin" },
    "关闭": { zh: "关闭", en: "Close" },
    "关闭后所有跨渠道推送暂停": { zh: "关闭后所有跨渠道推送暂停", en: "Cross-channel push pauses when off" },
    "其他": { zh: "其他", en: "Other" },
    "内存使用率": { zh: "内存使用率", en: "Memory Usage" },
    "内存使用率 (%)": { zh: "内存使用率 (%)", en: "Memory Usage (%)" },
    "切换插件状态失败:": { zh: "切换插件状态失败:", en: "Failed to toggle plugin state:" },
    "创建一个": { zh: "创建一个", en: "Create a" },
    "前往": { zh: "前往", en: "Go to" },
    "加载技能失败:": { zh: "加载技能失败:", en: "Failed to load skills:" },
    "加载插件失败": { zh: "加载插件失败", en: "Failed to load plugins" },
    "加载插件失败:": { zh: "加载插件失败:", en: "Failed to load plugins:" },
    "加载配置失败": { zh: "加载配置失败", en: "Failed to load config" },
    "加载配置失败:": { zh: "加载配置失败:", en: "Failed to load config:" },
    "危险操作时推送通知": { zh: "危险操作时推送通知", en: "Push notification on dangerous operations" },
    "卸载失败": { zh: "卸载失败", en: "Uninstall failed" },
    "卸载失败:": { zh: "卸载失败:", en: "Uninstall failed:" },
    "去抖": { zh: "去抖", en: "debounce" },
    "去重窗口 (秒)": { zh: "去重窗口 (秒)", en: "Deduplication window (sec)" },
    "反思": { zh: "反思", en: "Reflection" },
    "发送测试通知": { zh: "发送测试通知", en: "Send test notification" },
    "可选，用于重要通知的邮件备份": { zh: "可选，用于重要通知的邮件备份", en: "Optional, email backup for important notifications" },
    "启动监听": { zh: "启动监听", en: "Start listening" },
    "启用通知推送": { zh: "启用通知推送", en: "Enable notification push" },
    "在任务模板中使用占位符引用文件信息，例如：": { zh: "在任务模板中使用占位符引用文件信息，例如：", en: "Use placeholders in task templates to reference file info, e.g.:" },
    "在左侧添加要监听的目录（支持递归）。": { zh: "在左侧添加要监听的目录（支持递归）。", en: "Add directories to watch on the left (recursive supported)." },
    "基础模板": { zh: "基础模板", en: "Basic template" },
    "安装失败 (HTTP": { zh: "安装失败 (HTTP", en: "Install failed (HTTP" },
    "安装失败:": { zh: "安装失败:", en: "Install failed:" },
    "安装成功！": { zh: "安装成功！", en: "Install succeeded!" },
    "安装超时（仓库可能过大或网络慢）": { zh: "安装超时（仓库可能过大或网络慢）", en: "Install timeout (repo may be too large or network slow)" },
    "定时触发": { zh: "定时触发", en: "Scheduled trigger" },
    "实时监控 Scout Agent 运行状态": { zh: "实时监控 Scout Agent 运行状态", en: "Real-time monitoring of Scout Agent status" },
    "审批策略 approval_policy": { zh: "审批策略 approval_policy", en: "Approval policy (approval_policy)" },
    "工具": { zh: "工具", en: "Tools" },
    "工具白名单（逗号分隔，空=不限）": { zh: "工具白名单（逗号分隔，空=不限）", en: "Tool whitelist (comma-separated, empty=unlimited)" },
    "工具类型: ": { zh: "工具类型: ", en: "Tool types: " },
    "工具类型: 0": { zh: "工具类型: 0", en: "Tool types: 0" },
    "工具调用": { zh: "工具调用", en: "Tool calls" },
    "工具黑名单": { zh: "工具黑名单", en: "Tool blacklist" },
    "已加载": { zh: "已加载", en: "Loaded" },
    "已卸载": { zh: "已卸载", en: "Unloaded" },
    "已取消：插件创建需要密码确认": { zh: "已取消：插件创建需要密码确认", en: "Cancelled: plugin creation requires password confirmation" },
    "已复制:": { zh: "已复制:", en: "Copied:" },
    "已清空 ": { zh: "已清空 ", en: "Cleared " },
    "已禁用": { zh: "已禁用", en: "Disabled" },
    "已选中，请手动复制 (Ctrl+C)": { zh: "已选中，请手动复制 (Ctrl+C)", en: "Selected, please copy manually (Ctrl+C)" },
    "帮助": { zh: "帮助", en: "Help" },
    "常用字段": { zh: "常用字段", en: "Common fields" },
    "平均延迟": { zh: "平均延迟", en: "Avg latency" },
    "建议使用验证功能检查配置": { zh: "建议使用验证功能检查配置", en: "Use the validate feature to check config" },
    "总 Token": { zh: "总 Token", en: "Total Tokens" },
    "总 Token 消耗": { zh: "总 Token 消耗", en: "Total token usage" },
    "总会话数: ": { zh: "总会话数: ", en: "Total sessions: " },
    "总会话数: 0": { zh: "总会话数: 0", en: "Total sessions: 0" },
    "总插件数": { zh: "总插件数", en: "Total plugins" },
    "总调用次数": { zh: "总调用次数", en: "Total calls" },
    "您好！有什么可以帮助您的吗？": { zh: "您好！有什么可以帮助您的吗？", en: "Hello! How can I help you?" },
    "我可以帮您解答问题、完成任务等。": { zh: "我可以帮您解答问题、完成任务等。", en: "I can answer questions, complete tasks, etc." },
    "手动触发": { zh: "手动触发", en: "Manual trigger" },
    "技能详情": { zh: "技能详情", en: "Skill details" },
    "按模型统计": { zh: "按模型统计", en: "By model" },
    "按类型开关": { zh: "按类型开关", en: "Toggle by type" },
    "捕获的文件变化（可配合触发器实现自动处理）": { zh: "捕获的文件变化（可配合触发器实现自动处理）", en: "Captured file changes (can auto-process with triggers)" },
    "控制哪些类型的通知值得推送": { zh: "控制哪些类型的通知值得推送", en: "Control which notification types are worth pushing" },
    "推送历史": { zh: "推送历史", en: "Push history" },
    "推送规则": { zh: "推送规则", en: "Push rules" },
    "描述你想要的插件功能，AI 会自动生成完整的插件代码": { zh: "描述你想要的插件功能，AI 会自动生成完整的插件代码", en: "Describe the plugin feature you want; AI will generate complete plugin code" },
    "提醒": { zh: "提醒", en: "Reminder" },
    "插件不存在": { zh: "插件不存在", en: "Plugin not found" },
    "插件代码不能为空": { zh: "插件代码不能为空", en: "Plugin code cannot be empty" },
    "插件代码生成成功！": { zh: "插件代码生成成功！", en: "Plugin code generated!" },
    "插件保存成功！": { zh: "插件保存成功！", en: "Plugin saved!" },
    "插件名称只能包含小写字母、数字和下划线，且必须以字母开头": { zh: "插件名称只能包含小写字母、数字和下划线，且必须以字母开头", en: "Plugin name must contain only lowercase letters, digits, underscores and start with a letter" },
    "插件管理 - Scout Agent": { zh: "插件管理 - Scout Agent", en: "Plugin Management - Scout Agent" },
    "插件配置": { zh: "插件配置", en: "Plugin config" },
    "插件配置编辑": { zh: "插件配置编辑", en: "Plugin config editor" },
    "插件配置编辑 - Scout Agent": { zh: "插件配置编辑 - Scout Agent", en: "Plugin Config Editor - Scout Agent" },
    "搜索失败:": { zh: "搜索失败:", en: "Search failed:" },
    "操作失败": { zh: "操作失败", en: "Operation failed" },
    "敏感词1": { zh: "敏感词1", en: "Sensitive word 1" },
    "敏感词2": { zh: "敏感词2", en: "Sensitive word 2" },
    "敏感词过滤配置": { zh: "敏感词过滤配置", en: "Sensitive word filter config" },
    "文件监听 · Scout Agent": { zh: "文件监听 · Scout Agent", en: "File Watcher · Scout Agent" },
    "新建 Webhook": { zh: "新建 Webhook", en: "New Webhook" },
    "新建定时任务": { zh: "新建定时任务", en: "New scheduled task" },
    "新建触发器": { zh: "新建触发器", en: "New trigger" },
    "无人值守策略": { zh: "无人值守策略", en: "Unattended policy" },
    "无描述": { zh: "无描述", en: "No description" },
    "早上好！新的一天开始了！": { zh: "早上好！新的一天开始了！", en: "Good morning! A new day begins!" },
    "时间段问候配置": { zh: "时间段问候配置", en: "Time-of-day greeting config" },
    "晚上好！注意休息哦！": { zh: "晚上好！注意休息哦！", en: "Good evening! Rest well!" },
    "暂无事件": { zh: "暂无事件", en: "No events" },
    "更新 Agent 状态失败:": { zh: "更新 Agent 状态失败:", en: "Failed to update agent status:" },
    "更新插件状态失败:": { zh: "更新插件状态失败:", en: "Failed to update plugin state:" },
    "更新系统状态失败:": { zh: "更新系统状态失败:", en: "Failed to update system status:" },
    "最低级别": { zh: "最低级别", en: "Minimum level" },
    "最多保留 100 条": { zh: "最多保留 100 条", en: "Keep at most 100 entries" },
    "最多保留 500 条": { zh: "最多保留 500 条", en: "Keep at most 500 entries" },
    "最近 30 天": { zh: "最近 30 天", en: "Last 30 days" },
    "最近会话（按 trace 聚合）": { zh: "最近会话（按 trace 聚合）", en: "Recent sessions (aggregated by trace)" },
    "最近调用记录": { zh: "最近调用记录", en: "Recent call records" },
    "未加载": { zh: "未加载", en: "Not loaded" },
    "未指定插件名称": { zh: "未指定插件名称", en: "Plugin name not specified" },
    "未知": { zh: "未知", en: "Unknown" },
    "本周": { zh: "本周", en: "This week" },
    "格式化": { zh: "格式化", en: "Format" },
    "格式化成功": { zh: "格式化成功", en: "Formatted" },
    "模型监控": { zh: "模型监控", en: "Model Monitor" },
    "模型监控 · Scout Agent": { zh: "模型监控 · Scout Agent", en: "Model Monitor · Scout Agent" },
    "模板": { zh: "模板", en: "template" },
    "次运行": { zh: "次运行", en: " runs" },
    "每日 Token 消耗趋势": { zh: "每日 Token 消耗趋势", en: "Daily token usage trend" },
    "注意事项": { zh: "注意事项", en: "Notes" },
    "活跃会话": { zh: "活跃会话", en: "Active sessions" },
    "点击「启动监听」，agent 会持续感知该目录的文件新增/修改/删除。": { zh: "点击「启动监听」，agent 会持续感知该目录的文件新增/修改/删除。", en: "Click \"Start listening\"; the agent keeps sensing file add/modify/delete in this directory." },
    "版本: ": { zh: "版本: ", en: "Version: " },
    "版本：": { zh: "版本：", en: "Version:" },
    "状态": { zh: "状态", en: "Status" },
    "状态：": { zh: "状态：", en: "Status:" },
    "生成失败": { zh: "生成失败", en: "Generation failed" },
    "生成失败：": { zh: "生成失败：", en: "Generation failed:" },
    "登录已过期，请重新登录": { zh: "登录已过期，请重新登录", en: "Login expired, please log in again" },
    "监听目录": { zh: "监听目录", en: "Watch directories" },
    "目录: ": { zh: "目录: ", en: "Directory: " },
    "目标": { zh: "目标", en: "Target" },
    "确定要重新加载所有插件吗？": { zh: "确定要重新加载所有插件吗？", en: "Reload all plugins?" },
    "确定要重置配置吗？这将恢复到上次保存的状态。": { zh: "确定要重置配置吗？这将恢复到上次保存的状态。", en: "Reset config? This restores the last saved state." },
    "磁盘使用率": { zh: "磁盘使用率", en: "Disk Usage" },
    "空配置，适合自定义": { zh: "空配置，适合自定义", en: "Empty config, good for customization" },
    "管理插件 →": { zh: "管理插件 →", en: "Manage plugins →" },
    "系统": { zh: "系统", en: "System" },
    "系统监控 - Scout Agent": { zh: "系统监控 - Scout Agent", en: "System Monitor - Scout Agent" },
    "级联触发（上游完成后执行）": { zh: "级联触发（上游完成后执行）", en: "Cascade trigger (runs after upstream completes)" },
    "缓存命中": { zh: "缓存命中", en: "Cache hit" },
    "网络速率": { zh: "网络速率", en: "Network speed" },
    "自动化中心": { zh: "自动化中心", en: "Automation Center" },
    "自动化中心 · Scout Agent": { zh: "自动化中心 · Scout Agent", en: "Automation Center · Scout Agent" },
    "请输入插件功能描述": { zh: "请输入插件功能描述", en: "Enter plugin feature description" },
    "请输入插件名称": { zh: "请输入插件名称", en: "Enter plugin name" },
    "请输入登录密码以确认插件创建（写码操作需二次验证）": { zh: "请输入登录密码以确认插件创建（写码操作需二次验证）", en: "Enter login password to confirm plugin creation (write operation needs second verification)" },
    "调用次数": { zh: "调用次数", en: "Call count" },
    "路径: ": { zh: "路径: ", en: "Path: " },
    "轮询": { zh: "轮询", en: "poll" },
    "过滤插件": { zh: "过滤插件", en: "Filter plugins" },
    "运行历史": { zh: "运行历史", en: "Run history" },
    "运行时间: ": { zh: "运行时间: ", en: "Uptime: " },
    "运行时间: 0 小时": { zh: "运行时间: 0 小时", en: "Uptime: 0 hours" },
    "运行观测 · Scout Agent": { zh: "运行观测 · Scout Agent", en: "Run Observer · Scout Agent" },
    "运行观测 →": { zh: "运行观测 →", en: "Run Observer →" },
    "运行观测时间线": { zh: "运行观测时间线", en: "Run observer timeline" },
    "近7天共": { zh: "近7天共", en: "Last 7 days: " },
    "返回主页": { zh: "返回主页", en: "Back to home" },
    "返回插件列表": { zh: "返回插件列表", en: "Back to plugin list" },
    "选择上游触发器…": { zh: "选择上游触发器…", en: "Select upstream trigger..." },
    "通知中心 · Scout Agent": { zh: "通知中心 · Scout Agent", en: "Notification Center · Scout Agent" },
    "通知将主动推送到这些渠道": { zh: "通知将主动推送到这些渠道", en: "Notifications are actively pushed to these channels" },
    "邮件推送 (SMTP)": { zh: "邮件推送 (SMTP)", en: "Email push (SMTP)" },
    "配置保存成功": { zh: "配置保存成功", en: "Config saved" },
    "配置已重置": { zh: "配置已重置", en: "Config reset" },
    "配置文件 (config.json)": { zh: "配置文件 (config.json)", en: "Config file (config.json)" },
    "配置文件必须是有效的 JSON 格式": { zh: "配置文件必须是有效的 JSON 格式", en: "Config file must be valid JSON" },
    "配置格式": { zh: "配置格式", en: "Config format" },
    "配置格式错误，无法保存": { zh: "配置格式错误，无法保存", en: "Invalid config format, cannot save" },
    "配置格式错误，无法格式化": { zh: "配置格式错误，无法格式化", en: "Invalid config format, cannot format" },
    "配置模板": { zh: "配置模板", en: "Config template" },
    "配置说明": { zh: "配置说明", en: "Config description" },
    "配置错误会导致插件无法加载": { zh: "配置错误会导致插件无法加载", en: "Config errors prevent plugins from loading" },
    "重新加载失败": { zh: "重新加载失败", en: "Reload failed" },
    "重新加载所有插件失败:": { zh: "重新加载所有插件失败:", en: "Failed to reload all plugins:" },
    "重新加载插件失败:": { zh: "重新加载插件失败:", en: "Failed to reload plugin:" },
    "重置": { zh: "重置", en: "Reset" },
    "问候插件": { zh: "问候插件", en: "Greeting plugin" },
    "验证配置": { zh: "验证配置", en: "Validate config" },
    "（无内容）": { zh: "（无内容）", en: "(empty)" },
    "（无描述）": { zh: "（无描述）", en: "(no description)" },
    "，事件名填": { zh: "，事件名填", en: ", fill in the event name" },
    "🌐 网页": { zh: "🌐 网页", en: "🌐 Web" },
    "👁️ 文件监听": { zh: "👁️ 文件监听", en: "👁️ File Watch" },
    "👋 智能问候": { zh: "👋 智能问候", en: "👋 Smart Greeting" },
    "💀 死信队列 (DLQ)": { zh: "💀 死信队列 (DLQ)", en: "💀 Dead Letter Queue (DLQ)" },
    "💡 提示": { zh: "💡 提示", en: "💡 Tip" },
    "💬 帮助信息": { zh: "💬 帮助信息", en: "💬 Help" },
    "💻 系统资源": { zh: "💻 系统资源", en: "💻 System Resources" },
    "💾 保存插件": { zh: "💾 保存插件", en: "💾 Save Plugin" },
    "📄 SKILL.md 内容": { zh: "📄 SKILL.md 内容", en: "📄 SKILL.md Content" },
    "📅 日期查询": { zh: "📅 日期查询", en: "📅 Date Query" },
    "📈 CPU 使用历史": { zh: "📈 CPU 使用历史", en: "📈 CPU History" },
    "📈 内存使用历史": { zh: "📈 内存使用历史", en: "📈 Memory History" },
    "📊 工具监控": { zh: "📊 工具监控", en: "📊 Tool Monitor" },
    "📊 模板: 每日自我报告": { zh: "📊 模板: 每日自我报告", en: "📊 Template: Daily Self Report" },
    "📊 系统监控": { zh: "📊 系统监控", en: "📊 System Monitor" },
    "📋 复制全文": { zh: "📋 复制全文", en: "📋 Copy All" },
    "📋 复制命令": { zh: "📋 复制命令", en: "📋 Copy Command" },
    "📚 已安装技能": { zh: "📚 已安装技能", en: "📚 Installed Skills" },
    "📝 描述": { zh: "📝 描述", en: "📝 Description" },
    "📝 描述插件功能": { zh: "📝 描述插件功能", en: "📝 Describe Plugin Feature" },
    "📡 事件总线": { zh: "📡 事件总线", en: "📡 Event Bus" },
    "📦 一键安装": { zh: "📦 一键安装", en: "📦 One-click Install" },
    "📦 全网找到": { zh: "📦 全网找到", en: "📦 Found Online" },
    "📰 模板: 每日资讯简报": { zh: "📰 模板: 每日资讯简报", en: "📰 Template: Daily News Brief" },
    "🔄 刷新数据": { zh: "🔄 刷新数据", en: "🔄 Refresh" },
    "🔄 重新加载全部": { zh: "🔄 重新加载全部", en: "🔄 Reload All" },
    "🔄 重新生成": { zh: "🔄 重新生成", en: "🔄 Regenerate" },
    "🔍 先搜全网现成 Skill/插件": { zh: "🔍 先搜全网现成 Skill/插件", en: "🔍 Search Online for Ready-made Skills/Plugins" },
    "🔔 通知中心": { zh: "🔔 通知中心", en: "🔔 Notification Center" },
    "🔗 Webhook 管理": { zh: "🔗 Webhook 管理", en: "🔗 Webhook Management" },
    "🔗 查看仓库": { zh: "🔗 查看仓库", en: "🔗 View Repo" },
    "🔧 插件代码": { zh: "🔧 插件代码", en: "🔧 Plugin Code" },
    "🚫 广告过滤": { zh: "🚫 广告过滤", en: "🚫 Ad Blocker" },
    "🤖 AI 插件生成器": { zh: "🤖 AI 插件生成器", en: "🤖 AI Plugin Builder" },
    "🤖 Agent 状态": { zh: "🤖 Agent 状态", en: "🤖 Agent Status" },
    "🧩 插件状态": { zh: "🧩 插件状态", en: "🧩 Plugin Status" },
    "🧩 插件管理": { zh: "🧩 插件管理", en: "🧩 Plugin Management" },

    // ===== 补漏：各页面空状态 / 长文本 / 标签（2026-08 截图补拍）=====
    "暂无触发器 — 在上方创建一个，让 Scout 响应事件自动干活": { zh: "暂无触发器 — 在上方创建一个，让 Scout 响应事件自动干活", en: "No triggers yet — create one above to let Scout respond to events automatically" },
    "自动化任务（触发器/Webhook/定时）在无人值守时的工具权限策略。组织级策略（/etc/scout/requirements.json）会强制覆盖用户配置。": { zh: "自动化任务（触发器/Webhook/定时）在无人值守时的工具权限策略。组织级策略（/etc/scout/requirements.json）会强制覆盖用户配置。", en: "Tool permission policy for automation tasks (triggers/webhooks/scheduled) running unattended. Organization-level policies (/etc/scout/requirements.json) override user config." },
    "暂无渠道目标，点击右上角\"+\"添加": { zh: "暂无渠道目标，点击右上角\"+\"添加", en: "No channel targets yet. Click \"+\" in the top-right to add" },
    "暂无监听目录": { zh: "暂无监听目录", en: "No watched directories" },
    "任务模板:": { zh: "任务模板:", en: "Task template:" },
    "只监听新增": { zh: "只监听新增", en: "New files only" },
    "只监听 PDF 文件": { zh: "只监听 PDF 文件", en: "PDF files only" },
    "暂无插件": { zh: "暂无插件", en: "No plugins" },
    "无死信记录": { zh: "无死信记录", en: "No dead-letter records" },
    "暂无推送记录": { zh: "暂无推送记录", en: "No push records yet" },
    "+ 添加": { zh: "+ 添加", en: "+ Add" },
    "← 返回插件管理": { zh: "← 返回插件管理", en: "← Back to Plugins" },
    "载荷": { zh: "载荷", en: "Payload" },
    "分叉会话": { zh: "分叉会话", en: "Branch session" },
    "有新版本可用": { zh: "有新版本可用", en: "A new version is available" },
    "运行时间: ": { zh: "运行时间: ", en: "Uptime: " },
    "检测到文件变化:": { zh: "检测到文件变化:", en: "File change detected:" },
    "请根据需求处理该文件。": { zh: "请根据需求处理该文件。", en: "Process the file based on the request." },
    "可选过滤器（事件类型 / 路径 / 大小）": { zh: "可选过滤器（事件类型 / 路径 / 大小）", en: "Optional filters (event type / path / size)" },
    "每个事件含 path / relative / size / mtime 字段。": { zh: "每个事件含 path / relative / size / mtime 字段。", en: "Each event contains path / relative / size / mtime fields." },
    "可选过滤器（event_filters）:": { zh: "可选过滤器（event_filters）:", en: "Optional filters (event_filters):" },
    "如何实现\"文件变化 → Agent 自动处理\"": { zh: "如何实现\"文件变化 → Agent 自动处理\"", en: "How to automate \"file change → Agent processing\"" },

};

// 当前语言: zh / en（存 localStorage）
let UI_LANG = localStorage.getItem('scout_ui_lang') || 'zh';

const I18N = {
    t(key) {
        const entry = I18N_DICT[key];
        if (!entry) return key;
        return entry[UI_LANG] || key;
    },
    // 切换语言
    toggle() {
        UI_LANG = UI_LANG === 'zh' ? 'en' : 'zh';
        localStorage.setItem('scout_ui_lang', UI_LANG);
        this.apply();
        this.updateToggleBtn();
        // 通知页面重新渲染动态内容（如模型选项）
        document.dispatchEvent(new CustomEvent('uilangchange', { detail: { lang: UI_LANG } }));
    },
    set(lang) {
        UI_LANG = lang === 'en' ? 'en' : 'zh';
        localStorage.setItem('scout_ui_lang', UI_LANG);
        this.apply();
        this.updateToggleBtn();
        document.dispatchEvent(new CustomEvent('uilangchange', { detail: { lang: UI_LANG } }));
    },
    // 应用翻译到所有 data-i18n 元素
    apply() {
        // 1. 处理显式标记的 data-i18n 元素
        document.querySelectorAll('[data-i18n]').forEach(el => {
            const key = el.getAttribute('data-i18n');
            const val = this.t(key);
            const textNodes = [];
            el.childNodes.forEach(node => {
                if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                    textNodes.push(node);
                }
            });
            if (textNodes.length > 0) {
                textNodes[0].textContent = val;
                for (let i = 1; i < textNodes.length; i++) textNodes[i].textContent = '';
            } else if (el.children.length === 0) {
                el.textContent = val;
            }
        });
        // 2. 处理 placeholder
        document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
            el.placeholder = this.t(el.getAttribute('data-i18n-placeholder'));
        });
        // 3. 处理 title
        document.querySelectorAll('[data-i18n-title]').forEach(el => {
            el.title = this.t(el.getAttribute('data-i18n-title'));
        });
        // 4. 自动扫描：翻译所有含中文的文本节点（双向：中文时恢复，英文时翻译）
        this._translateTree(document.body);
        document.documentElement.lang = UI_LANG === 'zh' ? 'zh-CN' : 'en';
    },

    _translateTree(root) {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        const nodes = [];
        while (walker.nextNode()) {
            const node = walker.currentNode;
            const txt = node.textContent;
            if (txt && /[\u4e00-\u9fff]/.test(txt)) nodes.push(node);
        }
        nodes.forEach(node => {
            const txt = node.textContent.trim();
            if (!txt) return;
            if (UI_LANG === 'en') {
                // 记录原始中文（若已翻译过则跳过）
                if (!node.parentElement.getAttribute('data-i18n-orig')) {
                    // 只处理未被 data-i18n 显式标记的元素
                    if (node.parentElement && !node.parentElement.hasAttribute('data-i18n')) {
                        node.parentElement.setAttribute('data-i18n-orig', txt);
                    }
                }
            }
            if (I18N_DICT[txt]) {
                node.textContent = UI_LANG === 'en' ? I18N_DICT[txt].en : txt;
                return;
            }
            // 中文模式：若父元素有 data-i18n-orig，恢复原始
            if (UI_LANG === 'zh') {
                const parent = node.parentElement;
                if (parent && parent.getAttribute('data-i18n-orig')) {
                    // 由下方恢复逻辑统一处理
                }
            }
        });
        // 恢复：中文模式下，将 data-i18n-orig 元素恢复为中文
        if (UI_LANG === 'zh') {
            document.querySelectorAll('[data-i18n-orig]').forEach(el => {
                const orig = el.getAttribute('data-i18n-orig');
                // 只替换直接文本节点中的中文
                el.childNodes.forEach(node => {
                    if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                        node.textContent = orig;
                    }
                });
                el.removeAttribute('data-i18n-orig');
            });
        }
    },
    updateToggleBtn() {
        const btn = document.getElementById('lang-toggle-btn');
        if (btn) btn.textContent = UI_LANG === 'zh' ? '中 / EN' : 'EN / 中文';
    }
};

function toggleUILang() { I18N.toggle(); }

// ===== 页面自动接入 =====
// 任一页面引入本脚本后：DOMContentLoaded 自动应用翻译，并通过
// MutationObserver 监听动态渲染的中文文本节点自动翻译。
let _i18nObserver = null;
let _i18nDebounceTimer = null;

function _i18nApplyDebounced() {
    if (_i18nDebounceTimer) clearTimeout(_i18nDebounceTimer);
    _i18nDebounceTimer = setTimeout(() => {
        _i18nDebounceTimer = null;
        I18N.apply();
    }, 60);
}

function I18N_autoInit() {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', I18N_autoInit);
        return;
    }
    I18N.apply();
    I18N.updateToggleBtn();
    if (_i18nObserver) return;
    _i18nObserver = new MutationObserver(mutations => {
        let hasZh = false;
        for (const m of mutations) {
            for (const n of m.addedNodes) {
                if (n.nodeType === Node.TEXT_NODE) {
                    if (n.textContent && /[\u4e00-\u9fff]/.test(n.textContent)) { hasZh = true; break; }
                } else if (n.nodeType === Node.ELEMENT_NODE) {
                    if (n.textContent && /[\u4e00-\u9fff]/.test(n.textContent)) { hasZh = true; break; }
                }
            }
            if (hasZh) break;
        }
        if (hasZh) _i18nApplyDebounced();
    });
    _i18nObserver.observe(document.body, { childList: true, subtree: true });
}

// 生成语言切换按钮（固定右上角），页面只需调用 renderLangToggle()
function renderLangToggle() {
    if (document.getElementById('lang-toggle-btn')) return;
    const btn = document.createElement('button');
    btn.id = 'lang-toggle-btn';
    btn.textContent = UI_LANG === 'zh' ? '中 / EN' : 'EN / 中文';
    btn.onclick = toggleUILang;
    btn.title = '切换界面语言 / Toggle UI language';
    Object.assign(btn.style, {
        position: 'fixed', top: '12px', right: '12px', zIndex: '9999',
        padding: '5px 12px', borderRadius: '8px', cursor: 'pointer',
        background: 'rgba(255,255,255,0.08)', color: 'inherit',
        border: '1px solid rgba(255,255,255,0.15)',
        fontSize: '12px', fontFamily: 'inherit'
    });
    document.body.appendChild(btn);
}

I18N_autoInit();

// 模型名中文括注 → 英文注释（用于动态模型名翻译）
const MODEL_TAG_MAP = {
    "推荐": "recommended", "最新": "latest", "旗舰": "flagship", "预览": "preview",
    "快速": "fast", "最强": "strongest", "最强·最新": "strongest·latest", "推荐·最新": "recommended·latest",
    "免费": "free", "经济": "economical", "高性价比": "cost-effective", "轻量": "lightweight",
    "多模态": "multimodal", "推理": "reasoning", "推理增强": "reasoning+", "推理实验": "experimental",
    "推理专用": "reasoning", "深度推理": "deep reasoning", "深度推理·最强": "deep reasoning·strongest",
    "超长上下文": "long context", "超长文本": "long context", "长文本": "long context",
    "视觉": "vision", "视觉理解": "vision", "视觉理解·最新": "vision·latest",
    "代码专用": "coding", "增强版": "enhanced", "增强": "enhanced", "扩展": "extended",
    "开源": "open-source", "开源旗舰": "open-source flagship", "开源旗舰·推理": "open-source flagship·reasoning",
    "百炼": "Bailian", "百炼·快速": "Bailian·fast", "百炼·最新": "Bailian·latest",
    "经济": "economical", "最轻量": "lightest", "多语言": "multilingual",
    "1536维": "1536-dim", "1024维": "1024-dim", "2048维": "2048-dim", "768维": "768-dim", "3072维": "3072-dim",
    "Token Plan": "Token Plan", "聚合": "aggregator", "经典": "classic",
    "深度": "deep", "满血": "full", "蒸馏": "distilled", "最新·多语言": "latest·multilingual",
    "最新·高质量": "latest·high quality", "最新视觉": "latest vision", "快速·免费": "fast·free",
    "快速·最新": "fast·latest", "快速·推理·最新": "fast·reasoning·latest", "推理·快速": "reasoning·fast",
    "最新·多模态": "latest·multimodal", "旗舰·最新": "flagship·latest", "旗舰·最新·推理": "flagship·latest·reasoning",
    "免费": "free", "超长": "long", "最新": "latest", "32K": "32K", "128K": "128K", "256K": "256K",
};

// 翻译模型名：处理 "通义千问 Plus (高性价比)" / "Qwen3.8 Max Preview (预览·Token Plan)" 等
function __translateModelName(name) {
    if (!name || !/[\u4e00-\u9fff]/.test(name)) return name;
    // 1. 整段已在词典 → 直接用
    if (I18N_DICT[name]) return I18N_DICT[name].en;
    // 2. 解析 "英文部分 (中文括注)"
    const m = name.match(/^([^(（]*)[(（]([^)）]*)[)）]$/);
    if (m) {
        const base = m[1].trim();
        const tags = m[2].trim();
        // 翻译 base 中的中文部分（如"通义千问"、"豆包"、"火山引擎"）
        const baseEn = __translateBase(base);
        // 翻译括注
        const tagEn = tags.split(/[·|]/).map(t => MODEL_TAG_MAP[t.trim()] || t.trim()).join(' · ');
        return `${baseEn} (${tagEn})`;
    }
    // 3. 无括注，仅翻译中文单词
    return __translateBase(name);
}

function __translateBase(base) {
    const baseMap = {
        "通义千问": "Tongyi Qwen", "通义万相": "Tongyi Wanxiang", "豆包": "Doubao",
        "火山引擎": "Volcano", "智谱": "Zhipu", "小米": "Xiaomi", "阿里云 DashScope": "Alibaba DashScope",
        "阿里云": "Alibaba",
    };
    for (const [zh, en] of Object.entries(baseMap)) {
        if (base.startsWith(zh)) return en + base.slice(zh.length);
    }
    return base;
}

// 全局翻译函数：整段匹配 + 前缀匹配（用于 toast/alert 等动态文本）
function __t(text) {
    if (typeof text !== 'string' || !text) return text;
    if (UI_LANG === 'zh') return text; // 中文模式原样返回
    // 整段匹配
    if (I18N_DICT[text]) return I18N_DICT[text].en;
    // 模型名翻译
    if (/[\u4e00-\u9fff]/.test(text) && (/(预览|最新|旗舰|推荐|多模态|推理|快速|免费|高性价比|经济|百炼|开源|视觉|增强|超长|通用|深度|轻量|最轻量|最强)/.test(text))) {
        const translated = __translateModelName(text);
        if (translated !== text) return translated;
    }
    // 前缀匹配：找到第一个 ':' 或 ':' 前的中文部分
    const m = text.match(/^([\u4e00-\u9fff（）()，。、\s]+)[::：]/);
    if (m && I18N_DICT[m[1].trim()]) {
        return I18N_DICT[m[1].trim()].en + text.slice(m[0].length - 1);
    }
    return text;
}

// 模板字符串翻译：匹配含 ${} 或 中文前缀(...)后缀 的模板
function __tpl(text) {
    if (typeof text !== 'string' || !text) return text;
    if (UI_LANG === 'zh') return text;
    // 1. 含 ${} 的模板
    if (text.includes('${')) {
        const parts = text.split(/(\$\{[^}]+\})/);
        return parts.map(part => {
            if (/^\$\{/.test(part)) return part;
            return __t(part);
        }).join('');
    }
    // 2. 中文前缀(...)中文后缀模式：如 "✅ 已配置 (abc) — 本地无记忆..."
    //    匹配 "前缀 (变量) 后缀中文"
    const kv = text.match(/^([\u4e00-\u9fff✅⚠️——，。、：\s]+?)\(([^()]*)\)([\u4e00-\u9fff✅⚠️——，。、：\s]+)$/);
    if (kv) {
        const prefix = __t(kv[1]);
        const suffix = __t(kv[3]);
        // 若前缀或后缀翻译成功，重组
        if (prefix !== kv[1] || suffix !== kv[3]) {
            return `${prefix}(${kv[2]})${suffix}`;
        }
    }
    // 3. 整段匹配
    return __t(text);
}