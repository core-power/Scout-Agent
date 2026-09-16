"""工具系统测试."""

import pytest
from pathlib import Path


class TestEditTools:
    """统一文件工具测试（原 FileReadTool/FileWriteTool 等已合并为 UnifiedFileTool）."""

    @pytest.mark.unit
    async def test_file_read_basic(self, sample_python_file):
        """测试基础读取."""
        from scout.tools.builtin.files.unified import UnifiedFileTool

        tool = UnifiedFileTool()
        result = await tool.execute(action="read", path=str(sample_python_file))

        assert result.success
        assert "示例文件" in result.output
        assert "def hello():" in result.output

    @pytest.mark.unit
    async def test_file_read_range(self, sample_python_file):
        """测试行范围读取."""
        from scout.tools.builtin.files.unified import UnifiedFileTool

        tool = UnifiedFileTool()
        result = await tool.execute(
            action="read",
            path=str(sample_python_file),
            start_line=4,
            end_line=10
        )

        assert result.success
        assert "def hello():" in result.output
        assert "def add(" in result.output

    @pytest.mark.unit
    async def test_file_read_nonexistent(self, tmp_path):
        """测试读取不存在的文件."""
        from scout.tools.builtin.files.unified import UnifiedFileTool

        tool = UnifiedFileTool()
        result = await tool.execute(action="read", path=str(tmp_path / "nonexistent.py"))

        assert not result.success
        assert "文件不存在" in result.output

    @pytest.mark.unit
    async def test_file_write_basic(self, tmp_path):
        """测试写入文件."""
        from scout.tools.builtin.files.unified import UnifiedFileTool

        tool = UnifiedFileTool()
        target = tmp_path / "write_test.txt"
        result = await tool.execute(
            action="write",
            path=str(target),
            content="第一行\n第二行\n"
        )

        assert result.success
        assert target.exists()
        assert "第一行" in target.read_text(encoding="utf-8")

    @pytest.mark.unit
    async def test_file_replace_basic(self, sample_python_file):
        """测试基础替换."""
        from scout.tools.builtin.files.unified import UnifiedFileTool

        tool = UnifiedFileTool()
        result = await tool.execute(
            action="replace",
            path=str(sample_python_file),
            old_text='print("Hello, World!")',
            new_text='print("Hello, Scout!")'
        )

        assert result.success

        # 验证修改
        content = sample_python_file.read_text(encoding="utf-8")
        assert 'print("Hello, Scout!")' in content
        assert 'print("Hello, World!")' not in content

    @pytest.mark.unit
    async def test_file_replace_not_found(self, sample_python_file):
        """测试替换不存在的文本."""
        from scout.tools.builtin.files.unified import UnifiedFileTool

        tool = UnifiedFileTool()
        result = await tool.execute(
            action="replace",
            path=str(sample_python_file),
            old_text='print("Not Found")',
            new_text='print("New")'
        )

        assert not result.success

    @pytest.mark.unit
    async def test_file_insert(self, sample_python_file):
        """测试插入内容."""
        from scout.tools.builtin.files.unified import UnifiedFileTool

        tool = UnifiedFileTool()
        result = await tool.execute(
            action="insert",
            path=str(sample_python_file),
            line=5,
            content="\n# 新增注释\n"
        )

        assert result.success

        # 验证插入
        lines = sample_python_file.read_text(encoding="utf-8").splitlines()
        assert "# 新增注释" in lines

    @pytest.mark.unit
    async def test_file_delete(self, sample_python_file):
        """测试删除行."""
        from scout.tools.builtin.files.unified import UnifiedFileTool

        # 获取原始行数
        original_lines = len(sample_python_file.read_text(encoding="utf-8").splitlines())

        tool = UnifiedFileTool()
        result = await tool.execute(
            action="delete",
            path=str(sample_python_file),
            start_line=1,
            end_line=3
        )

        assert result.success

        # 验证删除
        new_lines = len(sample_python_file.read_text(encoding="utf-8").splitlines())
        assert new_lines == original_lines - 3

    @pytest.mark.unit
    async def test_file_edit_patch(self, sample_python_file):
        """测试 patch 模式编辑."""
        from scout.tools.builtin.files.unified import UnifiedFileTool

        patch = '''<<<<<<< SEARCH
def hello():
    """打招呼."""
    print("Hello, World!")
=======
def hello():
    """打招呼."""
    print("Hello, Scout!")
>>>>>>> REPLACE
'''

        tool = UnifiedFileTool()
        result = await tool.execute(
            action="edit",
            path=str(sample_python_file),
            patch=patch
        )

        assert result.success

        # 验证修改
        content = sample_python_file.read_text(encoding="utf-8")
        assert 'print("Hello, Scout!")' in content


class TestEnvConfigTools:
    """环境配置工具测试."""
    
    @pytest.mark.unit
    async def test_env_save_and_get(self, tmp_path, monkeypatch):
        """测试保存和获取配置."""
        # 设置临时配置目录
        config_dir = tmp_path / "config"
        monkeypatch.setattr(
            "scout.tools.builtin.env_config.CONFIG_DIR",
            config_dir
        )
        monkeypatch.setattr(
            "scout.tools.builtin.env_config.CONFIG_FILE",
            config_dir / "env_secrets.json"
        )
        
        from scout.tools.builtin.env_config import EnvConfigSaveTool, EnvConfigGetTool
        
        # 保存配置
        save_tool = EnvConfigSaveTool()
        result = await save_tool.execute(
            key="TEST_API_KEY",
            value="sk-1234567890abcdef",
            description="测试 API Key"
        )
        assert result.success
        
        # 获取配置
        get_tool = EnvConfigGetTool()
        result = await get_tool.execute(key="TEST_API_KEY")
        assert result.success
        assert "sk-1" in result.output  # 脱敏显示
        assert "90abcdef" in result.output
    
    @pytest.mark.unit
    async def test_env_list(self, tmp_path, monkeypatch):
        """测试列出配置."""
        config_dir = tmp_path / "config"
        monkeypatch.setattr(
            "scout.tools.builtin.env_config.CONFIG_DIR",
            config_dir
        )
        monkeypatch.setattr(
            "scout.tools.builtin.env_config.CONFIG_FILE",
            config_dir / "env_secrets.json"
        )
        
        from scout.tools.builtin.env_config import (
            EnvConfigSaveTool,
            EnvConfigListTool
        )
        
        # 保存多个配置
        save_tool = EnvConfigSaveTool()
        await save_tool.execute(key="KEY1", value="value1", description="配置1")
        await save_tool.execute(key="KEY2", value="value2", description="配置2")
        
        # 列出配置
        list_tool = EnvConfigListTool()
        result = await list_tool.execute()
        
        assert result.success
        assert "KEY1" in result.output
        assert "KEY2" in result.output
        assert "配置1" in result.output
        assert "配置2" in result.output
    
    @pytest.mark.unit
    async def test_env_delete(self, tmp_path, monkeypatch):
        """测试删除配置."""
        config_dir = tmp_path / "config"
        monkeypatch.setattr(
            "scout.tools.builtin.env_config.CONFIG_DIR",
            config_dir
        )
        monkeypatch.setattr(
            "scout.tools.builtin.env_config.CONFIG_FILE",
            config_dir / "env_secrets.json"
        )
        
        from scout.tools.builtin.env_config import (
            EnvConfigSaveTool,
            EnvConfigDeleteTool,
            EnvConfigGetTool
        )
        
        # 保存配置
        save_tool = EnvConfigSaveTool()
        await save_tool.execute(key="TO_DELETE", value="value")
        
        # 删除配置
        delete_tool = EnvConfigDeleteTool()
        result = await delete_tool.execute(key="TO_DELETE")
        assert result.success
        
        # 验证删除
        get_tool = EnvConfigGetTool()
        result = await get_tool.execute(key="TO_DELETE")
        assert not result.success
        assert "配置不存在" in result.output
