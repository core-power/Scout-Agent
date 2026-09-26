# -*- coding: utf-8 -*-
"""A2A JSON-RPC 2.0（Google A2A 规范）协议测试.

覆盖 scout/a2a 服务端规范端点（httpx.AsyncClient + ASGITransport 直连 app）：
1. message/send 正常流（含 text/data part、artifacts）
2. 任务生命周期（tasks/get 取回、tasks/cancel、终态不可取消）
3. 协议错误（-32700 / -32600 / -32601 / -32602 / -32001 / -32002）
4. agent-card.json 规范卡片路径 + agent.json 旧路径兼容
5. 旧自定义 REST 端点向后兼容（/a2a/tasks/send）
6. A2AClient JSON-RPC 模式（MockTransport，不触网）

app 构造方式：A2aRoutes 是 WebAdapter 的 mixin，仅需 self.app / self._agent
两个属性即可完成路由挂载，故用 __new__ 构造最小实例，不拉起完整 WebAdapter
（避免配置加载 / agent 重建等副作用），与 test_web_auth.py 的最小化思路一致。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from scout.adapters.web.routes.a2a import A2aRoutes
from scout.a2a.client import A2AClient, A2ARemoteError
from scout.a2a.jsonrpc import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    TASK_NOT_CANCELABLE,
    TASK_NOT_FOUND,
)
from scout.a2a.server import A2AServer
from scout.a2a.types import Task, TaskStatus


# ── 测试替身 ────────────────────────────────────────────────

class FakeAgent:
    """最小 Agent 替身：A2AServer 只依赖 run_conversation."""

    def __init__(self, prefix="echo:", artifacts=None, fail=False):
        self.prefix = prefix
        self.artifacts = artifacts or []
        self.fail = fail
        self.received = []

    async def run_conversation(self, message, session):
        self.received.append(message)
        if self.fail:
            raise RuntimeError("boom")
        result: dict[str, Any] = {"response": f"{self.prefix} {message}"}
        if self.artifacts:
            result["artifacts"] = self.artifacts
        return result


def make_app(agent) -> tuple[FastAPI, A2AServer]:
    """构造仅挂载 A2A 路由组的最小 FastAPI app."""
    app = FastAPI()
    routes = A2aRoutes.__new__(A2aRoutes)
    routes.app = app
    routes._agent = agent
    routes._setup_a2a_routes()
    return app, routes._a2a_server


def make_client(agent) -> httpx.AsyncClient:
    app, _ = make_app(agent)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def rpc_request(method: str, params: dict | None = None, rpc_id: Any = 1) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}


# ── Agent Card ─────────────────────────────────────────────

async def test_agent_card_spec_path():
    async with make_client(FakeAgent()) as client:
        resp = await client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "Scout Agent"
    assert card["url"].endswith("/a2a")
    assert "capabilities" in card


async def test_agent_card_legacy_path_compat():
    """旧路径 /.well-known/agent.json 保留（deprecated），旧客户端不破坏."""
    async with make_client(FakeAgent()) as client:
        resp = await client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Scout Agent"


# ── message/send 正常流 ────────────────────────────────────

async def test_message_send_happy_path():
    agent = FakeAgent(prefix="ok:")
    async with make_client(agent) as client:
        resp = await client.post("/a2a", json=rpc_request(
            "message/send",
            {
                "message": {
                    "role": "user",
                    "parts": [
                        {"kind": "text", "text": "hello scout"},
                        {"kind": "data", "data": {"lang": "zh"}},
                    ],
                    "messageId": "m-1",
                    "kind": "message",
                }
            },
            rpc_id=42,
        ))
    assert resp.status_code == 200
    body = resp.json()
    # JSON-RPC envelope
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 42
    assert "error" not in body
    task = body["result"]
    assert task["kind"] == "task"
    assert task["id"]
    # 任务完成，结论在 status.message 与 history 中
    assert task["status"]["state"] == "completed"
    assert task["status"]["message"]["parts"][0]["text"] == "ok: hello scout"
    history = task["history"]
    assert history[0]["role"] == "user"
    assert history[0]["parts"][0] == {"kind": "text", "text": "hello scout"}
    assert history[-1]["role"] == "agent"
    # data part 也被接受（不报错）
    assert agent.received == ["hello scout"]


async def test_message_send_failed_task():
    agent = FakeAgent(fail=True)
    async with make_client(agent) as client:
        resp = await client.post("/a2a", json=rpc_request(
            "message/send",
            {"message": {"role": "user", "parts": [{"kind": "text", "text": "go"}]}},
        ))
    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"
    assert "boom" in task["status"]["message"]["parts"][0]["text"]


# ── artifacts（轻量版） ────────────────────────────────────

async def test_artifacts_returned_and_retrievable():
    agent = FakeAgent(artifacts=[
        {
            "name": "report",
            "parts": [{"kind": "text", "text": "artifact-content"}],
        },
        {
            "name": "table",
            "parts": [{"kind": "data", "data": {"rows": 3}}],
        },
    ])
    async with make_client(agent) as client:
        send = await client.post("/a2a", json=rpc_request(
            "message/send",
            {"message": {"role": "user", "parts": [{"kind": "text", "text": "make report"}]}},
        ))
        task_id = send.json()["result"]["id"]

        # tasks/get 可取回（artifacts 持久保存在 server 内存）
        got = await client.post("/a2a", json=rpc_request("tasks/get", {"taskId": task_id}))

    artifacts = got.json()["result"]["artifacts"]
    assert len(artifacts) == 2
    assert artifacts[0]["name"] == "report"
    assert artifacts[0]["artifactId"]
    assert artifacts[0]["parts"][0] == {"kind": "text", "text": "artifact-content"}
    assert artifacts[1]["parts"][0] == {"kind": "data", "data": {"rows": 3}}


# ── 任务生命周期：tasks/get / tasks/cancel ─────────────────

async def test_tasks_get_unknown_task():
    async with make_client(FakeAgent()) as client:
        resp = await client.post("/a2a", json=rpc_request(
            "tasks/get", {"taskId": "no-such-task"}, rpc_id=7,
        ))
    body = resp.json()
    assert body["id"] == 7
    assert body["error"]["code"] == TASK_NOT_FOUND


async def test_tasks_cancel_working_task():
    agent = FakeAgent()
    app, server = make_app(agent)
    # 直接注入一个 working 状态的任务（任务在 message/send 内同步完成，
    # 正常流不会留下 working 任务）
    server.tasks["t-work"] = Task(id="t-work", status=TaskStatus(state="working"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post("/a2a", json=rpc_request(
            "tasks/cancel", {"taskId": "t-work"},
        ))
    task = resp.json()["result"]
    assert task["status"]["state"] == "canceled"
    assert server.tasks["t-work"].status.state == "canceled"


async def test_tasks_cancel_terminal_task_rejected():
    agent = FakeAgent()
    async with make_client(agent) as client:
        send = await client.post("/a2a", json=rpc_request(
            "message/send",
            {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
        ))
        task_id = send.json()["result"]["id"]
        resp = await client.post("/a2a", json=rpc_request(
            "tasks/cancel", {"taskId": task_id},
        ))
    assert resp.json()["error"]["code"] == TASK_NOT_CANCELABLE


async def test_tasks_cancel_unknown_task():
    async with make_client(FakeAgent()) as client:
        resp = await client.post("/a2a", json=rpc_request(
            "tasks/cancel", {"taskId": "ghost"},
        ))
    assert resp.json()["error"]["code"] == TASK_NOT_FOUND


# ── 协议错误码 ─────────────────────────────────────────────

async def test_unknown_method_returns_method_not_found():
    async with make_client(FakeAgent()) as client:
        resp = await client.post("/a2a", json=rpc_request("foo/bar", {}, rpc_id="abc"))
    body = resp.json()
    assert resp.status_code == 200  # JSON-RPC over HTTP：协议错误仍 200
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == "abc"
    assert body["error"]["code"] == METHOD_NOT_FOUND


async def test_parse_error():
    async with make_client(FakeAgent()) as client:
        resp = await client.post(
            "/a2a", content=b"this is not json", headers={"Content-Type": "application/json"}
        )
    body = resp.json()
    assert resp.status_code == 200
    assert body["id"] is None
    assert body["error"]["code"] == PARSE_ERROR


async def test_invalid_request_missing_jsonrpc_version():
    async with make_client(FakeAgent()) as client:
        resp = await client.post("/a2a", json={"id": 1, "method": "tasks/get"})
    assert resp.json()["error"]["code"] == INVALID_REQUEST


async def test_invalid_params_missing_message():
    async with make_client(FakeAgent()) as client:
        resp = await client.post("/a2a", json=rpc_request("message/send", {}))
    assert resp.json()["error"]["code"] == INVALID_PARAMS


async def test_invalid_params_bad_message_shape():
    async with make_client(FakeAgent()) as client:
        resp = await client.post("/a2a", json=rpc_request(
            "message/send", {"message": {"role": "user", "parts": [{"kind": "bogus"}]}},
        ))
    assert resp.json()["error"]["code"] == INVALID_PARAMS


async def test_invalid_params_no_text_in_message():
    async with make_client(FakeAgent()) as client:
        resp = await client.post("/a2a", json=rpc_request(
            "message/send",
            {"message": {"role": "user", "parts": [{"kind": "data", "data": {"a": 1}}]}},
        ))
    assert resp.json()["error"]["code"] == INVALID_PARAMS


async def test_streaming_unsupported():
    """streaming/push notification 明确不支持（TODO），返回 -32003."""
    async with make_client(FakeAgent()) as client:
        resp = await client.post("/a2a", json=rpc_request(
            "message/stream",
            {"message": {"role": "user", "parts": [{"kind": "text", "text": "x"}]}},
        ))
    assert resp.json()["error"]["code"] == -32003


# ── 旧自定义 REST 端点兼容（deprecated） ──────────────────

async def test_legacy_rest_send_task_still_works():
    agent = FakeAgent(prefix="legacy:")
    async with make_client(agent) as client:
        resp = await client.post("/a2a/tasks/send", json={
            "task": {
                "id": "legacy-1",
                "messages": [
                    {"role": "user", "parts": [{"type": "text", "text": "old client"}]}
                ],
            }
        })
        assert resp.status_code == 200
        task = resp.json()["task"]
        assert task["status"]["state"] == "completed"
        assert task["messages"][-1]["parts"][0]["text"] == "legacy: old client"

        # 旧 GET 端点也能取回同一任务
        resp = await client.get("/a2a/tasks/legacy-1")
        assert resp.status_code == 200
        assert resp.json()["id"] == "legacy-1"


# ── A2AClient JSON-RPC 模式（MockTransport，不触网） ────────

def _wire_task(task_id="t-1", state="completed", text="hi there"):
    return {
        "id": task_id,
        "contextId": "ctx-1",
        "kind": "task",
        "status": {
            "state": state,
            "message": {"role": "agent",
                        "parts": [{"kind": "text", "text": text}],
                        "messageId": "m-9", "kind": "message"},
        },
        "artifacts": [
            {"artifactId": "a-1", "name": "r", "parts": [{"kind": "text", "text": "x"}]}
        ],
        "history": [
            {"role": "user", "parts": [{"kind": "text", "text": "hello"}],
             "messageId": "m-8", "kind": "message"},
            {"role": "agent", "parts": [{"kind": "text", "text": text}],
             "messageId": "m-9", "kind": "message"},
        ],
        "metadata": {},
    }


def _legacy_wire_task(task_id="t-1", state="completed", text="hi there"):
    """旧 REST 协议的 wire 格式 —— 内部 Task 模型的 dump 形状.

    与 _wire_task（A2A spec 格式，status.message 为 Message 对象、parts 用 kind）
    不同：legacy 端点直接序列化内部模型，故 status.message 是**字符串**、
    messages[].parts[] 用 **type** 判别。client 的 legacy 路径用旧
    TaskSendResponse 模型解析，必须喂这种形状，否则 pydantic 校验失败。
    """
    return {
        "id": task_id,
        "session_id": "",
        "status": {"state": state, "message": text},
        "messages": [
            {"role": "user", "parts": [{"type": "text", "text": "hello"}]},
            {"role": "agent", "parts": [{"type": "text", "text": text}]},
        ],
        "artifacts": [],
        "metadata": {},
    }


def _patch_transport(monkeypatch, handler):
    """让 client.py 内部创建的 httpx.AsyncClient 走 MockTransport."""
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("scout.a2a.client.httpx.AsyncClient", factory)


async def test_client_jsonrpc_send_task(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        body = json.loads(request.content)
        seen["body"] = body
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"], "result": _wire_task(),
        })

    _patch_transport(monkeypatch, handler)
    client = A2AClient("http://127.0.0.1:9999", allow_private=True)  # 默认 jsonrpc
    task = await client.send_task("hello")

    # 请求形态符合规范
    assert seen["path"] == "/a2a"
    assert seen["body"]["jsonrpc"] == "2.0"
    assert seen["body"]["method"] == "message/send"
    assert seen["body"]["params"]["message"]["role"] == "user"
    assert seen["body"]["params"]["message"]["parts"][0]["kind"] == "text"
    # 响应解析回内部类型
    assert task.id == "t-1"
    assert task.status.state == "completed"
    assert task.messages[-1].role == "agent"
    assert task.messages[-1].parts[0].text == "hi there"
    assert task.artifacts[0]["name"] == "r"


async def test_client_jsonrpc_get_task_not_found(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "error": {"code": TASK_NOT_FOUND, "message": "任务不存在"},
        })

    _patch_transport(monkeypatch, handler)
    client = A2AClient("http://127.0.0.1:9999", allow_private=True)
    assert await client.get_task("missing") is None


async def test_client_jsonrpc_remote_error_raised(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "error": {"code": -32603, "message": "kaboom"},
        })

    _patch_transport(monkeypatch, handler)
    client = A2AClient("http://127.0.0.1:9999", allow_private=True)
    with pytest.raises(A2ARemoteError):
        await client.send_task("x")


async def test_client_agent_card_prefers_spec_path(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json={
                "name": "Remote", "description": "d", "url": "http://r/a2a",
                "version": "1.0.0",
            })
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    client = A2AClient("http://127.0.0.1:9999", allow_private=True)
    card = await client.get_agent_card()
    assert seen == ["/.well-known/agent-card.json"]
    assert card.name == "Remote"


async def test_client_agent_card_falls_back_to_legacy_path(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(200, json={
                "name": "OldRemote", "description": "d", "url": "http://r/a2a",
            })
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    client = A2AClient("http://127.0.0.1:9999", allow_private=True)
    card = await client.get_agent_card()
    assert card.name == "OldRemote"


async def test_client_legacy_protocol_uses_old_endpoints(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"task": _legacy_wire_task(task_id="t-old")})

    _patch_transport(monkeypatch, handler)
    client = A2AClient("http://127.0.0.1:9999", allow_private=True, protocol="legacy")
    task = await client.send_task("old school", task_id="t-old")

    assert seen["path"] == "/a2a/tasks/send"
    assert seen["body"]["task"]["id"] == "t-old"
    assert seen["body"]["task"]["messages"][0]["parts"][0]["type"] == "text"
    assert task.id == "t-old"


async def test_client_rejects_unknown_protocol():
    with pytest.raises(ValueError):
        A2AClient("http://127.0.0.1:9999", allow_private=True, protocol="bogus")
