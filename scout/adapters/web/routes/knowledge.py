"""知识库路由组（/api/knowledge：目录/上传/搜索/删除）.

W3 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from fastapi.responses import JSONResponse, Response
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class KnowledgeRoutes:
    """知识库路由组（/api/knowledge：目录/上传/搜索/删除）（mixin）."""

    def _setup_knowledge_routes(self):
        """知识库 API."""

        # ── 知识库 API ──

        @self.app.get("/api/knowledge")
        async def list_knowledge():
            """列出所有知识页面."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR
            pages = []
            if KNOWLEDGE_DIR.exists():
                for md_file in sorted(KNOWLEDGE_DIR.rglob("*.md")):
                    if md_file.name == "index.md":
                        continue
                    rel_path = str(md_file.relative_to(KNOWLEDGE_DIR))
                    try:
                        content = md_file.read_text(encoding="utf-8")
                        # 提取标题
                        title = md_file.stem.replace("-", " ").title()
                        for line in content.split("\n"):
                            if line.startswith("# "):
                                title = line[2:].strip()
                                break
                        # 提取摘要（跳过 YAML front matter）
                        summary = ""
                        in_frontmatter = False
                        for line in content.split("\n"):
                            stripped = line.strip()
                            if stripped == "---":
                                in_frontmatter = not in_frontmatter
                                continue
                            if in_frontmatter:
                                continue
                            if stripped and not stripped.startswith("#") and not stripped.startswith(">") and not stripped.startswith("```") and not stripped.startswith("|"):
                                summary = stripped[:100]
                                break
                        stat = md_file.stat()
                        pages.append({
                            "path": rel_path,
                            "title": title,
                            "summary": summary,
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        })
                    except Exception:
                        continue
            return {"pages": pages}

        @self.app.get("/api/knowledge/{path:path}")
        async def read_knowledge(path: str):
            """读取知识页面内容."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR
            full_path = (KNOWLEDGE_DIR / path).resolve()
            if not str(full_path).startswith(str(KNOWLEDGE_DIR.resolve())):
                return JSONResponse({"error": "路径不合法"}, status_code=400)
            if not full_path.exists():
                return JSONResponse({"error": f"页面不存在: {path}"}, status_code=404)
            content = full_path.read_text(encoding="utf-8")
            return {"path": path, "content": content}

        @self.app.post("/api/knowledge")
        async def save_knowledge(req: dict):
            """保存知识页面."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR
            import os
            path = req.get("path", "")
            content = req.get("content", "")
            if not path or not content:
                return JSONResponse({"error": "path 和 content 不能为空"}, status_code=400)
            
            full_path = (KNOWLEDGE_DIR / path).resolve()
            if not str(full_path).startswith(str(KNOWLEDGE_DIR.resolve())):
                return JSONResponse({"error": "路径不合法"}, status_code=400)
            
            full_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = full_path.with_suffix(".tmp")
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, full_path)
            
            # 更新索引
            index_path = KNOWLEDGE_DIR / "index.md"
            title = Path(path).stem.replace("-", " ").title()
            for line in content.split("\n"):
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            entry = f"- [{title}]({path})"
            if index_path.exists():
                idx_content = index_path.read_text(encoding="utf-8")
                if path not in idx_content:
                    idx_content = idx_content.rstrip() + f"\n{entry}\n"
                    tmp = index_path.with_suffix(".tmp")
                    tmp.write_text(idx_content, encoding="utf-8")
                    os.replace(tmp, index_path)
            else:
                tmp = index_path.with_suffix(".tmp")
                tmp.write_text(f"# 知识库索引\n\n{entry}\n", encoding="utf-8")
                os.replace(tmp, index_path)
            
            return {"status": "ok", "path": path}

        @self.app.delete("/api/knowledge/{path:path}")
        async def delete_knowledge(path: str):
            """删除知识页面."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR
            import os
            full_path = (KNOWLEDGE_DIR / path).resolve()
            if not str(full_path).startswith(str(KNOWLEDGE_DIR.resolve())):
                return JSONResponse({"error": "路径不合法"}, status_code=400)
            if full_path.exists():
                full_path.unlink()
            return {"status": "ok"}

        @self.app.get("/api/knowledge/search")
        async def search_knowledge(q: str = "", limit: int = 20):
            """搜索知识库."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR, _global_index
            if not q:
                return {"results": []}
            results = _global_index.search(q, KNOWLEDGE_DIR)
            return {"results": [
                {"path": path, "score": score, "summary": summary}
                for path, score, summary in results[:limit]
            ]}

        @self.app.post("/api/knowledge/upload")
        async def upload_knowledge(req: Request):
            """上传文件并解析为知识页面."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR
            from scout.tools.builtin.knowledge.parser import DocumentParser
            import os
            import tempfile

            # 获取上传的文件
            form = await req.form()
            file = form.get("file")
            target_path = form.get("path", "")
            
            if not file:
                return JSONResponse({"error": "没有上传文件"}, status_code=400)

            # 保存到临时文件
            filename = file.filename or "upload"
            suffix = Path(filename).suffix
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    content = await file.read()
                    tmp.write(content)
                    tmp_path = Path(tmp.name)

                # 解析文档
                result = DocumentParser.parse(tmp_path)
                
                # 确定目标路径
                if not target_path:
                    stem = Path(filename).stem
                    # 清理文件名
                    stem = re.sub(r'[^\w\u4e00-\u9fff-]', '-', stem)
                    stem = re.sub(r'-+', '-', stem).strip('-').lower()
                    target_path = f"uploads/{stem}.md"
                
                # 确保 .md 后缀
                if not target_path.endswith(".md"):
                    target_path += ".md"

                # 保存到知识库
                full_path = (KNOWLEDGE_DIR / target_path).resolve()
                if not str(full_path).startswith(str(KNOWLEDGE_DIR.resolve())):
                    return JSONResponse({"error": "路径不合法"}, status_code=400)
                
                full_path.parent.mkdir(parents=True, exist_ok=True)
                
                # 添加元数据头
                parsed_content = result["content"]
                header = f"---\nsource: {filename}\nformat: {result['format']}\nsize: {result['size']}\nparsed_at: {result['parsed_at']}\n---\n\n"
                final_content = header + parsed_content
                
                tmp_out = full_path.with_suffix(".tmp")
                tmp_out.write_text(final_content, encoding="utf-8")
                os.replace(tmp_out, full_path)

                # 更新索引
                index_path = KNOWLEDGE_DIR / "index.md"
                entry = f"- [{result['title']}]({target_path})"
                if index_path.exists():
                    idx = index_path.read_text(encoding="utf-8")
                    if target_path not in idx:
                        idx = idx.rstrip() + f"\n{entry}\n"
                        tmp_idx = index_path.with_suffix(".tmp")
                        tmp_idx.write_text(idx, encoding="utf-8")
                        os.replace(tmp_idx, index_path)
                else:
                    tmp_idx = index_path.with_suffix(".tmp")
                    tmp_idx.write_text(f"# 知识库索引\n\n{entry}\n", encoding="utf-8")
                    os.replace(tmp_idx, index_path)

                return {
                    "status": "ok",
                    "path": target_path,
                    "title": result["title"],
                    "format": result["format"],
                    "content": parsed_content,
                    "meta": result["meta"],
                }
            except Exception as e:
                return JSONResponse({"error": f"解析失败: {e}"}, status_code=500)
            finally:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
