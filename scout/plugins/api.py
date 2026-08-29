"""插件管理 API 路由"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from pathlib import Path
import logging

from scout.plugins.manager import PluginManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plugins", tags=["plugins"])


def _require_password_confirmation(password: str) -> None:
    """插件写码操作需要密码二次确认.

    防止 JWT 泄露后通过插件上传持久化后门（token 有有效期，插件无）。
    未初始化凭证（本地单用户模式）时跳过，该场景已由全局鉴权中间件 401 拦截。
    """
    from scout.security.auth import AuthManager

    # 与全局鉴权中间件保持一致：登录认证关闭时（本地单用户模式）无需密码确认，
    # 否则 auth_enabled=False 的用户（无 token）会被空密码 401 卡住无法创建插件。
    try:
        from scout.config.manager import ConfigManager
        _cfg = ConfigManager().load()
        if not getattr(_cfg, "auth_enabled", False):
            return
    except Exception:
        pass

    mgr = AuthManager()
    if not mgr.has_credentials():
        return
    if not mgr.verify(mgr.get_username(), password):
        raise HTTPException(status_code=401, detail="密码验证失败，插件变更已拒绝")

# 统一使用 scout.plugins.manager 的全局单例（避免多套 PluginManager 状态不一致）
from scout.plugins.manager import get_plugin_manager  # noqa: F401


def set_plugin_manager(manager: PluginManager):
    """设置插件管理器实例（用于注入）"""
    global _plugin_manager
    _plugin_manager = manager


# ── 数据模型 ──

class PluginInfo(BaseModel):
    name: str
    version: str
    author: str
    description: str
    enabled: bool
    priority: int


class PluginToggleRequest(BaseModel):
    enabled: bool


class PluginCreateRequest(BaseModel):
    name: str
    code: str
    description: Optional[str] = "通过可视化构建器创建的插件"
    password: str = ""  # 密码二次确认（已设置凭证时必须匹配）


class PluginConfigRequest(BaseModel):
    config: dict


class PluginResponse(BaseModel):
    success: bool
    message: str
    plugin: Optional[PluginInfo] = None


# ── API 路由 ──

@router.post("/create", response_model=PluginResponse)
async def create_plugin(request: PluginCreateRequest):
    """通过可视化构建器创建新插件"""
    import re

    # 密码二次确认：防止 token 泄露后植入持久化后门
    _require_password_confirmation(request.password)

    manager = get_plugin_manager()
    name = request.name.strip()
    
    # 验证插件名
    if not re.match(r'^[a-z][a-z0-9_]*$', name):
        raise HTTPException(
            status_code=400, 
            detail="插件名只能包含小写字母、数字和下划线，且必须以字母开头"
        )
    
    # 检查插件是否已存在
    if manager.get_plugin(name):
        raise HTTPException(
            status_code=400,
            detail=f"插件 {name} 已存在"
        )
    
    # 处理代码中的转义字符（将 \n 转换为真正的换行符）
    code = request.code
    # 如果代码中包含字面量 \n（反斜杠+n），需要转换为真正的换行符
    if '\\n' in code and '\n' not in code:
        code = code.replace('\\n', '\n')
    
    # 创建插件目录
    plugin_dir = manager.plugins_dir / name
    try:
        plugin_dir.mkdir(parents=True, exist_ok=True)
        
        # 写入 __init__.py
        init_file = plugin_dir / "__init__.py"
        init_file.write_text(code, encoding="utf-8")
        
        # 创建空的 config.json
        config_file = plugin_dir / "config.json"
        config_file.write_text("{}", encoding="utf-8")
        
        # 加载插件：加载失败必须报错（清理目录），不能静默返回"创建成功"
        manager.discover_plugins()
        if not manager.load_plugin(name):
            raise RuntimeError(f"插件 {name} 加载失败，请检查代码是否有语法错误或导入问题")
        
        return PluginResponse(
            success=True,
            message=f"插件 {name} 创建成功"
        )
        
    except Exception as e:
        logger.error(f"创建插件失败: {e}")
        # 清理已创建的文件
        if plugin_dir.exists():
            import shutil
            shutil.rmtree(plugin_dir)
        
        raise HTTPException(
            status_code=500,
            detail=f"创建插件失败: {str(e)}"
        )


@router.get("/", response_model=List[PluginInfo])
async def list_plugins():
    """列出所有已加载的插件"""
    manager = get_plugin_manager()
    plugins = manager.list_plugins()
    return [PluginInfo(**p) for p in plugins]


@router.get("/{name}", response_model=PluginInfo)
async def get_plugin(name: str):
    """获取单个插件信息"""
    manager = get_plugin_manager()
    plugin = manager.get_plugin(name)
    if not plugin:
        raise HTTPException(status_code=404, detail=f"插件 {name} 未找到")
    
    plugins = manager.list_plugins()
    for p in plugins:
        if p["name"] == name:
            return PluginInfo(**p)
    
    raise HTTPException(status_code=404, detail=f"插件 {name} 未找到")


@router.post("/{name}/enable", response_model=PluginResponse)
async def enable_plugin(name: str):
    """启用插件"""
    manager = get_plugin_manager()
    
    if not manager.get_plugin(name):
        raise HTTPException(status_code=404, detail=f"插件 {name} 未找到")
    
    success = manager.enable_plugin(name)
    if success:
        plugin = manager.get_plugin(name)
        plugins = manager.list_plugins()
        plugin_info = None
        for p in plugins:
            if p["name"] == name:
                plugin_info = PluginInfo(**p)
                break
        
        return PluginResponse(
            success=True,
            message=f"插件 {name} 已启用",
            plugin=plugin_info
        )
    else:
        return PluginResponse(
            success=False,
            message=f"启用插件 {name} 失败"
        )


@router.post("/{name}/disable", response_model=PluginResponse)
async def disable_plugin(name: str):
    """禁用插件"""
    manager = get_plugin_manager()
    
    if not manager.get_plugin(name):
        raise HTTPException(status_code=404, detail=f"插件 {name} 未找到")
    
    success = manager.disable_plugin(name)
    if success:
        plugin = manager.get_plugin(name)
        plugins = manager.list_plugins()
        plugin_info = None
        for p in plugins:
            if p["name"] == name:
                plugin_info = PluginInfo(**p)
                break
        
        return PluginResponse(
            success=True,
            message=f"插件 {name} 已禁用",
            plugin=plugin_info
        )
    else:
        return PluginResponse(
            success=False,
            message=f"禁用插件 {name} 失败"
        )


@router.post("/{name}/toggle", response_model=PluginResponse)
async def toggle_plugin(name: str, request: PluginToggleRequest):
    """切换插件启用/禁用状态"""
    manager = get_plugin_manager()
    
    if not manager.get_plugin(name):
        raise HTTPException(status_code=404, detail=f"插件 {name} 未找到")
    
    if request.enabled:
        success = manager.enable_plugin(name)
        action = "启用"
    else:
        success = manager.disable_plugin(name)
        action = "禁用"
    
    if success:
        plugins = manager.list_plugins()
        plugin_info = None
        for p in plugins:
            if p["name"] == name:
                plugin_info = PluginInfo(**p)
                break
        
        return PluginResponse(
            success=True,
            message=f"插件 {name} 已{action}",
            plugin=plugin_info
        )
    else:
        return PluginResponse(
            success=False,
            message=f"{action}插件 {name} 失败"
        )


@router.post("/{name}/reload", response_model=PluginResponse)
async def reload_plugin(name: str):
    """重新加载插件"""
    manager = get_plugin_manager()
    
    if not manager.get_plugin(name):
        raise HTTPException(status_code=404, detail=f"插件 {name} 未找到")
    
    # 先卸载再加载
    success = manager.unload_plugin(name)
    if not success:
        return PluginResponse(
            success=False,
            message=f"卸载插件 {name} 失败"
        )
    
    success = manager.load_plugin(name)
    if success:
        plugins = manager.list_plugins()
        plugin_info = None
        for p in plugins:
            if p["name"] == name:
                plugin_info = PluginInfo(**p)
                break
        
        return PluginResponse(
            success=True,
            message=f"插件 {name} 已重新加载",
            plugin=plugin_info
        )
    else:
        return PluginResponse(
            success=False,
            message=f"重新加载插件 {name} 失败"
        )


@router.post("/reload-all", response_model=PluginResponse)
async def reload_all_plugins():
    """重新加载所有插件"""
    manager = get_plugin_manager()
    
    # 卸载所有插件
    for name in list(manager._plugins.keys()):
        manager.unload_plugin(name)
    
    # 重新加载
    count = manager.load_all_plugins()
    
    return PluginResponse(
        success=True,
        message=f"已重新加载 {count} 个插件"
    )


@router.get("/{name}/config")
async def get_plugin_config(name: str):
    """获取插件配置"""
    manager = get_plugin_manager()
    plugin = manager.get_plugin(name)
    
    if not plugin:
        raise HTTPException(status_code=404, detail=f"插件 {name} 未找到")
    
    return {
        "name": name,
        "config": plugin.config
    }


@router.post("/{name}/config")
async def update_plugin_config(name: str, request: PluginConfigRequest):
    """更新插件配置"""
    manager = get_plugin_manager()
    plugin = manager.get_plugin(name)
    
    if not plugin:
        raise HTTPException(status_code=404, detail=f"插件 {name} 未找到")
    
    try:
        plugin.config = request.config
        plugin.save_config()
        
        return {
            "success": True,
            "message": f"插件 {name} 配置已更新"
        }
    except Exception as e:
        logger.error(f"更新插件 {name} 配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"更新配置失败: {str(e)}")
