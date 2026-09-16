"""测试配置和通用 fixtures."""

import pytest
import sys
from pathlib import Path


# 添加项目根目录到 Python 路径
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))


@pytest.fixture(autouse=True)
def _isolate_legacy_restore(monkeypatch):
    """禁用旧数据目录配置自愈（2026-09-01）.

    避免单测在 api_key 为空时读取真实历史数据目录（如 D:\\.scout）,
    导致测试间数据串扰与断言不稳定。
    """
    monkeypatch.setenv("SCOUT_DISABLE_LEGACY_RESTORE", "1")


@pytest.fixture
def temp_dir(tmp_path):
    """创建临时目录."""
    return tmp_path


@pytest.fixture
def sample_python_file(tmp_path):
    """创建示例 Python 文件."""
    file_path = tmp_path / "sample.py"
    content = '''"""示例文件."""


def hello():
    """打招呼."""
    print("Hello, World!")


def add(a, b):
    """加法."""
    return a + b


if __name__ == "__main__":
    hello()
'''
    file_path.write_text(content, encoding="utf-8")
    return file_path


@pytest.fixture
def sample_json_file(tmp_path):
    """创建示例 JSON 文件."""
    file_path = tmp_path / "config.json"
    content = '''{
  "name": "test",
  "version": "1.0.0",
  "settings": {
    "debug": true,
    "timeout": 30
  }
}
'''
    file_path.write_text(content, encoding="utf-8")
    return file_path
