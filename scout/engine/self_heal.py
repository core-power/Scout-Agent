"""自修复与自动校验模块 — Scout Agent 的工程化闭环.

借鉴 Herness/Harness Agent 的自动化反馈机制，通过静态检查和单元测试
构建“执行-校验-修复”的闭环。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("scout.self_heal")


@dataclass
class VerificationResult:
    """校验结果."""
    success: bool = True
    stdout: str = ""
    stderr: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def summary(self) -> str:
        """生成给 LLM 看的简洁错误摘要."""
        if self.success:
            return "校验通过"
        
        lines = []
        if self.errors:
            for err in self.errors[:5]:  # 最多取前 5 个错误
                loc = f"{err.get('file', 'unknown')}:{err.get('line', '?')}"
                msg = err.get('message', 'Unknown error')
                code = err.get('code', '')
                lines.append(f"- [{loc}] {code}: {msg}")
        elif self.stderr:
            lines.append(f"Runtime Error:\n{self.stderr.strip()}")
        
        return "\n".join(lines) if lines else "未知校验失败"


class VerifyPipeline:
    """自动校验管道 — 负责运行 ruff 和 pytest."""

    def __init__(self, work_dir: str = "."):
        self.work_dir = Path(work_dir).resolve()

    async def run_ruff(self, target: str) -> VerificationResult:
        """运行 ruff 进行静态检查."""
        start_time = asyncio.get_event_loop().time()
        try:
            proc = await asyncio.create_subprocess_exec(
                "ruff", "check", target, "--output-format=json",
                cwd=self.work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            
            result = VerificationResult(
                success=(proc.returncode == 0),
                stdout=stdout.decode(),
                stderr=stderr.decode(),
                duration_ms=int((asyncio.get_event_loop().time() - start_time) * 1000),
            )

            if not result.success and result.stdout:
                import json
                try:
                    issues = json.loads(result.stdout)
                    for issue in issues:
                        result.errors.append({
                            "file": issue.get("filename", ""),
                            "line": issue.get("location", {}).get("row", 0),
                            "column": issue.get("location", {}).get("column", 0),
                            "code": issue.get("code", ""),
                            "message": issue.get("message", ""),
                        })
                except Exception:
                    pass
            
            return result
        except FileNotFoundError:
            return VerificationResult(success=True, stdout="ruff not installed, skipping.")
        except Exception as e:
            return VerificationResult(success=False, stderr=str(e))

    async def run_pytest(self, target: str) -> VerificationResult:
        """运行 pytest 进行单元测试."""
        start_time = asyncio.get_event_loop().time()
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", "-m", "pytest", target, "-v", "--tb=short",
                cwd=self.work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            
            result = VerificationResult(
                success=(proc.returncode == 0),
                stdout=stdout.decode(),
                stderr=stderr.decode(),
                duration_ms=int((asyncio.get_event_loop().time() - start_time) * 1000),
            )

            if not result.success:
                # 简单解析 pytest 输出中的错误位置
                error_pattern = re.compile(r"E\s+.*?File \"(.*?)\", line (\d+).*?\n(.*?)(?:\nE|$)", re.DOTALL)
                for match in error_pattern.finditer(result.stdout + result.stderr):
                    result.errors.append({
                        "file": match.group(1),
                        "line": int(match.group(2)),
                        "message": match.group(3).strip(),
                    })
            
            return result
        except Exception as e:
            return VerificationResult(success=False, stderr=str(e))

    async def verify_code(self, file_path: str, run_tests: bool = True) -> VerificationResult:
        """执行完整的校验流程."""
        path = Path(file_path)
        if not path.exists():
            return VerificationResult(success=False, stderr=f"File not found: {file_path}")

        # 1. 静态检查
        lint_result = await self.run_ruff(str(path))
        if not lint_result.success:
            return lint_result

        # 2. 单元测试 (可选)
        if run_tests:
            test_path = path.with_name(f"test_{path.name}")
            if test_path.exists():
                test_result = await self.run_pytest(str(test_path))
                if not test_result.success:
                    return test_result
        
        return VerificationResult(success=True)
