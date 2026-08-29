# 多 Agent 协作系统

Scout Agent 支持多个 Agent 协作完成复杂任务。

## 核心组件

### 1. 协作协调器 (MultiAgentCoordinator)

负责任务分解、分配和结果聚合。

**特性：**
- 自动任务分解（基于 LLM）
- 智能任务分配（根据 Agent 专长）
- 结果聚合（合并多个子任务结果）
- 依赖管理（支持 DAG 执行）

**使用示例：**
```python
from scout.multiagent import MultiAgentCoordinator
from scout.multiagent import AgentRouter

# 创建路由器并注册 Agent
router = AgentRouter()
router.register_agent("researcher", researcher_agent)
router.register_agent("writer", writer_agent)
router.register_agent("reviewer", reviewer_agent)

# 创建协调器
coordinator = MultiAgentCoordinator(
    router=router,
    llm=llm_client,
    decompose_strategy="llm",  # 使用 LLM 分解任务
    aggregate_strategy="llm"   # 使用 LLM 聚合结果
)

# 协调执行复杂任务
result = await coordinator.coordinate(
    task="撰写一篇关于 AI 发展趋势的技术报告",
    context={"length": "5000字", "audience": "技术专家"}
)

print(result["final_result"])
print(f"执行时间: {result['execution_time']:.2f}秒")
```

### 2. 工作流引擎 (WorkflowExecutor)

支持串行、并行和 DAG 任务编排。

**特性：**
- 顺序执行（Sequential）
- 并行执行（Parallel）
- 扇出-扇入（Fan-out/Fan-in）
- DAG 依赖解析
- 自动重试和错误处理

**使用示例：**
```python
from scout.multiagent import WorkflowExecutor, WorkflowBuilder

executor = WorkflowExecutor(router=router, llm=llm_client)

# 方式 1: 顺序工作流
workflow = WorkflowBuilder.sequential(
    name="数据分析流程",
    tasks=[
        {"name": "收集数据", "description": "从多个源收集数据", "agent_id": "collector"},
        {"name": "清洗数据", "description": "清洗和标准化数据", "agent_id": "cleaner"},
        {"name": "分析数据", "description": "执行统计分析", "agent_id": "analyst"},
    ]
)

# 方式 2: 并行工作流
workflow = WorkflowBuilder.parallel(
    name="并行搜索",
    tasks=[
        {"name": "搜索 Google", "description": "搜索 Google", "agent_id": "searcher1"},
        {"name": "搜索 Bing", "description": "搜索 Bing", "agent_id": "searcher2"},
        {"name": "搜索 DuckDuckGo", "description": "搜索 DuckDuckGo", "agent_id": "searcher3"},
    ]
)

# 方式 3: 扇出-扇入
workflow = WorkflowBuilder.fan_out_fan_in(
    name="代码审查",
    parallel_tasks=[
        {"name": "审查前端", "description": "审查前端代码", "agent_id": "frontend_reviewer"},
        {"name": "审查后端", "description": "审查后端代码", "agent_id": "backend_reviewer"},
        {"name": "审查测试", "description": "审查测试代码", "agent_id": "test_reviewer"},
    ],
    final_task={"name": "汇总审查", "description": "汇总所有审查意见", "agent_id": "lead_reviewer"}
)

# 执行工作流
result = await executor.execute(workflow)
print(f"状态: {result.status}")
print(f"节点数: {len(result.nodes)}")
```

### 3. 共享状态管理 (SharedStateManager)

支持 Agent 间的数据共享和同步。

**特性：**
- 线程安全的读写操作
- 原子更新（update）
- 比较并交换（CAS）
- 订阅机制（watch）
- 持久化存储
- Agent 工作空间隔离

**使用示例：**
```python
from scout.multiagent import SharedStateManager, AgentWorkspace

# 创建共享状态管理器
shared_state = SharedStateManager(persistence_path="shared.json")

# 方式 1: 直接操作共享状态
await shared_state.set("config", {"timeout": 30})
config = await shared_state.get("config")

# 原子更新
new_version, new_value = await shared_state.update(
    "counter",
    lambda x: (x or 0) + 1
)

# CAS 操作（乐观锁）
success = await shared_state.compare_and_swap(
    "balance",
    expected_version=1,
    new_value=100
)

# 订阅变更
def on_change(key, old_value, new_value):
    print(f"{key}: {old_value} -> {new_value}")

shared_state.subscribe("config", on_change)

# 方式 2: 使用 Agent 工作空间
workspace1 = AgentWorkspace("agent1", shared_state)
workspace2 = AgentWorkspace("agent2", shared_state)

# Agent1 写入私有数据
workspace1.local["private_data"] = "secret"

# Agent1 写入共享数据
await workspace1.share("shared_result", {"score": 95})

# Agent2 读取共享数据
result = await workspace2.read("shared_result")
print(result)  # {"score": 95}

# Agent2 无法访问 Agent1 的私有数据
# workspace2.local["private_data"]  # 不存在
```

### 4. 协作模式

支持多种协作策略：

#### 共识模式 (Consensus)
多个 Agent 讨论直到达成一致。

```python
result = await coordinator.collaborative_solve(
    problem="是否应该采用微服务架构？",
    strategy="consensus",
    max_rounds=3
)
```

#### 辩论模式 (Debate)
正方和反方进行辩论。

```python
result = await coordinator.collaborative_solve(
    problem="Python vs Rust 哪个更适合系统编程？",
    strategy="debate",
    pro_agent="python_advocate",
    con_agent="rust_advocate"
)
```

#### 投票模式 (Voting)
多个 Agent 投票选出最佳方案。

```python
result = await coordinator.collaborative_solve(
    problem="选择最佳数据库方案",
    strategy="voting",
    options=["PostgreSQL", "MongoDB", "Redis"]
)
```

## 实际应用示例

### 示例 1: 研究报告生成

```python
# 注册专业 Agent
router.register_agent("researcher", researcher_agent)
router.register_agent("analyst", analyst_agent)
router.register_agent("writer", writer_agent)
router.register_agent("editor", editor_agent)

# 创建协调器
coordinator = MultiAgentCoordinator(router=router, llm=llm_client)

# 执行复杂研究任务
result = await coordinator.coordinate(
    task="撰写一份关于 2024 年 AI 发展趋势的深度报告",
    context={
        "length": "10000字",
        "audience": "企业决策者",
        "focus_areas": ["LLM", "多模态", "Agent"]
    }
)

# 结果包含：
# - 分解后的子任务
# - 每个任务的执行结果
# - 聚合后的最终报告
# - 执行时间和资源消耗
```

### 示例 2: 代码审查工作流

```python
# 创建代码审查工作流
workflow = WorkflowBuilder.fan_out_fan_in(
    name="PR 审查",
    parallel_tasks=[
        {"name": "功能审查", "description": "审查功能实现", "agent_id": "feature_reviewer"},
        {"name": "性能审查", "description": "审查性能影响", "agent_id": "performance_reviewer"},
        {"name": "安全审查", "description": "审查安全问题", "agent_id": "security_reviewer"},
    ],
    final_task={
        "name": "汇总审查",
        "description": "汇总所有审查意见并给出最终建议",
        "agent_id": "lead_reviewer"
    }
)

# 使用共享状态存储审查结果
shared_state = SharedStateManager()

# 执行工作流
executor = WorkflowExecutor(router=router, llm=llm_client, shared_state=shared_state)
result = await executor.execute(workflow)

# 获取最终审查意见
final_review = await shared_state.get("final_review")
```

## 架构设计

```
┌─────────────────────────────────────────────────────────┐
│                    MultiAgentCoordinator                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │   Task       │  │    Task      │  │   Result     │  │
│  │ Decomposer   │  │  Executor    │  │ Aggregator   │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────┘
         │                    │                    │
         │                    │                    │
         ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────┐
│                     AgentRouter                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │ Agent 1  │  │ Agent 2  │  │ Agent 3  │             │
│  └──────────┘  └──────────┘  └──────────┘             │
└─────────────────────────────────────────────────────────┘
         │                    │                    │
         │                    │                    │
         ▼                    ▼                    ▼
┌─────────────────────────────────────────────────────────┐
│                  SharedStateManager                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ Task Results │  │Agent States  │  │  Shared Data │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## 性能优化

1. **任务并行执行**：无依赖的任务自动并行执行
2. **结果缓存**：相同任务的执行结果可以缓存复用
3. **增量聚合**：支持流式聚合，不需要等待所有任务完成
4. **资源限制**：可以限制并发任务数量，避免资源耗尽

## 错误处理

- **任务失败重试**：自动重试失败的任务（可配置重试次数）
- **降级策略**：关键任务失败时可以使用降级策略
- **超时控制**：每个任务可以设置超时时间
- **错误隔离**：单个任务失败不影响其他任务执行

## 监控和调试

```python
# 查看工作流状态
print(f"工作流状态: {workflow.status}")
print(f"已完成节点: {sum(1 for n in workflow.nodes if n.status == 'completed')}")
print(f"失败节点: {sum(1 for n in workflow.nodes if n.status == 'failed')}")

# 查看共享状态
snapshot = await shared_state.snapshot()
print(f"共享状态: {snapshot}")

# 查看执行历史
history = coordinator.get_execution_history()
for record in history[-10:]:
    print(f"{record['task_id']}: {record['status']} ({record['duration']:.2f}s)")
```

## 最佳实践

1. **任务分解粒度**：任务不宜过细，避免协调开销过大
2. **Agent 专长**：为不同 Agent 分配明确的专长领域
3. **共享状态最小化**：只共享必要的数据，避免冲突
4. **错误处理**：为关键任务设置重试和降级策略
5. **资源监控**：监控 Agent 的资源使用情况，及时调整

## 测试

运行多 Agent 协作系统的测试：

```bash
pytest tests/unit/test_multiagent.py -v
```

当前测试覆盖：
- 任务分解和执行
- 结果聚合
- 工作流构建和执行
- 共享状态管理
- 集成场景
