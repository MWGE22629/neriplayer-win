"""网络冒烟:匿名可测,不需要真扫码。

申请扫码 unikey 成功即视为核心管线(weapi 加密/请求头/cookie)可用;
另附公共歌单与播放地址解析的冒烟。离线/被墙时自动 skip
(设 NERIPLAYER_SKIP_NETWORK=1 可强制跳过)。
"""

from __future__ import annotations

import os

import httpx
import pytest

from neriplayer_win.api.netease import NeteaseApiError, NeteaseClient

_SKIP = os.environ.get("NERIPLAYER_SKIP_NETWORK") == "1"

# 云音乐热歌榜(公共歌单,匿名可读)
_PUBLIC_PLAYLIST_ID = 3778678
# 公共可播歌曲(id 固定的老歌,匿名可解析出试听/标准音质 URL)
_PUBLIC_SONG_ID = 347230


def _make_client() -> NeteaseClient:
    return NeteaseClient(
        timeout=httpx.Timeout(connect=8.0, read=12.0, write=12.0, pool=8.0)
    )


@pytest.mark.skipif(_SKIP, reason="NERIPLAYER_SKIP_NETWORK=1")
def test_create_qr_session_smoke():
    client = _make_client()
    try:
        session = client.create_qr_session()
    except NeteaseApiError as error:
        client.close()
        pytest.skip(f"网络不可用,跳过冒烟: {error}")
    try:
        assert session.key
        assert session.qr_content.startswith("https://music.163.com/login?codekey=")
        assert f"codekey={session.key}" in session.qr_content
        assert session.chain_id.startswith("v1_")
        assert "web_login" in session.chain_id
    finally:
        client.close()


@pytest.mark.skipif(_SKIP, reason="NERIPLAYER_SKIP_NETWORK=1")
def test_login_status_anonymous_smoke():
    client = _make_client()
    try:
        status = client.get_login_status()
    except NeteaseApiError as error:
        client.close()
        pytest.skip(f"网络不可用,跳过冒烟: {error}")
    try:
        assert status is None  # 匿名 cookie 应判定为未登录
    finally:
        client.close()


@pytest.mark.skipif(_SKIP, reason="NERIPLAYER_SKIP_NETWORK=1")
def test_public_playlist_tracks_smoke():
    client = _make_client()
    try:
        songs = client.get_playlist_tracks(_PUBLIC_PLAYLIST_ID)
    except NeteaseApiError as error:
        client.close()
        pytest.skip(f"网络不可用,跳过冒烟: {error}")
    try:
        assert len(songs) > 50
        first = songs[0]
        assert first.id > 0
        assert first.title
        assert first.duration_ms > 0
    finally:
        client.close()


@pytest.mark.skipif(_SKIP, reason="NERIPLAYER_SKIP_NETWORK=1")
def test_resolve_public_song_url_smoke():
    client = _make_client()
    try:
        playable = client.resolve_playable_url(_PUBLIC_SONG_ID, 250000)
    except NeteaseApiError as error:
        client.close()
        pytest.skip(f"网络不可用,跳过冒烟: {error}")
    try:
        assert playable.url.startswith("https://")
        assert playable.level
    finally:
        client.close()
