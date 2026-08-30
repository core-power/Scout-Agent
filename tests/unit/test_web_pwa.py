"""PWA 相关 Web 路由测试（manifest / service worker / 图标）."""

from fastapi.testclient import TestClient

from scout.web.server import create_web_app


def _client():
    return TestClient(create_web_app())


def test_manifest_json():
    r = _client().get("/manifest.json")
    assert r.status_code == 200
    assert "application/manifest+json" in r.headers["content-type"]
    data = r.json()
    assert data["name"] == "Scout Agent"
    assert data["start_url"] == "/chat"
    assert data["display"] == "standalone"
    # 必须包含 any 与 maskable 图标
    purposes = {i["purpose"] for i in data["icons"]}
    assert "any" in purposes and "maskable" in purposes


def test_service_worker_scope_allowed():
    r = _client().get("/sw.js")
    assert r.status_code == 200
    assert r.headers.get("service-worker-allowed") == "/"
    assert "text/javascript" in r.headers["content-type"]


def test_pwa_icons_served():
    c = _client()
    for icon in ("icon-192.png", "icon-512.png", "maskable-512.png", "apple-touch-icon.png"):
        r = c.get(f"/static/icons/{icon}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"


def test_sw_does_not_cache_api():
    """Service Worker 源码中不得出现对 /api 的缓存分支."""
    from pathlib import Path
    sw = Path(__file__).resolve().parents[2] / "scout" / "web" / "static" / "sw.js"
    assert sw.exists()
    src = sw.read_text(encoding="utf-8")
    assert "path.startsWith('/api')" in src  # 敏感路径被明确排除
