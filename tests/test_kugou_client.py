"""酷狗客户端解析与凭据逻辑的单元测试。

全部用构造的响应字典,不联网。真实接口的端到端验证在
docs/research/M6-kugou-api.md 与 tools/probe_kugou_*.py 里。

重点覆盖「不要频繁登录」相关的三条约定(见 M6 调研 §2.6 实现要求):
- 30020 归入限流(先退避重试,不提示重登)
- 35005 归入 token 失效(触发续期,续期失败才提示重登)
- 播放请求的 cookie 必须带上完整 KuGoo 串,query 也要有 token
"""

from __future__ import annotations

import pytest

from neriplayer_win.api.kugou import (
    KugouApiError,
    KugouAuthRequiredError,
    KugouClient,
    KugouCredentials,
    KugouNoPlayUrlError,
    KugouRateLimitedError,
    KugouSong,
)
from neriplayer_win.api.kugou.client import (
    parse_play_response,
    parse_playlist_list_response,
    parse_playlist_tracks_response,
    parse_search_response,
    parse_song_info_response,
)

def _creds(**overrides) -> KugouCredentials:
    base = dict(
        userid="1060404679",
        token="token-value",
        t1="t1-value",
        mid="m" * 32,
        dfid="d" * 24,
        kugoo_cookie="KugooID=1060404679&KugooPwd=ABCD&t=old",
    )
    base.update(overrides)
    return KugouCredentials(**base)

class TestParseSearchResponse:
    def test_parses_core_fields(self) -> None:
        payload = {
            "status": 1,
            "data": {
                "total": 480,
                "lists": [
                    {
                        "FileHash": "AAA",
                        "HQFileHash": "BBB",
                        "SQFileHash": "",
                        "EMixSongID": "j410q60",
                        "AlbumID": "966846",
                        "SongName": "晴天",
                        "SingerName": "周杰伦",
                        "Duration": "269",
                        "Image": "http://imge.kugou.com/stdmusic/{size}/x.jpg",
                        "Privilege": 10,
                        "PayType": 3,
                    }
                ],
            },
        }
        songs, total = parse_search_response(payload)
        assert total == 480
        assert len(songs) == 1
        song = songs[0]
        assert song.hash == "AAA"
        assert song.encode_album_audio_id == "j410q60"
        assert song.album_id == "966846"
        assert song.title == "晴天"
        assert song.artist == "周杰伦"
        assert song.duration_ms == 269_000
        assert song.hashes == {"FileHash": "AAA", "HQFileHash": "BBB"}
        assert "{size}" not in song.cover_url

    def test_skips_items_without_hash_or_title(self) -> None:
        payload = {"data": {"total": 2, "lists": [
            {"FileHash": "", "SongName": "x"},
            {"FileHash": "H", "SongName": ""},
        ]}}
        songs, _ = parse_search_response(payload)
        assert songs == []

    def test_playable_key_prefers_encode_id(self) -> None:
        payload = {"data": {"lists": [
            {"FileHash": "H", "EMixSongID": "E", "SongName": "t"},
            {"FileHash": "H2", "SongName": "t2"},
        ]}}
        songs, _ = parse_search_response(payload)
        assert songs[0].playable_key == "E"
        assert songs[1].playable_key == "H2"


class TestParsePlaylistList:
    def test_parses_playlists_and_liked_flag(self) -> None:
        payload = {"data": {
            "list_count": 2,
            "info": [
                {"listid": 1, "name": "默认收藏", "count": 17, "is_def": 1,
                 "global_collection_id": "collection_3_1_1_0",
                 "pic": "http://c1.kgimg.com/{size}/x.jpg",
                 "list_create_userid": 1},
                {"listid": 2, "name": "我喜欢", "count": 525, "is_def": 2,
                 "global_collection_id": "collection_3_1_2_0"},
            ],
        }}
        playlists = parse_playlist_list_response(payload)
        assert [p.listid for p in playlists] == [1, 2]
        assert playlists[0].is_liked is False
        assert playlists[1].is_liked is True
        assert playlists[1].count == 525
        assert "{size}" not in playlists[0].cover_url

    def test_falls_back_to_derived_global_id(self) -> None:
        """global_collection_id 缺失时按 collection_3_<uid>_<listid>_0 兜底。"""
        payload = {"data": {"info": [
            {"listid": 5, "name": "x", "list_create_userid": 99},
        ]}}
        playlists = parse_playlist_list_response(payload)
        assert playlists[0].global_collection_id == "collection_3_99_5_0"


class TestParsePlaylistTracks:
    def test_strips_extension_and_joins_singers(self) -> None:
        payload = {"data": {"info": [
            {
                "hash": "H",
                "name": "A - B.mp3",
                "timelen": 161_906,
                "singerinfo": [{"name": "A"}, {"name": "B"}],
                "album_id": "1",
                "cover": "http://imge.kugou.com/{size}/c.jpg",
                "media_privilege": 10,
                "media_pay_type": 3,
            }
        ]}}
        songs = parse_playlist_tracks_response(payload)
        song = songs[0]
        assert song.title == "A - B"
        assert song.artist == "A / B"
        assert song.duration_ms == 161_906
        assert "{size}" not in song.cover_url

    def test_ogg_320_hash_is_not_treated_as_mp3_tier(self) -> None:
        """trans_param.ogg_320_hash 是 ogg 容器,与播放接口 mp3 档不通用。

        实测传它返回 status=1 但 play_url 为空,所以不能当作 HQFileHash。
        """
        payload = {"data": {"info": [
            {"hash": "H", "name": "x.mp3", "trans_param": {"ogg_320_hash": "OGG"}},
        ]}}
        songs = parse_playlist_tracks_response(payload)
        assert songs[0].hashes == {"FileHash": "H"}


class TestParseSongInfo:
    def test_maps_tier_fields(self) -> None:
        payload = {
            "hash": "H",
            "extra": {"128hash": "H", "320hash": "H320", "sqhash": "HSQ",
                      "highhash": "HH"},
        }
        info = parse_song_info_response(payload)
        assert info == {
            "FileHash": "H", "HQFileHash": "H320",
            "SQFileHash": "HSQ", "ResFileHash": "HH",
        }

    def test_drops_empty_fields(self) -> None:
        info = parse_song_info_response({"hash": "H", "extra": {"320hash": ""}})
        assert info == {"FileHash": "H"}


class TestParsePlayResponse:
    def test_success_returns_urls(self) -> None:
        payload = {"status": 1, "err_code": 0, "data": {
            "play_url": "https://webfs.kugou.com/a.mp3",
            "play_backup_url": "https://webfs.tx.kugou.com/a.mp3",
            "bitrate": 320,
            "timelength": 269_000,
        }}
        playable = parse_play_response(payload, "exhigh")
        assert playable.url.endswith("a.mp3")
        assert playable.candidate_urls == [
            "https://webfs.kugou.com/a.mp3",
            "https://webfs.tx.kugou.com/a.mp3",
        ]
        assert playable.bitrate_kbps == 320
        assert playable.quality_key == "HQFileHash"

    def test_30020_is_rate_limit_not_auth(self) -> None:
        """实测 30020 多为密集请求限流,不该让用户重登。"""
        with pytest.raises(KugouRateLimitedError):
            parse_play_response({"status": 0, "err_code": 30020}, "lossless")

    def test_35005_is_token_invalid(self) -> None:
        with pytest.raises(KugouAuthRequiredError):
            parse_play_response({"status": 0, "err_code": 35005}, "lossless")

    def test_empty_url_means_no_permission(self) -> None:
        with pytest.raises(KugouNoPlayUrlError, match="没有可播放地址"):
            parse_play_response(
                {"status": 1, "data": {"play_url": "", "bitrate": 128}}, "lossless"
            )

    def test_unknown_code_is_plain_api_error(self) -> None:
        with pytest.raises(KugouNoPlayUrlError):
            parse_play_response({"status": 0, "err_code": 20006}, "lossless")


class TestPlayCookies:
    def test_includes_kugoo_cookie_verbatim(self) -> None:
        """播放鉴权靠 KuGoo 串,必须原样带上(不能重建简化串)。"""
        client = KugouClient(_creds())
        cookies = client._play_cookies()
        assert cookies["KuGoo"] == "KugooID=1060404679&KugooPwd=ABCD&t=old"
        assert cookies["t"] == "token-value"
        assert cookies["KugooID"] == "1060404679"

    def test_empty_without_web_login(self) -> None:
        """没有网页登录态(无 KuGoo 串)时不发 cookie,由解析层报错。"""
        client = KugouClient(_creds(kugoo_cookie=""))
        assert client._play_cookies() == {}

    def test_credentials_flags(self) -> None:
        both = _creds()
        assert both.has_login and both.has_web_login
        only_token = _creds(kugoo_cookie="")
        assert only_token.has_login and not only_token.has_web_login
        only_web = _creds(token="")
        assert not only_web.has_login and only_web.has_web_login


class TestAuthRetry:
    def test_retries_once_after_refresh(self) -> None:
        """凭据失效时先续期再重试一次(不是直接失败)。"""
        client = KugouClient(_creds())
        refreshed = {"count": 0}

        def fake_refresh() -> KugouCredentials:
            refreshed["count"] += 1
            client.set_credentials(_creds(token="new-token"))
            return client.credentials()

        client.refresh_token = fake_refresh  # type: ignore[method-assign]
        calls = {"n": 0}

        def call() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise KugouAuthRequiredError("expired")
            return "ok"

        assert client._with_auth_retry(call) == "ok"
        assert refreshed["count"] == 1
        assert calls["n"] == 2

    def test_second_failure_propagates(self) -> None:
        """续期后仍失败,异常要抛给上层提示重登。"""
        client = KugouClient(_creds())
        client.refresh_token = lambda: client.credentials()  # type: ignore[method-assign]

        def call() -> str:
            raise KugouAuthRequiredError("still expired")

        with pytest.raises(KugouAuthRequiredError):
            client._with_auth_retry(call)

    def test_no_refresh_when_not_logged_in(self) -> None:
        """未登录时不做无意义的续期尝试。"""
        client = KugouClient(_creds(token=""))
        called = {"n": 0}

        def fake_refresh() -> KugouCredentials:
            called["n"] += 1
            return client.credentials()

        client.refresh_token = fake_refresh  # type: ignore[method-assign]

        def call() -> str:
            raise KugouAuthRequiredError("no login")

        with pytest.raises(KugouAuthRequiredError):
            client._with_auth_retry(call)
        assert called["n"] == 0


class TestRateLimitRetry:
    def test_retries_until_success(self, monkeypatch) -> None:
        """限流退避:前两次抛限流,第三次成功。"""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        client = KugouClient(_creds())
        calls = {"n": 0}

        def call() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise KugouRateLimitedError("busy")
            return "ok"

        assert client._retry_rate_limited(call) == "ok"
        assert calls["n"] == 3

    def test_gives_up_after_three_attempts(self, monkeypatch) -> None:
        monkeypatch.setattr("time.sleep", lambda _s: None)
        client = KugouClient(_creds())

        def call() -> str:
            raise KugouRateLimitedError("busy")

        with pytest.raises(KugouRateLimitedError):
            client._retry_rate_limited(call)

    def test_non_rate_limit_errors_pass_through(self, monkeypatch) -> None:
        """限流重试不应吞掉其它错误。"""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        client = KugouClient(_creds())

        def call() -> str:
            raise KugouNoPlayUrlError("no url")

        with pytest.raises(KugouNoPlayUrlError):
            client._retry_rate_limited(call)


class TestLookupByHash:
    def test_matches_by_file_hash_only(self, monkeypatch) -> None:
        """只按 FileHash 精确匹配,不做标题模糊匹配(防同名翻唱)。"""
        client = KugouClient(_creds())
        target = KugouSong(hash="ABC", title="t", artist="a", duration_ms=1000)
        same_name = KugouSong(hash="ZZZ", title="t", artist="a", duration_ms=1000,
                              hashes={"FileHash": "ZZZ"})
        exact = KugouSong(hash="ABC", title="t", artist="a", duration_ms=1000,
                          encode_album_audio_id="E",
                          hashes={"FileHash": "ABC", "SQFileHash": "SQ"})
        monkeypatch.setattr(
            client, "search_songs", lambda *_a, **_k: ([same_name, exact], 2)
        )
        matched = client._lookup_song_by_hash(target)
        assert matched is not None
        assert matched.encode_album_audio_id == "E"

    def test_returns_none_when_no_hash_match(self, monkeypatch) -> None:
        client = KugouClient(_creds())
        target = KugouSong(hash="ABC", title="t", artist="a", duration_ms=1000)
        other = KugouSong(hash="ZZZ", title="t", artist="a", duration_ms=1000,
                          hashes={"FileHash": "ZZZ"})
        monkeypatch.setattr(client, "search_songs", lambda *_a, **_k: ([other], 1))
        assert client._lookup_song_by_hash(target) is None

    def test_search_failure_is_swallowed(self, monkeypatch) -> None:
        """反查失败不该让播放整体失败,回落到已知 hash。"""
        client = KugouClient(_creds())

        def boom(*_a, **_k):
            raise KugouApiError("network")

        monkeypatch.setattr(client, "search_songs", boom)
        target = KugouSong(hash="ABC", title="t", artist="a", duration_ms=1000)
        assert client._lookup_song_by_hash(target) is None


class TestCredentialExpiry:
    def test_seconds_left(self) -> None:
        creds = _creds(t_expire_time=1000)
        assert creds.seconds_left(400) == 600

    def test_unknown_expiry_returns_negative(self) -> None:
        assert _creds(t_expire_time=0).seconds_left(400) == -1
