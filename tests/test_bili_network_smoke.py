"""B站匿名网络冒烟:不需要登录(收藏夹等需登录接口只在有 cookie 时测)。

链路:匿名 nav(WBI mixin key 来源)→ 音乐区热门挑一支公开视频 →
pagelist 拿 cid → playurl 解析 DASH 音轨 → 带 Referer/UA 拉流前 1KB。
离线/被墙时自动 skip(设 NERIPLAYER_SKIP_NETWORK=1 可强制跳过)。
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from neriplayer_win.api.bili import BiliApiError, BiliClient, build_bili_stream_headers

_SKIP = os.environ.get("NERIPLAYER_SKIP_NETWORK") == "1"

# 音乐区热门排行榜(匿名可读)
_RANKING_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"


def _make_client() -> BiliClient:
    return BiliClient(
        timeout=httpx.Timeout(connect=8.0, read=12.0, write=12.0, pool=8.0)
    )


def _pick_public_bvid(client: BiliClient) -> str:
    """从音乐区热门挑第一支有 cid 的公开视频。"""
    root = client.get_json(_RANKING_URL, {"rid": 3, "type": "all"})
    assert root.get("code", -1) == 0
    for item in (root.get("data") or {}).get("list") or []:
        bvid = item.get("bvid")
        if bvid:
            return str(bvid)
    pytest.fail("排行榜未返回可用视频")


@pytest.mark.skipif(_SKIP, reason="NERIPLAYER_SKIP_NETWORK=1")
def test_anonymous_login_status_smoke():
    client = _make_client()
    try:
        status = client.get_login_status()
    except BiliApiError as error:
        client.close()
        pytest.skip(f"网络不可用,跳过冒烟: {error}")
    try:
        assert status is None  # 匿名(无 SESSDATA)应判定为未登录
    finally:
        client.close()


@pytest.mark.skipif(_SKIP, reason="NERIPLAYER_SKIP_NETWORK=1")
def test_invalid_sessdata_reports_not_logged_in():
    client = _make_client()
    try:
        client.set_cookies({"SESSDATA": "invalid-value-for-smoke"})
    except BiliApiError as error:
        client.close()
        pytest.skip(f"网络不可用,跳过冒烟: {error}")
    try:
        assert client.get_login_status() is None
    finally:
        client.close()


@pytest.mark.skipif(_SKIP, reason="NERIPLAYER_SKIP_NETWORK=1")
def test_wbi_signed_request_smoke():
    """get_json_wbi 走完整 WBI 签名(mixin key + w_rid)并被服务端接受。"""
    client = _make_client()
    try:
        bvid = _pick_public_bvid(client)
    except BiliApiError as error:
        client.close()
        pytest.skip(f"网络不可用,跳过冒烟: {error}")
    try:
        pages = client.get_video_page_list(bvid)
        assert pages, "pagelist 不应为空"
        assert pages[0].cid > 0
        assert pages[0].duration_sec >= 0
    finally:
        client.close()


@pytest.mark.skipif(_SKIP, reason="NERIPLAYER_SKIP_NETWORK=1")
def test_resolve_public_audio_stream_smoke():
    """匿名解析公开免费视频音频流,并验证带 Referer/UA 可实际取流。"""
    client = _make_client()
    try:
        bvid = _pick_public_bvid(client)
    except BiliApiError as error:
        client.close()
        pytest.skip(f"网络不可用,跳过冒烟: {error}")
    try:
        stream = client.resolve_audio_stream(bvid)
        assert stream.url.startswith("https://")
        assert "bilivideo" in stream.url or "mountaintoys" in stream.url
        assert stream.bitrate_kbps >= 0

        headers = build_bili_stream_headers()
        with httpx.Client(
            timeout=httpx.Timeout(connect=8.0, read=12.0, write=12.0, pool=8.0),
            follow_redirects=True,
        ) as http:
            response = http.get(
                stream.url, headers={**headers, "Range": "bytes=0-1023"}
            )
        assert response.status_code in (200, 206)
        assert len(response.content) >= 1
    finally:
        client.close()


@pytest.mark.skipif(_SKIP, reason="NERIPLAYER_SKIP_NETWORK=1")
def test_engine_plays_bili_stream_with_headers(qapp, engine, tmp_path):
    """端到端:解析 B站音频流 → libmpv 带 Referer/UA 实际出声(ao=null)。

    覆盖 PlayerEngine.play_url 的逐文件请求头路径(含 UA 逗号的
    %len% 转义),这是 B站播放与网易云的唯一差异点。
    """
    client = _make_client()
    try:
        bvid = _pick_public_bvid(client)
    except BiliApiError as error:
        client.close()
        pytest.skip(f"网络不可用,跳过冒烟: {error}")
    try:
        stream = client.resolve_audio_stream(bvid)
    finally:
        client.close()

    errors: list[str] = []
    positions: list[float] = []
    engine.error.connect(lambda message: errors.append(message))
    engine.progress.connect(lambda pos, _dur: positions.append(pos))
    try:
        engine.play_url(stream.url, build_bili_stream_headers())
        deadline = time.time() + 30
        while time.time() < deadline:
            qapp.processEvents()
            if errors or (positions and positions[-1] > 0.5):
                break
            time.sleep(0.05)
        assert not errors, f"播放出错: {errors}"
        assert positions and positions[-1] > 0.5, "播放进度未推进"
    finally:
        engine.error.disconnect()
        engine.progress.disconnect()
        engine.stop()


# -- Qt/engine fixtures(与 test_engine.py 同模式) --------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def engine(qapp):
    from neriplayer_win.player.engine import PlayerEngine, PlayerEngineError

    try:
        player = PlayerEngine(audio_output="null")
    except PlayerEngineError as error:
        pytest.skip(f"mpv 运行库不可用: {error}")
    yield player
    player.stop()
