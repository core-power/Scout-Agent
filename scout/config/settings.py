"""配置管理 — 环境变量加载与验证."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


def _resolve_source_root() -> Path:
    """解析 Scout 源码安装根目录.

    通过当前文件 (scout/config/settings.py) 向上推导:
        scout/config/settings.py -> scout/config -> scout -> <source_root>
    
    返回源码根目录的绝对路径。
    """
    return Path(__file__).resolve().parent.parent.parent


def _resolve_data_dir() -> Path:
    """解析运行时数据目录.

    优先级:
    1. 环境变量 SCOUT_DATA_DIR (如果设置)
    2. ~/.scout/ (默认)
    """
    env_dir = os.getenv("SCOUT_DATA_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    
    # 默认：~/.scout/
    return Path.home() / ".scout"


@dataclass
class LLMConfig:
    """LLM 配置."""
    provider: str
    model: str
    api_key: str
    base_url: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 4096


@dataclass
class EmbeddingConfig:
    """Embedding 模型配置."""
    provider: str = ""  # "" = 纯文本检索 | "api" | "hash"
    api_key: Optional[str] = None
    api_base_url: Optional[str] = None
    api_model: Optional[str] = None
    dimension: int = 1024  # 向量维度（API 嵌入按模型推断时覆盖）
    max_length: int = 512  # 最大 token 长度


@dataclass
class ScoutConfig:
    """Scout Agent 全局配置."""
    llm: LLMConfig
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    data_dir: Path = field(default_factory=lambda: _resolve_data_dir())
    log_level: str = "INFO"
    
    # Runtime paths (separation of source and runtime artifacts)
    @property
    def source_root(self) -> Path:
        """源码安装根目录."""
        return _resolve_source_root()
    
    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"
    
    @property
    def mcp_dir(self) -> Path:
        return self.data_dir / "mcp"
    
    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"
    
    @property
    def db_path(self) -> Path:
        return self.data_dir / "scout.db"
    
    @property
    def vector_store_path(self) -> Path:
        return self.data_dir / "vector_store"
    
    def ensure_dirs(self):
        """确保所有运行时目录存在."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.mcp_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.vector_store_path.mkdir(parents=True, exist_ok=True)
    
    @classmethod
    def from_env(cls) -> "ScoutConfig":
        """从环境变量加载配置."""
        # 尝试从源码根目录加载 .env 文件
        source_root = _resolve_source_root()
        env_file = source_root / ".env"
        if env_file.exists():
            load_dotenv(env_file)
        else:
            load_dotenv()
        
        # 验证必需的环境变量
        required_vars = ["SCOUT_LLM_API_KEY", "SCOUT_LLM_MODEL", "SCOUT_LLM_PROVIDER"]
        missing = [v for v in required_vars if not os.getenv(v)]
        if missing:
            raise ValueError(f"缺少必需的环境变量: {', '.join(missing)}")
        
        llm_config = LLMConfig(
            provider=os.getenv("SCOUT_LLM_PROVIDER", "openai"),
            model=os.getenv("SCOUT_LLM_MODEL", "gpt-4"),
            api_key=os.getenv("SCOUT_LLM_API_KEY", ""),
            base_url=os.getenv("SCOUT_LLM_BASE_URL"),
            temperature=float(os.getenv("SCOUT_LLM_TEMPERATURE", "0.7")),
            max_tokens=int(os.getenv("SCOUT_LLM_MAX_TOKENS", "4096")),
        )
        
        # Embedding 配置 — 默认纯文本检索（无需本地模型/API Key）
        embedding_config = EmbeddingConfig(
            provider=os.getenv("SCOUT_EMBEDDING_PROVIDER", ""),
            api_key=os.getenv("SCOUT_EMBEDDING_API_KEY"),
            api_base_url=os.getenv("SCOUT_EMBEDDING_API_BASE_URL"),
            api_model=os.getenv("SCOUT_EMBEDDING_API_MODEL"),
            dimension=int(os.getenv("SCOUT_EMBEDDING_DIMENSION", "1024")),
            max_length=int(os.getenv("SCOUT_EMBEDDING_MAX_LENGTH", "512")),
        )
        
        return cls(
            llm=llm_config,
            embedding=embedding_config,
            data_dir=_resolve_data_dir(),
            log_level=os.getenv("SCOUT_LOG_LEVEL", "INFO"),
        )


# 全局配置实例
_config: Optional[ScoutConfig] = None


def get_config() -> ScoutConfig:
    """获取全局配置（单例）."""
    global _config
    if _config is None:
        _config = ScoutConfig.from_env()
    return _config


def reset_config():
    """重置全局配置（用于测试）."""
    global _config
    _config = None
