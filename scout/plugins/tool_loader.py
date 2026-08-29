"""动态工具加载器 — 支持运行时加载第三方工具和技能包."""

import importlib
import sys
import logging
from pathlib import Path
from typing import Dict, List, Optional, Type

from scout.tools.base import BaseTool

logger = logging.getLogger(__name__)


class DynamicToolLoader:
    """动态加载第三方工具包."""
    
    def __init__(self, tools_dir: Path):
        self.tools_dir = tools_dir
        self._loaded_tools: Dict[str, Type[BaseTool]] = {}
    
    def scan_directory(self) -> List[str]:
        """扫描目录发现可加载的工具包."""
        discovered = []
        if not self.tools_dir.exists():
            return discovered
        
        for pkg_dir in self.tools_dir.iterdir():
            if pkg_dir.is_dir() and (pkg_dir / "__init__.py").exists():
                # 检查是否有 Tool 类
                init_file = pkg_dir / "__init__.py"
                content = init_file.read_text()
                if "BaseTool" in content or "Tool" in content:
                    discovered.append(pkg_dir.name)
        
        return discovered
    
    def load_tool(self, tool_name: str) -> Optional[BaseTool]:
        """加载单个工具."""
        if tool_name in self._loaded_tools:
            return self._loaded_tools[tool_name]()
        
        tool_path = self.tools_dir / tool_name
        if not tool_path.exists():
            logger.warning(f"工具包不存在: {tool_name}")
            return None
        
        # 添加到 sys.path 以便导入
        sys.path.insert(0, str(self.tools_dir))
        
        try:
            module = importlib.import_module(f"{tool_name}")
            # 寻找 BaseTool 子类
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type) and 
                    issubclass(attr, BaseTool) and 
                    attr != BaseTool):
                    self._loaded_tools[tool_name] = attr
                    logger.info(f"成功加载工具: {tool_name}")
                    return attr()
        except Exception as e:
            logger.error(f"加载工具 {tool_name} 失败: {e}")
        finally:
            sys.path.pop(0)
        
        return None
    
    def load_all(self) -> List[BaseTool]:
        """加载所有发现的工具."""
        tools = []
        for name in self.scan_directory():
            tool = self.load_tool(name)
            if tool:
                tools.append(tool)
        return tools
