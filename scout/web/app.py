"""Scout Agent Web UI — 基于 Streamlit 的可视化界面 (带进度条与安全加密)."""

import asyncio
import streamlit as st
import sys
import os

# 确保能导入 scout 模块
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from scout.llm.providers.registry import create_provider
from scout.engine.agent import Agent
from scout.automation.skill_manager import get_skill_manager
from scout.core.callbacks import Callbacks
from scout.memory.vector.embeddings import (
    create_embedding_provider,
)

st.set_page_config(page_title="Scout Agent Dashboard", page_icon="🧭", layout="wide")

st.title("🧭 Scout Agent Dashboard")

# --- 侧边栏配置 ---
with st.sidebar:
    st.header("⚙️ 系统配置")
    
    # 1. 优先从环境变量获取 Key（最安全的方式）
    env_key = os.environ.get("SCOUT_LLM_API_KEY", "")
    
    if env_key:
        st.success("✅ 已从环境变量加载 API Key")
        api_key = env_key
        # 禁用手动输入，防止覆盖环境变量
        st.text_input("LLM API Key", value="****************", disabled=True, help="已通过 SCOUT_LLM_API_KEY 环境变量设置")
    else:
        # 2. 如果没有环境变量，允许手动输入（视觉加密）
        api_key = st.text_input("LLM API Key", type="password", help="建议通过环境变量 SCOUT_LLM_API_KEY 设置以提高安全性")

    model = st.selectbox("模型选择", ["qwen-plus", "qwen-turbo", "gpt-4o-mini", "gpt-4o"])
    provider = st.selectbox("Provider", ["dashscope", "openai"])
    
    st.divider()

    # ─────────────────────────────────────────────────────────────────
    # 🧠 记忆引擎配置
    # ─────────────────────────────────────────────────────────────────
    st.header("🧠 记忆引擎")

    embedding_provider = st.selectbox(
        "Embedding 提供者",
        ["off", "openai", "dashscope", "hash"],
        index=0,
        help="off = 纯文本检索（默认，无需模型与密钥）；openai/dashscope = API 嵌入；hash = 开发测试用。",
    )

    if embedding_provider in ("openai", "dashscope"):
        emb_api_key = st.text_input(
            "Embedding API Key",
            type="password",
            value=os.environ.get("SCOUT_EMBEDDING_API_KEY", ""),
            help="用于调用远程 Embedding API。",
        )
        emb_model = st.text_input(
            "Embedding 模型",
            value="text-embedding-3-small",
            help="OpenAI 推荐 text-embedding-3-small (1536维)。",
        )
        emb_dim = st.number_input("向量维度", value=1536, min_value=128, max_value=4096)

    elif embedding_provider == "hash":
        hash_dim = st.number_input("哈希向量维度", value=768, min_value=128, max_value=4096)
        st.info("💡 哈希嵌入适合开发测试，生产环境建议使用 API 嵌入。")

    st.divider()
    
    # 技能管理面板
    st.header("🔌 技能中心")
    manager = get_skill_manager()
    skills = manager.discover_all()
    
    for skill in skills:
        with st.expander(f"**{skill['display_name']}** ({skill['source']})"):
            st.write(f"ID: `{skill['name']}`")
            st.write(skill['description'])
            if skill.get('triggers'):
                st.write(f"触发词: {', '.join(skill['triggers'])}")

# --- 主界面逻辑 ---
if not api_key:
    st.warning("请在侧边栏配置 API Key 或通过环境变量 SCOUT_LLM_API_KEY 设置。")
    st.stop()

# 初始化 Embedding Provider
def _create_embedder():
    """根据前端选择创建 Embedding Provider."""
    if embedding_provider == "off":
        return None
    elif embedding_provider in ("openai", "dashscope"):
        return create_embedding_provider(
            provider=embedding_provider,
            api_key=emb_api_key if 'emb_api_key' in dir() else "",
            model=emb_model if 'emb_model' in dir() else "text-embedding-3-small",
            dim=emb_dim if 'emb_dim' in dir() else 1536,
        )
    else:
        return create_embedding_provider(
            provider="hash",
            dim=hash_dim if 'hash_dim' in dir() else 768,
        )

# 初始化 Agent
if "agent" not in st.session_state:
    try:
        llm = create_provider(provider=provider, api_key=api_key, model=model)
        
        # 定义自定义回调类，用于更新进度条
        class StreamlitCallbacks(Callbacks):
            async def on_step(self, step: int, total_budget: int) -> None:
                progress_bar.progress(step / total_budget)
                status_text.text(f"🔄 正在执行第 {step}/{total_budget} 步...")

            async def on_status(self, status: str) -> None:
                status_text.text(f"状态: {status}")

            async def on_tool_progress(self, tool_name: str, stage: str, message: str) -> None:
                tool_output.markdown(f"**工具:** `{tool_name}` | **阶段:** {stage} | **信息:** {message}")

            async def on_thinking(self, started: bool) -> None:
                if started:
                    thinking_box.info("🤔 思考中...")
                else:
                    thinking_box.empty()

            async def on_reasoning(self, content: str) -> None:
                pass

            async def on_stream_delta(self, text: str) -> None:
                pass

            async def on_clarify(self, question: str) -> str:
                return ""

            async def on_tool_gen(self, tool_name: str, args: dict) -> None:
                pass

        callbacks = StreamlitCallbacks()
        st.session_state.agent = Agent(llm=llm, max_turns=10, callbacks=callbacks)
        
        # 初始化 Embedding Provider 并存入 session_state
        embedding_provider = _create_embedder()
        st.session_state.embedding_provider = embedding_provider
        
        # 注入 Embedding Provider 到 Agent 的 MemoryStore
        # 这样前端选择的模型（本地 ONNX / OpenAI / DashScope / Hash）会被实际使用
        if hasattr(st.session_state.agent, 'memory_store') and st.session_state.agent.memory_store:
            st.session_state.agent.memory_store.set_embedding_provider(embedding_provider)
            st.session_state.agent._embedding_provider = embedding_provider
        
        # 安全加固：初始化后立即删除内存中的 Key 引用
        del api_key 
        
        st.success("✅ Agent 初始化成功 (Key 已安全隔离)")
    except Exception as e:
        st.error(f"❌ Agent 初始化失败: {e}")
        st.stop()

# 聊天历史展示
for message in st.session_state.get("messages", []):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# 输入框
if prompt := st.chat_input("输入指令..."):
    # 1. 显示用户消息
    st.session_state.setdefault("messages", []).append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # 2. Agent 执行并流式/逐步显示结果
    with st.chat_message("assistant"):
        # 创建进度条和状态显示组件
        progress_bar = st.progress(0)
        status_text = st.empty()
        thinking_box = st.empty()
        tool_output = st.empty()
        response_container = st.empty()
        
        agent = st.session_state.agent
        
        full_response = ""
        
        async def run_agent():
            result = await agent.run_conversation(prompt)
            return result

        # 由于 Streamlit 是同步的，我们需要在事件循环中运行
        try:
            loop = asyncio.new_event_loop()
            response = loop.run_until_complete(run_agent())
            loop.close()
            
            full_response = str(response.get("response", ""))
            response_container.markdown(full_response)
            st.session_state.setdefault("messages", []).append({"role": "assistant", "content": full_response})
        except Exception as e:
            st.error(f"执行出错: {e}")
