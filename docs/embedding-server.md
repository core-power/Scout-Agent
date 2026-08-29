# 自托管 Embedding 服务

Scout Agent 的向量语义检索默认使用云 API（如 DashScope `text-embedding-v3`）。
如果你希望**自己部署**一个 Embedding 服务（内网、私有化、无外部依赖），
仓库内置了独立的 OpenAI 兼容 Embedding HTTP 服务：`tools/embedding_server.py`。

它不依赖 Scout 包、不修改 Agent 进程，是一个可独立启动/停止的轻量服务。

## 特性

- **OpenAI 兼容接口**：`POST /v1/embeddings`，任何 OpenAI 兼容客户端可直接接入
- **本地模型后端**：基于 sentence-transformers（BGE / M3E / text2vec / gte 等中英文模型），CPU / CUDA / MPS 均支持
- **代理后端**：可把请求转发到任意 OpenAI 兼容 Embedding API，作为统一入口与鉴权层
- **文本级缓存**：相同文本直接复用向量（上限 1 万条），重复检索零开销
- **可选鉴权**：设置 token 后需携带 `Authorization: Bearer <token>`

## 快速开始

### 1. 安装依赖

```bash
pip install "sentence-transformers>=2.2.0"   # 本地模型后端需要
pip install "fastapi" "uvicorn" "httpx"       # 服务框架（Scout 依赖中通常已包含）
```

### 2. 启动服务（本地模型）

```bash
python tools/embedding_server.py --model BAAI/bge-small-zh-v1.5 --port 8849
```

首次运行会自动下载模型（约 100MB，缓存于 `~/.cache/huggingface`）。更多模型推荐：

| 模型 | 维度 | 语言 | 说明 |
|------|------|------|------|
| `BAAI/bge-small-zh-v1.5` | 512 | 中英 | 轻量、速度快，默认推荐 |
| `BAAI/bge-base-zh-v1.5` | 768 | 中英 | 精度更高 |
| `BAAI/bge-large-zh-v1.5` | 1024 | 中英 | 精度最高（需较多内存） |
| `moka-ai/m3e-base` | 768 | 中英 | 通用场景 |
| `shibing624/text2vec-base-chinese` | 768 | 中文 | 中文语义匹配 |

有 GPU 时加 `--device cuda`（或 `mps`）。

### 3. 启动服务（代理模式）

无本地 GPU、但希望统一管理多个 Embedding 上游时：

```bash
python tools/embedding_server.py \
  --upstream https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --upstream-key $DASHSCOPE_API_KEY \
  --upstream-model text-embedding-v3
```

### 4. 验证

```bash
curl -s http://127.0.0.1:8849/health
# {"status":"ok","backend":"local","model":"BAAI/bge-small-zh-v1.5"}

curl -s http://127.0.0.1:8849/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"input": ["你好", "hello world"]}'
# {"object":"list","data":[{"object":"embedding","index":0,"embedding":[...]},...]}
```

## 接入 Scout Agent

在 `.env` 中配置（无需改任何代码，Scout 客户端走标准 OpenAI 兼容协议）：

```bash
SCOUT_EMBEDDING_PROVIDER=api
SCOUT_EMBEDDING_API_KEY=local                     # 服务未启用鉴权时任意值即可
SCOUT_EMBEDDING_API_BASE_URL=http://127.0.0.1:8849/v1
SCOUT_EMBEDDING_API_MODEL=BAAI/bge-small-zh-v1.5
SCOUT_EMBEDDING_DIMENSION=512                     # 与所选模型维度一致
```

然后重启 `scout`。在 Web UI「设置 → 语义检索」中选择该模型名即可启用向量检索。

## 鉴权（可选）

```bash
python tools/embedding_server.py --auth-token your-secret-token
```

启用后，客户端需配置：

```bash
SCOUT_EMBEDDING_API_KEY=your-secret-token
```

## 安全建议

- 默认仅监听 `127.0.0.1`。局域网部署时改 `--host 0.0.0.0` 并**务必启用 `--auth-token`**
- 生产环境建议通过 systemd / docker 托管，参考 [Scout 服务管理](../README.md#服务管理)

## 全部参数

```bash
python tools/embedding_server.py --help
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--model` | `BAAI/bge-small-zh-v1.5` | 本地嵌入模型 |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8849` | 监听端口 |
| `--device` | `cpu` | `cpu` / `cuda` / `mps` |
| `--max-length` | `512` | 本地模型最大输入长度 |
| `--normalize` | 开 | L2 归一化输出向量 |
| `--auth-token` | 空 | 启用 Bearer 鉴权 |
| `--upstream` | 空 | 代理模式：上游 OpenAI 兼容 API 地址 |
| `--upstream-key` | 空 | 上游 API Key |
| `--upstream-model` | `text-embedding-3-small` | 上游模型名 |
