"""配置管理 — 运行时可修改的 LLM 配置.

优化 (2026-08-01):
- env_file 路径使用 Path(__file__) 动态计算，不再硬编码
- 支持 fallback_models 列表（多级 fallback 链），从配置读取
- 支持 SCOUT_CORS_ORIGINS 环境变量配置 CORS

迁移 (2026-08-03):
- 原 scout/config.py 被同名包遮蔽，现正式迁入 scout/config/manager.py。
  _PROJECT_ROOT 相应改为向上三级（manager.py -> config -> scout -> 项目根）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from scout.security.secret import (
    decrypt_secret as _decrypt_field,
    encrypt_secret as _encrypt_field,
    is_encrypted as _is_encrypted,
)

# 项目根目录 — 动态计算，不再硬编码
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 统一路径（2026-08-30）：SCOUT_CONFIG_DIR 或 <项目根>/.scout，随项目迁移；
# exe 便携模式由 launcher 设置 SCOUT_CONFIG_DIR=exe旁data，不再写死 C 盘 ~/.scout
from scout.config.paths import CONFIG_PATH  # noqa: E402

# 需要加密存储的敏感字段（直接值）
_SENSITIVE_FIELDS = ("api_key",)

# 需要加密存储的敏感字段（dict 的 value，如 provider -> api_key 映射）
_SENSITIVE_MAP_FIELDS = ("provider_keys",)

# 首次启动时生成配置文件的模板
INITIAL_CONFIG = {
    "provider": "dashscope",
    "model": "qwen-plus",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "",
    "provider_keys": {},
    # provider -> base_url 多厂商映射（明文，base_url 非敏感）
    "provider_base_urls": {},
    # 视觉/图像/Embedding 模型的独立厂商（空 = 跟随主 provider）
    "vision_provider": "",
    "image_provider": "",
    "embedding_provider": "",
    "max_turns": 30,
    "max_loop_seconds": 600,  # 2026-08-28：对话回合总时长上限（秒），超时强制收尾，防止卡死
    "temperature": 0.7,
    "system_prompt": "",  # 已弃用（2026-08-25）：禁止自定义，统一内置模板，仅保留字段兼容旧配置
    "deep_thinking": True,
    "agent_mode": "react",
    "vision_model": "",
    "embedding_model": "local",
    "image_model": "",
    "fallback_model": "",
    "fallback_models": [],
    "failover_primary_model": "",
    "failover_fallback_model": "",
    "web_host": "127.0.0.1",  # 安全默认：仅本机。对外访问请显式配置
    "web_port": 8848,
    "web_docs": False,  # FastAPI 交互式文档（/docs）默认关闭，避免泄露 API 结构
    "a2a_allow_private": False,  # 允许 A2A 连接私有/内网地址（默认拦截，防 SSRF）
    "cors_origins": [],
    "max_retries": 3,
    "retry_backoff_base": 2.0,
    "retry_backoff_max": 30.0,
    "stream_timeout": 180,
    "request_timeout": 90,
    # 回复语言: auto=跟随用户 / zh=始终中文 / en=始终英文
    "language": "auto",
    # 会话恢复: 启动/刷新时恢复上次会话与上次模型
    "restore_last_session": True,
    "restore_last_model": True,
    # 搜索引擎：search_engine 为旧版单值（SearXNG 实例 URL，兼容保留）；
    # search_engines 为多源列表，每项 {name,type,url,api_key,enabled}。
    # 全部为空 = 未配置 → web_search 工具与技能全网搜索不可用
    "search_engine": "",
    "search_engines": [],
    "auth_enabled": False,  # 登录认证开关（默认关闭；开启后访问 Web 界面需登录）
}


class LLMConfig(BaseModel):
    """LLM 配置模型 — 所有字段都是可选的，从 ~/.scout/config.json 读取."""
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    provider_keys: dict[str, str] = Field(default_factory=dict)  # provider -> api_key 多 key 映射
    provider_base_urls: dict[str, str] = Field(default_factory=dict)  # provider -> base_url 多厂商映射
    vision_provider: str = ""  # 视觉模型独立厂商（空 = 跟随主 provider）
    image_provider: str = ""  # 图像模型独立厂商（空 = 跟随主 provider）
    embedding_provider: str = ""  # Embedding 模型独立厂商（空 = 跟随主 provider）
    max_turns: int = 0
    max_loop_seconds: int = 600
    temperature: float = 0.0
    system_prompt: str = ""  # 已弃用（2026-08-25）：禁止自定义，仅保留字段兼容旧配置，Agent 不再读取
    deep_thinking: bool = False
    agent_mode: str = "react"  # react / multi_agent
    vision_model: str = ""
    embedding_model: str = ""
    image_model: str = ""
    fallback_model: str = ""
    fallback_models: list[str] = []
    failover_primary_model: str = ""
    failover_fallback_model: str = ""
    web_host: str = ""
    web_port: int = 0
    web_docs: bool = False
    a2a_allow_private: bool = False
    cors_origins: list[str] = []
    max_retries: int = 0
    retry_backoff_base: float = 0.0
    retry_backoff_max: float = 0.0
    stream_timeout: int = 0
    request_timeout: int = 0
    sandbox_mode: str = "off"  # off / non-main / all
    auto_approve: bool = True  # 自动审批工具执行
    language: str = "auto"  # auto=跟随用户 / zh=中文 / en=英文
    restore_last_session: bool = True  # 恢复上次会话
    restore_last_model: bool = True  # 恢复上次模型
    search_engine: str = ""  # 旧版单值 SearXNG URL（兼容，优先使用 search_engines）
    search_engines: list[dict] = []  # 多搜索引擎源：{name,type,url,api_key,enabled}
    auth_enabled: bool = False  # 登录认证开关（默认关闭）


class ConfigManager:
    """配置管理器 — 读写 config.json."""

    def __init__(self):
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 首次启动时，自动生成默认配置文件
        if not CONFIG_PATH.exists():
            self._create_default_config()

    def _create_default_config(self):
        """创建默认配置文件."""
        with open(CONFIG_PATH, "w") as f:
            json.dump(INITIAL_CONFIG, f, indent=2, ensure_ascii=False)

    def load(self) -> LLMConfig:
        """加载配置 — 从 ~/.scout/config.json 读取."""
        # 1. 从 .env 加载（仅用于向后兼容）
        config = {}
        env_candidates = [
            Path.cwd() / ".env",
            Path.home() / "scout-agent" / ".env",
            _PROJECT_ROOT / ".env",
        ]
        for env_path in env_candidates:
            if env_path.exists():
                self._load_env_file(env_path, config)
                break

        # 2. 从 ~/.scout/config.json 读取（覆盖 .env）
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
                config.update(saved)

        # 3. 合并默认值（确保所有字段都有值）。
        #    过滤配置文件中的 None 值：JSON 里显式写的 null 不应覆盖默认字段，
        #    否则会把 base_url 等必填 str 字段变成 None，导致 pydantic 校验崩溃。
        merged = dict(INITIAL_CONFIG)
        for _k, _v in config.items():
            if _v is not None:
                merged[_k] = _v

        # 4. 解密敏感字段（api_key / provider_keys）。
        #    同时做平滑迁移：若字段是非空的明文，则自动加密并回写，
        #    保证配置文件里不再出现明文 key。
        need_migrate = False
        for field in _SENSITIVE_FIELDS:
            if merged.get(field):
                if _is_encrypted(merged[field]):
                    merged[field] = _decrypt_field(merged[field])
                else:
                    need_migrate = True
        for field in _SENSITIVE_MAP_FIELDS:
            raw = merged.get(field) or {}
            decrypted = {}
            for key, value in raw.items():
                if value and _is_encrypted(value):
                    decrypted[key] = _decrypt_field(value)
                else:
                    if value:
                        need_migrate = True
                    decrypted[key] = value
            merged[field] = decrypted
        # search_engines 列表内嵌 api_key 解密（平滑迁移明文→加密）
        engines = merged.get("search_engines")
        if isinstance(engines, list):
            migrated_engines = []
            for e in engines:
                if isinstance(e, dict) and e.get("api_key"):
                    e = dict(e)
                    if _is_encrypted(e["api_key"]):
                        e["api_key"] = _decrypt_field(e["api_key"])
                    else:
                        need_migrate = True
                migrated_engines.append(e)
            merged["search_engines"] = migrated_engines
        if need_migrate:
            self._write_encrypted(merged)

        return LLMConfig(**merged)

    def save(self, config: LLMConfig) -> None:
        """保存配置到 config.json（敏感字段加密存储）."""
        data = config.model_dump()
        self._write_encrypted(data)

    def save_provider_key(
        self, provider: str, api_key: str, activate: bool = True, base_url: str | None = None
    ) -> None:
        """保存某 provider 的 API key 到 provider_keys 映射（加密落盘）.

        activate=True 时同步切换当前激活配置（provider + api_key + base_url）。
        api_key 为空则删除该 provider 的已存 key；base_url 为空则删除已存 base_url。
        """
        config = self.load()
        keys = dict(config.provider_keys or {})
        if api_key:
            keys[provider] = api_key
        else:
            keys.pop(provider, None)
        config.provider_keys = keys
        # 同步保存该 provider 的 base_url（与 key 一起按厂商记忆）
        urls = dict(config.provider_base_urls or {})
        if base_url:
            urls[provider] = base_url
        else:
            urls.pop(provider, None)
        config.provider_base_urls = urls
        if activate and api_key:
            config.api_key = api_key
            config.provider = provider
        if activate and base_url:
            config.base_url = base_url
        self.save(config)

    def save_provider_base_url(self, provider: str, base_url: str, activate: bool = False) -> None:
        """仅保存某 provider 的 base_url 到 provider_base_urls 映射（明文落盘）.

        activate=True 时同步更新当前激活配置的 base_url。
        base_url 为空则删除该 provider 的已存 base_url。
        """
        config = self.load()
        urls = dict(config.provider_base_urls or {})
        if base_url:
            urls[provider] = base_url
        else:
            urls.pop(provider, None)
        config.provider_base_urls = urls
        if activate and base_url:
            config.base_url = base_url
        self.save(config)

    def list_provider_keys(self) -> dict[str, bool]:
        """列出已保存 key 的 provider（仅返回是否存在，不泄露明文）."""
        config = self.load()
        return {p: bool(k) for p, k in (config.provider_keys or {}).items()}

    def list_provider_base_urls(self) -> dict[str, str]:
        """列出各 provider 已保存的 base_url（明文）."""
        config = self.load()
        return {p: u for p, u in (config.provider_base_urls or {}).items()}

    def get_provider_credentials(self, provider: str) -> tuple[str, str]:
        """返回某厂商已保存的 (api_key, base_url)；未保存时返回空串."""
        config = self.load()
        key = (config.provider_keys or {}).get(provider) or ""
        url = (config.provider_base_urls or {}).get(provider) or ""
        return key, url

    def save_scope_providers(
        self,
        vision_provider: str | None = None,
        image_provider: str | None = None,
        embedding_provider: str | None = None,
    ) -> None:
        """保存视觉/图像/Embedding 模型的独立厂商（空字符串 = 跟随主服务商）."""
        config = self.load()
        if vision_provider is not None:
            config.vision_provider = (vision_provider or "").strip()
        if image_provider is not None:
            config.image_provider = (image_provider or "").strip()
        if embedding_provider is not None:
            config.embedding_provider = (embedding_provider or "").strip()
        self.save(config)

    def resolve_scoped_credentials(self, scope_provider: str, main_provider: str) -> tuple[str, str, str]:
        """解析某模型类型（视觉/图像）最终使用的凭据.

        返回 (provider, api_key, base_url)：scope_provider 非空且不等于主厂商时，
        使用该厂商已保存的 key/base_url；否则回退主配置，key/base_url 为 "" 表示
        由调用方回退到主配置 api_key/base_url。
        """
        if scope_provider and scope_provider != main_provider:
            key, url = self.get_provider_credentials(scope_provider)
            return scope_provider, key, url
        return main_provider, "", ""

    def activate_provider(
        self,
        provider: str,
        model: str | None = None,
        base_url: str | None = None,
    ) -> bool:
        """切换当前激活 provider（key 从已保存的 provider_keys 中取）.

        返回是否切换成功（该 provider 未保存 key 时返回 False）。
        """
        config = self.load()
        key = (config.provider_keys or {}).get(provider)
        if not key:
            return False
        config.provider = provider
        config.api_key = key
        if model:
            config.model = model
        # 优先恢复该 provider 已保存的 base_url；否则用显式传入值；否则保持不变
        saved_url = (config.provider_base_urls or {}).get(provider)
        if saved_url:
            config.base_url = saved_url
        elif base_url is not None:
            config.base_url = base_url
        self.save(config)
        return True

    def _write_encrypted(self, data: dict) -> None:
        """将配置写入 config.json，敏感字段先加密."""
        out = dict(data)
        for field in _SENSITIVE_FIELDS:
            if out.get(field):
                out[field] = _encrypt_field(out[field])
        for field in _SENSITIVE_MAP_FIELDS:
            raw = out.get(field) or {}
            if raw:
                out[field] = {k: _encrypt_field(v) for k, v in raw.items() if v}
        # search_engines 列表内嵌 api_key 加密
        engines = out.get("search_engines")
        if isinstance(engines, list):
            cleaned = []
            for e in engines:
                if isinstance(e, dict):
                    e = dict(e)
                    if e.get("api_key"):
                        e["api_key"] = _encrypt_field(e["api_key"])
                cleaned.append(e)
            out["search_engines"] = cleaned
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)

    def _load_env_file(self, path: Path, config: dict) -> None:
        """从 .env 文件加载配置."""
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    mapping = {
                        "SCOUT_LLM_API_KEY": "api_key",
                        "SCOUT_LLM_MODEL": "model",
                        "SCOUT_LLM_PROVIDER": "provider",
                        "SCOUT_LLM_BASE_URL": "base_url",
                        "SCOUT_FALLBACK_MODEL": "fallback_model",
                        "SCOUT_FALLBACK_MODELS": "fallback_models",
                        "SCOUT_SEARCH_ENGINE": "search_engine",
                    }
                    if key in mapping and value:
                        config_key = mapping[key]
                        # fallback_models 支持逗号分隔的列表
                        if config_key == "fallback_models":
                            config[config_key] = [m.strip() for m in value.split(",") if m.strip()]
                        else:
                            config[config_key] = value
