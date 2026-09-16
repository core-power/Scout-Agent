"""发送文件工具 — 将文件推送给前端供用户下载."""

from __future__ import annotations

import os

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry


class SendFileTool(ToolDefinition):
    """将文件发送给用户 — 前端会显示下载按钮."""

    name = "send_file"
    description = (
        "将指定路径的文件发送给用户，前端会显示可下载的附件卡片。"
        "【重要】仅当用户明确要求发送文件时使用此工具，例如用户直接说出/暗示"
        "'发文件给我'、'下载这个文件'、'把文件发给我'、'导出一个文件'等明确诉求。"
        "如果用户只是让你回答内容、整理信息、总结要点、写代码或解释——"
        "请直接在对话中用文本回复，不要生成文件发送。"
        "切勿在用户未提出文件诉求时主动产出并发送文件。"
        "支持任意文件类型（docx、pdf、txt、xlsx 等）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要发送的文件路径（绝对路径或相对路径）",
            },
        },
        "required": ["path"],
    }
    annotations = ToolAnnotations(read_only=True)

    async def execute(self, path: str) -> Observation:
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return Observation(
                tool_name="send_file",
                success=False,
                output=f"文件不存在: {path}",
            )
        if os.path.isdir(path):
            return Observation(
                tool_name="send_file",
                success=False,
                output=f"路径是目录，不是文件: {path}",
            )

        file_size = os.path.getsize(path)
        file_name = os.path.basename(path)

        return Observation(
            tool_name="send_file",
            success=True,
            output=f"已发送文件: {file_name} ({file_size} bytes)",
            metadata={
                "path": path,
                "downloadable": True,
                "file_name": file_name,
                "file_size": file_size,
                "is_image": file_name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico")),
            },
        )


# import 时自动注册
ToolRegistry.register(SendFileTool())
