"""代理失败降级直连的回归测试(BiliClient / NeteaseClient)。

背景:httpx 默认 trust_env,系统/环境代理残留(代理进程已退出)会让
全部请求以 WinError 10061(拒绝连接)失败。两个客户端在"存在代理配置
且连接类失败"时应改用 trust_env=False 的直连客户端重试一次;无代理
配置时不重试;重试失败抛原始异常。
"""

from __future__ import annotations

import httpx
import pytest

from neriplayer_win.api.bili.client import BiliClient
from neriplayer_win.api.bili.models import BiliApiError
from neriplayer_win.api.netease.client import NeteaseClient

_PROXIES = {"https": "http://127.0.0.1:7890", "http": "http://127.0.0.1:7890"}


def _ok_response(body: bytes = b'{"code":0}') -> httpx.Response:
    return httpx.Response(200, content=body)


@pytest.fixture()
def bili():
    client = BiliClient()
    # 注入登录 cookie,避免匿名指纹补齐路径发真实网络请求
    client.set_cookies({"SESSDATA": "test"})
    yield client
    client.close()


@pytest.fixture()
def netease():
    client = NeteaseClient()
    yield client
    client.close()


def _with_proxies(monkeypatch, proxies):
    for module in (
        "neriplayer_win.api.bili.client",
        "neriplayer_win.api.netease.client",
    ):
        monkeypatch.setattr(f"{module}.getproxies", lambda: dict(proxies))


class TestBiliDirectFallback:

    def test_connect_error_with_proxy_falls_back_to_direct(
        self, bili, monkeypatch
    ):
        _with_proxies(monkeypatch, _PROXIES)
        monkeypatch.setattr(
            bili._http, "get",
            lambda url, headers=None: (_ for _ in ()).throw(
                httpx.ConnectError("[WinError 10061] 拒绝连接")
            ),
        )
        direct_calls: list[str] = []

        def direct_get(url, headers=None):
            direct_calls.append(url)
            return _ok_response()

        monkeypatch.setattr(bili._http_direct, "get", direct_get)

        text = bili._execute_get_as_text("https://api.bilibili.com/x/test")
        assert direct_calls == ["https://api.bilibili.com/x/test"]
        assert '"code"' in text

    def test_connect_timeout_also_falls_back(self, bili, monkeypatch):
        _with_proxies(monkeypatch, _PROXIES)
        monkeypatch.setattr(
            bili._http, "get",
            lambda url, headers=None: (_ for _ in ()).throw(
                httpx.ConnectTimeout("timed out")
            ),
        )
        monkeypatch.setattr(
            bili._http_direct, "get", lambda url, headers=None: _ok_response()
        )
        assert '"code"' in bili._execute_get_as_text("https://api.bilibili.com/x/t")

    def test_no_proxy_config_no_retry(self, bili, monkeypatch):
        _with_proxies(monkeypatch, {})

        def direct_get(url, headers=None):
            raise AssertionError("无代理配置时不应降级直连")

        monkeypatch.setattr(
            bili._http, "get",
            lambda url, headers=None: (_ for _ in ()).throw(
                httpx.ConnectError("[WinError 10061] 拒绝连接")
            ),
        )
        monkeypatch.setattr(bili._http_direct, "get", direct_get)

        with pytest.raises(BiliApiError) as excinfo:
            bili._execute_get_as_text("https://api.bilibili.com/x/test")
        assert "10061" in str(excinfo.value)

    def test_fallback_failure_raises_original_error(self, bili, monkeypatch):
        _with_proxies(monkeypatch, _PROXIES)
        monkeypatch.setattr(
            bili._http, "get",
            lambda url, headers=None: (_ for _ in ()).throw(
                httpx.ConnectError("original-refused")
            ),
        )
        monkeypatch.setattr(
            bili._http_direct, "get",
            lambda url, headers=None: (_ for _ in ()).throw(
                httpx.ReadTimeout("direct-timeout")
            ),
        )
        with pytest.raises(BiliApiError) as excinfo:
            bili._execute_get_as_text("https://api.bilibili.com/x/test")
        # 抛用户配置路径(代理)的原始错误,不被直连错误覆盖
        assert "original-refused" in str(excinfo.value)


class TestNeteaseDirectFallback:

    def test_send_falls_back_to_direct(self, netease, monkeypatch):
        _with_proxies(monkeypatch, _PROXIES)
        request = netease._http.build_request(
            "POST", "https://music.163.com/weapi/test", data=b"payload"
        )
        monkeypatch.setattr(
            netease._http, "send",
            lambda req: (_ for _ in ()).throw(
                httpx.ConnectError("[WinError 10061] 拒绝连接")
            ),
        )
        direct_calls: list[bytes] = []

        def direct_send(req):
            direct_calls.append(req.read())
            return _ok_response()

        monkeypatch.setattr(netease._http_direct, "send", direct_send)

        response = netease._send_with_direct_fallback(request)
        assert response.status_code == 200
        # bytes 请求体重发不丢 body
        assert direct_calls == [b"payload"]

    def test_no_proxy_config_no_retry(self, netease, monkeypatch):
        _with_proxies(monkeypatch, {})
        request = netease._http.build_request(
            "POST", "https://music.163.com/weapi/test", data=b"payload"
        )

        def direct_send(req):
            raise AssertionError("无代理配置时不应降级直连")

        monkeypatch.setattr(
            netease._http, "send",
            lambda req: (_ for _ in ()).throw(
                httpx.ConnectError("[WinError 10061] 拒绝连接")
            ),
        )
        monkeypatch.setattr(netease._http_direct, "send", direct_send)

        with pytest.raises(httpx.ConnectError):
            netease._send_with_direct_fallback(request)
