"""A2A SSRF 防护测试 — 私有/保留地址拦截 + allow_private 开关.

覆盖 scout/a2a/client.py 的 check_url_ssrf 与 A2AClient/A2AManager 集成。
"""
import pytest

from scout.a2a.client import A2AClient, A2AManager, check_url_ssrf


class TestCheckUrlSsfr:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8848",
        "http://127.0.0.2/a2a",
        "http://10.0.0.5:8000",
        "http://172.16.0.1:9000",
        "http://192.168.1.100:8080",
        "http://169.254.169.254/latest/meta-data",  # 云元数据地址
        "http://0.0.0.0:80",
        "http://[::1]:8080",
        "http://[fc00::1]/a2a",
        "http://localhost:8848",  # 域名解析到回环/链路本地
    ])
    def test_private_blocked(self, url):
        with pytest.raises(ValueError):
            check_url_ssrf(url)

    def test_public_ip_allowed(self):
        # 公网地址不应被拦
        check_url_ssrf("http://8.8.8.8:8000/a2a")
        check_url_ssrf("https://example.com/a2a")

    def test_allow_private(self):
        check_url_ssrf("http://127.0.0.1:8848", allow_private=True)
        check_url_ssrf("http://192.168.1.5:8080", allow_private=True)

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://host/",
        "gopher://host/",
        "http://",
    ])
    def test_bad_scheme_or_host(self, url):
        with pytest.raises(ValueError):
            check_url_ssrf(url)


class TestA2AClient:
    def test_init_blocks_private(self):
        with pytest.raises(ValueError):
            A2AClient("http://127.0.0.1:8848")

    def test_init_allows_private_when_flag(self):
        client = A2AClient("http://127.0.0.1:8848", allow_private=True)
        assert client.agent_url == "http://127.0.0.1:8848"

    def test_init_blocks_domain_resolving_to_private(self):
        # localhost 解析到回环地址，默认拦截
        with pytest.raises(ValueError):
            A2AClient("http://localhost:8848")


class TestA2AManager:
    def test_add_agent_default_blocks_private(self):
        mgr = A2AManager()
        with pytest.raises(ValueError):
            mgr.add_agent("internal", "http://127.0.0.1:8848")

    def test_add_agent_explicit_allow_private(self):
        mgr = A2AManager()
        client = mgr.add_agent("internal", "http://127.0.0.1:8848", allow_private=True)
        assert mgr.get_client("internal") is client

    def test_remove_and_list(self):
        mgr = A2AManager()
        mgr.add_agent("ext", "http://8.8.8.8:8080", allow_private=False)
        assert mgr.list_agents()[0]["name"] == "ext"
        assert mgr.remove_agent("ext") is True
        assert mgr.remove_agent("ext") is False
