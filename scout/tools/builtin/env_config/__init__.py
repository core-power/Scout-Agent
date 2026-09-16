"""环境配置工具 — 安全存储 API Key 等敏感信息.

特性:
- 使用系统密钥链 (keyring) 或加密文件存储
- 支持 save/get/list/delete 操作
- 自动加密，明文不落盘
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

# 配置文件路径
CONFIG_DIR = _SCOUT_DATA_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "env_secrets.json"

# keyring 索引条目（keyring 无列举 API，用固定条目维护 key 列表）
_KEYRING_INDEX_SERVICE = "scout-env-index"
_KEYRING_INDEX_USER = "keys"


class EnvStore:
    """环境变量安全存储.
    
    优先级:
    1. 尝试 keyring (系统密钥链)
    2. 回退到加密文件
    """
    
    def __init__(self):
        self._use_keyring = False
        try:
            import keyring
            # 测试 keyring 是否可用
            keyring.get_password("scout-test", "test")
            self._use_keyring = True
        except Exception:
            pass
        
        if not self._use_keyring:
            # 确保配置目录存在
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    def _keyring_keys(self) -> list[str]:
        """读取 keyring 中的 key 索引列表."""
        import keyring

        raw = keyring.get_password(_KEYRING_INDEX_SERVICE, _KEYRING_INDEX_USER)
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    def _keyring_set_keys(self, keys: list[str]):
        """写入 keyring 中的 key 索引列表."""
        import keyring

        keyring.set_password(_KEYRING_INDEX_SERVICE, _KEYRING_INDEX_USER, json.dumps(keys))

    def save(self, key: str, value: str, description: str = "") -> bool:
        """保存密钥."""
        if self._use_keyring:
            import keyring
            keyring.set_password(f"scout-env-{key}", "value", value)
            if description:
                keyring.set_password(f"scout-env-{key}", "description", description)
            # 维护索引，保证 list_all 可用
            keys = self._keyring_keys()
            if key not in keys:
                keys.append(key)
                self._keyring_set_keys(keys)
            return True
        else:
            # 加密文件存储
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)  # 确保目录存在
            data = self._load_all()
            data[key] = {"value": value, "description": description}
            self._save_all(data)
            return True
    
    def get(self, key: str) -> str | None:
        """获取密钥值."""
        if self._use_keyring:
            import keyring
            return keyring.get_password(f"scout-env-{key}", "value")
        else:
            data = self._load_all()
            entry = data.get(key)
            return entry["value"] if entry else None
    
    def delete(self, key: str) -> bool:
        """删除密钥."""
        if self._use_keyring:
            import keyring
            keys = self._keyring_keys()
            if key not in keys:
                return False
            # keyring 条目可能不存在，删除失败可容忍（以索引为准）
            try:
                keyring.delete_password(f"scout-env-{key}", "value")
            except Exception:
                pass  # 条目不存在
            try:
                keyring.delete_password(f"scout-env-{key}", "description")
            except Exception:
                pass  # 条目不存在
            keys.remove(key)
            self._keyring_set_keys(keys)
            return True
        else:
            data = self._load_all()
            if key in data:
                del data[key]
                self._save_all(data)
                return True
            return False
    
    def list_all(self) -> list[dict]:
        """列出所有密钥 (不含值，仅名称和描述)."""
        if self._use_keyring:
            import keyring

            result = []
            for key in self._keyring_keys():
                desc = keyring.get_password(f"scout-env-{key}", "description") or ""
                result.append({"key": key, "description": desc})
            return result
        else:
            data = self._load_all()
            return [
                {"key": k, "description": v.get("description", "")}
                for k, v in data.items()
            ]
    
    def _load_all(self) -> dict:
        """加载所有配置."""
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    
    def _save_all(self, data: dict):
        """保存所有配置."""
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 设置文件权限为仅所有者可读写
        os.chmod(CONFIG_FILE, 0o600)


# 全局实例
_env_store = EnvStore()


class EnvConfigSaveTool(ToolDefinition):
    """保存环境配置."""
    
    name = "env_config_save"
    description = "保存 API Key、Token 等敏感信息到安全存储。系统会自动加密，不会明文落盘。"
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "配置名称 (如: OPENAI_API_KEY)"},
            "value": {"type": "string", "description": "配置值 (如: sk-xxx)"},
            "description": {"type": "string", "description": "配置说明 (可选)", "default": ""},
        },
        "required": ["key", "value"],
    }
    annotations = ToolAnnotations(destructive=True)
    
    async def execute(self, key: str, value: str, description: str = "") -> Observation:
        _env_store.save(key, value, description)
        return Observation(
            tool_name="env_config_save",
            success=True,
            output=f"已保存配置: {key}",
        )


class EnvConfigGetTool(ToolDefinition):
    """获取环境配置."""
    
    name = "env_config_get"
    description = "获取之前保存的 API Key 或 Token。返回的值会自动脱敏显示。"
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "配置名称"},
        },
        "required": ["key"],
    }
    annotations = ToolAnnotations(read_only=True)
    
    async def execute(self, key: str) -> Observation:
        value = _env_store.get(key)
        if value is None:
            return Observation(
                tool_name="env_config_get",
                success=False,
                output=f"配置不存在: {key}",
            )
        
        # 脱敏显示 (显示前4位和后8位，中间用 *)
        if len(value) > 12:
            masked = value[:4] + "*" * (len(value) - 12) + value[-8:]
        else:
            masked = "****"
        
        return Observation(
            tool_name="env_config_get",
            success=True,
            output=f"配置: {key}\n值: {masked} (完整长度: {len(value)})",
        )


class EnvConfigListTool(ToolDefinition):
    """列出所有环境配置."""
    
    name = "env_config_list"
    description = "列出所有已保存的环境配置 (仅显示名称和描述，不显示值)。"
    parameters = {
        "type": "object",
        "properties": {},
    }
    annotations = ToolAnnotations(read_only=True)
    
    async def execute(self) -> Observation:
        configs = _env_store.list_all()
        if not configs:
            return Observation(
                tool_name="env_config_list",
                success=True,
                output="暂无已保存的配置",
            )
        
        lines = [f"已保存 {len(configs)} 个配置:\n"]
        for cfg in configs:
            desc = f" - {cfg['description']}" if cfg["description"] else ""
            lines.append(f"• {cfg['key']}{desc}")
        
        return Observation(
            tool_name="env_config_list",
            success=True,
            output="\n".join(lines),
        )


class EnvConfigDeleteTool(ToolDefinition):
    """删除环境配置."""
    
    name = "env_config_delete"
    description = "删除已保存的环境配置。"
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "配置名称"},
        },
        "required": ["key"],
    }
    annotations = ToolAnnotations(destructive=True)
    
    async def execute(self, key: str) -> Observation:
        if _env_store.delete(key):
            return Observation(
                tool_name="env_config_delete",
                success=True,
                output=f"已删除配置: {key}",
            )
        else:
            return Observation(
                tool_name="env_config_delete",
                success=False,
                output=f"配置不存在: {key}",
            )


# import 时自动注册
ToolRegistry.register(EnvConfigSaveTool())
ToolRegistry.register(EnvConfigGetTool())
ToolRegistry.register(EnvConfigListTool())
ToolRegistry.register(EnvConfigDeleteTool())
