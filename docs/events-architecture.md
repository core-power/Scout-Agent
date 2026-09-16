# 事件与调度体系架构边界

> 2026-09-14 建档。此前 bus / automation / scheduler / notify 四个模块的职责边界
> 只存在于代码注释里，新需求落点靠猜——本文档是"谁发布、谁订阅、谁投递"的
> 单一事实来源。

## 一、职责矩阵

| 模块 | 角色定位 | 职责 | 不做什么 |
|------|---------|------|---------|
| `bus/`（hub.py / nats_bus.py） | **传输层** | 进程内/跨进程事件的发布-订阅枢纽；事件定义、路由、异步分发、DLQ | 不含任何业务逻辑，不知道事件"意味着什么" |
| `automation/triggers.py` | **事件触发器** | 订阅事件 → 匹配触发规则 → 派生任务（对标 Goal Intake） | 不直接执行任务（交给 runner） |
| `automation/watcher.py` | **文件事件源** | 监听文件系统变化 → 发射事件到 bus | 不响应事件（只产生事件） |
| `automation/cron.py` | **时间触发器** | Cron 表达式驱动的定时任务定义/调度（一等公民 Cron 设计） | 不做事件响应（那是 triggers 的职责） |
| `automation/runner.py` | **执行入口** | 无人值守任务的统一执行（触发器与 cron 的下游） | 不定义何时触发（那是上游的职责） |
| `scheduler/cron_manager.py` | **遗留 Cron 管理** | 周期任务的管理 API 支撑 | ⚠️ 与 automation/cron 职责重叠（见下） |
| `notify/dispatcher.py` | **投递端** | 订阅 `notification` 类事件 → 按用户偏好跨渠道推送（飞书/微信/邮件…） | 不产生业务事件，是纯消费端 |

## 二、事件流向图

```
        产生事件                          消费事件
  ┌─────────────────┐            ┌──────────────────────┐
  │ engine (agent)   │──emit────▶│                      │──▶ automation/triggers
  │ 工具执行/会话状态 │            │      bus (hub)       │      （事件→派生任务→runner）
  ├─────────────────┤            │   发布-订阅枢纽       │──▶ notify/dispatcher
  │ automation/watcher│──emit───▶│   （NATS 可选）       │      （notification→渠道推送）
  │ 文件系统变化      │            │                      │──▶ 其他订阅者（插件等）
  ├─────────────────┤            └──────────────────────┘
  │ automation/cron   │──到点──────────────▶ runner（直接执行，不经 bus）
  │ scheduler(遗留)   │──到点──────────────▶ 同上
  └─────────────────┘
```

关键约定：**cron/时间触发直接调 runner，不绕 bus**（时间不是"事件"，bus 只承载
真实业务事件）；**文件/工具/会话事件一律走 bus**（可多订阅者复用）。

## 三、新需求落点决策树

```
需求是什么？
├─ "某事件发生时做 X"        → automation/triggers（订阅 bus 事件 + 规则匹配）
├─ "每天/每周期定时做 X"     → automation/cron（一等公民 Cron）
├─ "文件变化时做 X"          → automation/watcher（配置监听目录）
├─ "把结果推送/通知给用户"    → notify/dispatcher（订阅 notification 事件）
├─ "无人值守执行一个任务"     → automation/runner
└─ "新增一种进程内事件"       → bus（只加事件名与载荷约定，业务放 emit 方）
```

## 四、已知重叠与处置计划

| 重叠 | 现状 | 处置 |
|------|------|------|
| `automation/cron.py`（182 行）vs `scheduler/cron_manager.py`（67 行） | 两套 Cron 管理，API 各挂各的（`/api/cron` 与 scheduler 相关路由） | **收敛方向**：scheduler/cron_manager 为遗留实现，冻结不新增；新 Cron 需求一律落 automation/cron；后续版本将 scheduler 的存量任务迁移后移除该模块（迁移脚本待做） |

## 五、扩展约定

- 新事件命名：`<domain>.<subject>.<verb>`（如 `tool.complete`、`conversation.start`）
- 事件载荷必须可序列化（JSON-safe），大对象传路径引用不传内容
- 订阅者异常不得影响发布方（hub 已隔离）——新订阅者不要在回调里抛出
