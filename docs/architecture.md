# Scout Agent 架构补强说明（2026-08-27）

> 对标 DeepSeek Harness 后补齐的能力：测试反馈闭环、可插拔 Agent 循环、
> 工具契约、持久 Shell 会话、插件 SPI（§1-5）；
> 增强型补齐：eval 基准 / Pass@1、PTY 交互式终端、SPI 全类型落地（§6-8）。
> 差距评估与路线图见 `docs/dsh-comparison.md`。

---

## 1. 测试反馈闭环

- 模块：`scout/engine/test_feedback.py`
- 流程：`execute_code` 失败 → 自动探测项目测试目录（tests/ 或 test_*.py）→ 运行
  `pytest --tb=short -q`（超时 90s，失败解析上限 8 条）→ 提取结构化失败
  （文件/行号/测试名/断言信息/精简堆栈）→ 追加到 `SelfHealLoop.generate_fix`
  的修复 prompt，让 healer 依据真实失败原因自纠错。
- 开关：环境变量 `SCOUT_TEST_FEEDBACK=0` 关闭（默认开启）。
- 对比 DSH：DSBench 的「测试失败 → 提取错误堆栈 → 修复 → 重测」闭环思路；
  本实现以 pytest 为基准，Pass@1 度量见 §6（eval 基准）。

## 2. 可插拔 Agent 循环

- 模块：`scout/engine/loops.py`
- `AgentLoop` 抽象接口：`run(user_message, session, attachments) -> {response, session, steps}`。
- `ReActLoop`（默认）：委托 `Agent._run_react`（原内联循环，行为不变）。
- `DAGLoop`（`agent_mode="dag"` 或 `SCOUT_LOOP_MODE=dag`）：
  1. `DAGPlanner.plan()` LLM 拆解目标为带 depends_on 的步骤；
  2. `_topo_order()` 拓扑排序（检测环回退）；
  3. 逐步骤用独立子会话跑 `_run_react`，依赖结果注入下一步上下文；
  4. `_synthesize()` LLM 汇总为最终答复。
- `Agent.run_conversation` 改为纯分发入口；`_run_react` 是原循环体（改名）。
- 每步可用 `step_max_turns` 收紧轮次预算（默认复用 `agent.max_turns`）。

## 3. 工具契约

- 模块：`scout/tools/base.py`
- 统一错误码（`Observation.error_code`）：
  `UNKNOWN_TOOL` / `INVALID_ARGS` / `INTERNAL`（注册表兜底），
  工具可细化 `NOT_FOUND` / `PERMISSION` / `TIMEOUT` / `NETWORK` / `SANDBOX` / `UNAUTHORIZED`。
- `ensure_schema()`：手写 `parameters` 优先；为空时从 `execute()` 签名推导
  （`typing.get_type_hints` 解析延迟注解，Optional/Union/List 兼容，按类缓存）。
- `validate_args()`：注册表执行前统一校验——必填缺失 → `INVALID_ARGS`；
  数字字符串纠正为数值、单值包装为数组，宽松策略避免误伤。
- 注册表 `ToolRegistry.execute`：未知工具 → `UNKNOWN_TOOL`；校验失败 →
  `INVALID_ARGS`；异常 → `INTERNAL`。

## 4. 持久 Shell 会话

- 模块：`scout/tools/builtin/shell/session.py`
- 一个会话 = 一个常驻 bash 进程（`--norc --noprofile` 非交互），stdin 注入命令，
  哨兵行 `__SCOUT_SESSION_END__` 分帧输出与退出码。
- 跨调用保留：cwd / 导出变量 / 后台任务（`nohup ... &`）。
- 超时 → 杀会话并自动重建；会话退出 → 下次调用自动拉起；按 `session_key` 隔离
  （agent 主循环自动传 `session.id`），per-session 锁串行化。
- 用法：shell 工具加 `persistent=true` 参数；`command="__session_reset__"` 手动重置。
- 对比 DSH：持久化终端（Bash 调度引擎）思路；交互式程序支持见 §7（PTY 终端）。

## 5. 插件 SPI

- 模块：`scout/plugins/spi.py`
- 插件可声明 `provides = ["llm", "storage"]` 并实现同步 `provide(kind)`，
  `PluginManager.load_plugin` 自动注册进 `SPIRegistry`（卸载时自动注销）。
- 应用侧接入（全类型，2026-08-27）：
  - `LLMClientFactory.create(provider="spi")` → 插件 LLM 实现（`impl(**kwargs)`）。
  - `get_storage_backend(backend="spi")` → 插件存储实现。
  - `get_cache_backend(backend="spi")` → 插件缓存实现（`SCOUT_CACHE_BACKEND=spi`）。
  - `get_session_store(backend="spi")` → 插件会话实现（`SCOUT_SESSION_STORE=spi`）。
  - `get_memory_store(backend="spi")` → 插件记忆实现（`SCOUT_MEMORY_STORE=spi`）。
- SPI 分支每次动态解析（插件可装卸，不受全局单例短路）。
- 未注册时使用对应 SPI 类型会得到明确错误（提示加载对应插件），而非静默回退。
- 对比 DSH：Cordis「一切皆插件」哲学的地基；五类核心组件（llm/storage/cache/
  session/memory）全部可声明式替换，循环策略独立为 `AgentLoop`（见 §2）。

## 6. eval 基准 / Pass@1（对标 DSBench）

- 模块：`scout/eval/`（`runner.py` / `tasks.py` / `metrics.py` / `__main__.py`）
- 流程：任务 × 每次采样 → 隔离临时工作区（TemporaryDirectory）→ 写入 setup_files →
  构造 Agent（默认 ReAct，可切 DAG 对比）→ `run_conversation` → 验证器
  （`command` 型跑 pytest/shell 检查返回码，`file` 型断言结果文件）→ 汇总报告。
- 指标：`pass_at_k(n, c, k)` 无偏估计（Codex 公式），内置任务集 5 个
  （修复 / 统计 / shell 整理 / 计算 / 多步重构），支持 JSON 任务目录扩展。
- 隔离性：关闭记忆/持久化/人工确认，验证器只读工作区、不信任 Agent 输出文本。
- 用法：
  ```bash
  python -m scout.eval --samples 3 --loop-mode react   # Pass@1/3/5
  python -m scout.eval --loop-mode dag --task fix_factorial
  python -m scout.eval --tasks-dir ./eval --output report.json
  ```
- 价值：可复现地验证「修复 / 规划」能力提升（如 react vs dag 循环策略对比）。

## 7. PTY 交互式终端

- 模块：`scout/tools/builtin/shell/pty_session.py`
- 伪终端（pty）：进程 stdin/stdout/stderr 接 PTY，vim/top/less 等交互程序可用。
- `start_new_session=True`：bash 成为会话 leader 并把 PTY 作为控制终端，
  任务控制/前台进程组正常（Ctrl-C 可路由到前台作业）。
- 关闭回显与提示符（`stty -echo; PS1=''`）：命令不再回显，哨兵分帧可靠；
  TERM=xterm-256color 保证全屏程序正确渲染；`TIOCSWINSZ` 动态调整窗口尺寸。
- 交互语义：`run()` 超时**不自动中断**（会话保留）→ 上层可
  `send_keys(":wq\r")` 注入按键继续，或 `interrupt()`/`session_keys="\x03"` 显式中断。
- shell 工具参数：`interactive=true` 走 PTY 会话，`session_keys` 注入按键；
  `__session_reset__` 同时重置管道与 PTY 会话。
- 单行拼接命令（分号合成）：交互 bash 会预读多行，`read`/`cat` 会吞掉后续行。

## 8. 上下文与记忆工程化（E4）

对标 DSH「组织上下文」的**跨会话**维度——单会话内压缩/剪枝由
`context/manager.py` 负责，跨会话由本套能力闭环：

### 8.1 跨会话关键记忆抽取（`context/memory_extract.py`）

- `SessionMemoryExtractor.extract(session)`：会话结束后把重要信息沉淀为
  结构化长期记忆写入 `MemoryStore`，带类别（`preference`/`decision`/
  `conclusion`/`skill`/`fact`）与重要性标注；
- **LLM 结构化抽取**（注入 `LLMClient` 时）：JSON 输出、`min_importance`
  过滤、容错（非法输出自动降级）；
- **启发式降级**：无 LLM 时按偏好/决策/结论/技能信号词 + 长度过滤抽取，
  剥离 `<runtime_context>` 等注入块；
- **去重**：与库内已有记忆做 Jaccard 相似度对比（默认阈值 0.85），
  超过即跳过，避免记忆库被重复信息污染；
- **防重复抽取**：`mark_extracted`/`is_extracted` 在会话 `extra` 打标；
  `extract_pending_sessions` 批量补抽取历史已完成会话。

### 8.2 跨会话上下文组装（`context/context_assembler.py`）

- `ContextAssembler.assemble(query, exclude_session_id)` → `(memory_text, summary_text)`；
- **记忆块**：召回候选按 `decay_score`（重要性 × 时间衰减）排序，
  `memory_limit` 截断 + 字符预算截断；
- **历史摘要块**：最近已完成会话的标题/压缩摘要（`<summary>` 注入）。

### 8.3 Agent 接入（可选注入，默认零侵入）

- 构造参数 `memory_extractor=` / `context_assembler=` / `memory_flush=`；
- `run_conversation` / `stream_conversation` 收尾自动 `_maybe_extract_session_memory`
  （失败仅告警，不阻塞主流程）；
- `_inject_context` 优先走组装器：跨会话记忆召回替换单会话检索，
  并新增 `<summary>` 注入；
- **压缩前 flush**：注入 `memory_extractor` 时自动包装 `MemoryFlush` 接入
  `ContextManager.compress(memory_flush=...)` —— 压缩替换旧消息段前先抽取
  关键记忆，防止摘要丢失跨会话信息（`MemoryFlush` 2026-08-27 重构，
  此前仅导出未接入）。

## 9. 注：SPI 扩展测试覆盖

- `tests/unit/test_contracts_loops_spi.py`：cache/session/memory 工厂 + 未注册报错。
- `tests/unit/test_eval.py`：Pass@k 数学、任务加载、Runner 全流程（隔离/超时/报告）。
- `tests/unit/test_pty_session.py`：基本执行/退出码/cwd 保留/挂起→按键恢复/
  显式中断/resize/工具级接入。
- `tests/unit/test_context_engineering.py`（19 项）：抽取器（启发式/LLM 与降级/
  去重/批量补抽取）、组装器（记忆块/预算/排序/摘要）、Agent 接入（闭环抽取/
  失败容错）。
