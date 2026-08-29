# Scout Agent vs DeepSeek Harness（DSH）差距评估

> 评估日期：2026-08-27。方法：对比 DSH（DeepSeek Agent Harness）公开能力
> （模型接入文件系统/终端/网页/代码工具/多 Agent，组织上下文与工具调用）
> 与 DSBench（Agent 评测基准，含 Pass@1 度量），逐项对照 scout-agent 实际实现。
>
> 结论：13 项评估 = 10 项差距（6 结构性 + 4 增强型）+ 3 项已接近/反超。
> 全部 10 项差距已于 2026-08-27 清零（见 `docs/architecture.md`）。

---

## 一、结构性差距（6 项，已全部补齐 ✅）

| # | 能力 | DSH 参照 | scout 落地 | 状态 |
|---|------|---------|-----------|------|
| S1 | 测试反馈闭环 | DSBench「测试失败→提取堆栈→修复→重测」 | `engine/test_feedback.py` + `heal_loop.py`（`SCOUT_TEST_FEEDBACK` 默认开） | ✅ 已补齐 |
| S2 | 可插拔 Agent Loop | 不同任务不同执行策略 | `engine/loops.py`：`ReActLoop`（默认）/ `DAGLoop`（计划-执行），`SCOUT_LOOP_MODE` 切换 | ✅ 已补齐 |
| S3 | 工具契约 | 结构化工具调用与参数约束 | `tools/base.py`：注解推导 JSON Schema、`validate_args` 运行时校验、统一 `error_code` | ✅ 已补齐 |
| S4 | 持久 Shell 会话 | 终端 / Bash 调度引擎 | `tools/builtin/shell/session.py`：长驻 bash、cwd/env/后台任务跨调用保留 | ✅ 已补齐（PTY 未做 → E2） |
| S5 | 沙箱隔离 | 任务隔离环境 | `security/sandbox.py`：off/non-main/all 模式、Docker 探测 + `require_docker` 硬失败 | ✅ 已补齐 |
| S6 | 插件 SPI | Cordis「一切皆插件」 | `plugins/spi.py`：`provides`/`provide(kind)` 声明式替换 LLM/存储 | ✅ 已补齐（类型未接全 → E3） |

## 二、增强型差距（4 项）

| # | 能力 | DSH 参照 | scout 现状 | 状态 |
|---|------|---------|-----------|------|
| E1 | **eval 基准 / Pass@1** | DSBench 可复现评测：多轮采样→Pass@1→报告 | `scout/eval/`：隔离工作区 + 验证器 + Pass@k 无偏估计 + CLI | ✅ 已补齐 |
| E2 | **PTY 交互式终端** | 终端完整支持（交互式程序） | `shell/pty_session.py`：伪终端 + 按键注入 + 显式中断 + resize | ✅ 已补齐 |
| E3 | **SPI 全类型落地** | 一切皆插件（会话/缓存/记忆…） | `get_cache_backend`/`get_session_store`/`get_memory_store` 全部接入 `spi` | ✅ 已补齐 |
| E4 | **上下文与记忆工程化** | DSH「组织上下文」 | `context/memory_extract.py`（`SessionMemoryExtractor`：启发式/LLM 结构化抽取 + 去重 + 会话标记 + 批量补抽取）+ `context/context_assembler.py`（`ContextAssembler`：跨会话记忆 × 重要性 × 时间衰减排序、预算截断、历史会话摘要），已接入 `Agent`（会话结束自动抽取、`<summary>` 注入） | ✅ 已补齐 |

## 三、已接近 / 反超（3 项 ✅）

| # | 能力 | 说明 |
|---|------|------|
| C1 | 多 Agent 协作 | `a2a/`（A2A 协议 client/server）+ `multiagent/`（coordinator/router/workflow/messenger/shared_state/runtime），比 DSH 早期更完整 |
| C2 | 自动化与调度 | `bus/`（事件总线）+ `automation/` + `scheduler/`，支持定时/触发式任务 |
| C3 | 平台接入面 | `voice/`（语音）、`web/`（Web 界面 + 中间件）、`adapters/`（微信等平台）、`gateway/`、`notify/`，接入面比 DSH 更广 |

## 四、演进路线图

```
已完成（2026-08-27，10/10 差距清零）          远期
┌────────────────────────────────────────┐   ┌──────────────────┐
│ S1 测试反馈闭环  E1 eval/Pass@1        │   │ C1-C3 反超项      │
│ S2 可插拔 Loop   E2 PTY 终端           │──▶│   持续打磨        │
│ S3 工具契约      E3 SPI 全类型落地     │   └──────────────────┘
│ S4 持久 Shell    E4 上下文/记忆工程化  │
│ S5 沙箱强化                            │
│ S6 插件 SPI                            │
└────────────────────────────────────────┘
```

**下一优先**：基于 E4 的抽取/组装基建，持续打磨反超项（C1-C3）——
多 Agent 协作的跨会话记忆共享、eval 基准扩充离线任务集。
