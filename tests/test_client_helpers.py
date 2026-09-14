"""client 纯函数单测:断言移植自 reference 的
NeteaseClientTest.kt / NeteaseQrLoginClientTest.kt / PlayerUrlResolver.kt,
以及 NeteasePlaybackResponseParser 的 VIP/试听识别逻辑。
"""

from __future__ import annotations

import json

from neriplayer_win.api.netease import (
    build_quality_candidates,
    build_qr_account_params,
    merge_netease_request_cookies,
    merge_qr_credential_cookies,
    parse_playback_response,
    should_preheat_netease_weapi_session,
)
from neriplayer_win.api.netease.client import _CookieStore, _parse_song_item


class TestMergeRequestCookies:
    """对应 NeteaseClientTest.mergeRequestCookies_keepsRuntimeSessionCookies"""

    def test_keeps_runtime_session_cookies(self):
        cookies = merge_netease_request_cookies(
            persisted_cookies={
                "MUSIC_U": "persisted-session",
                "__csrf": "persisted-csrf",
                "NMTID": "persisted-context",
            },
            runtime_cookies={
                "MUSIC_U": "runtime-session",
                "NMTID": "runtime-context",
            },
            request_context_cookies={
                "__remember_me": "true",
                "NMTID": "generated-context",
            },
        )
        assert cookies["MUSIC_U"] == "runtime-session"
        assert cookies["__csrf"] == "persisted-csrf"
        assert cookies["NMTID"] == "runtime-context"
        assert cookies["__remember_me"] == "true"
        assert cookies["os"] == "pc"
        assert cookies["appver"] == "8.10.35"

    def test_blank_values_skipped(self):
        cookies = merge_netease_request_cookies(
            persisted_cookies={"MUSIC_U": " ", "NMTID": ""},
            runtime_cookies={},
        )
        assert "MUSIC_U" not in cookies
        assert "NMTID" not in cookies
        assert cookies["os"] == "pc"


class TestWeapiPreheat:
    """对应 NeteaseClientTest.musicUOnlyLogin_requiresWeapiSessionPreheat"""

    def test_requires_preheat(self):
        assert should_preheat_netease_weapi_session(
            {"MUSIC_U": "login-session"}, {}, True
        )

    def test_no_preheat_with_csrf(self):
        assert not should_preheat_netease_weapi_session(
            {"MUSIC_U": "login-session"}, {"__csrf": "csrf-token"}, True
        )

    def test_no_preheat_without_persisted(self):
        assert not should_preheat_netease_weapi_session(
            {"MUSIC_U": "login-session"}, {}, False
        )


class TestQrAccountParams:
    """对应 NeteaseQrLoginClientTest.buildNeteaseQrAccountParams…"""

    def test_follows_web_login_account_request(self):
        params = build_qr_account_params("csrf-token")
        assert params["noCheckToken"] is True
        assert params["csrf_token"] == "csrf-token"

    def test_keeps_nochecktoken_without_csrf(self):
        params = build_qr_account_params("")
        assert params["noCheckToken"] is True
        assert "csrf_token" not in params


class TestQrCredentialCookies:
    """对应 NeteaseQrLoginClientTest.mergeNeteaseQrCredentialCookies…"""

    def test_uses_refresh_token_as_login_credential(self):
        cookies = merge_qr_credential_cookies(
            {"__csrf": "csrf-token"}, "refresh-token"
        )
        assert cookies["MUSIC_U"] == "refresh-token"
        assert cookies["__csrf"] == "csrf-token"

    def test_preserves_existing_music_cookie(self):
        cookies = merge_qr_credential_cookies(
            {"MUSIC_U": "cookie-token"}, "refresh-token"
        )
        assert cookies["MUSIC_U"] == "cookie-token"


class TestQualityCandidates:
    """对应 buildNeteaseQualityCandidates 的回退链语义"""

    def test_drop_before_preferred(self):
        assert build_quality_candidates("lossless") == [
            "lossless",
            "exhigh",
            "higher",
            "standard",
        ]

    def test_unknown_quality_falls_back(self):
        assert build_quality_candidates("weird") == ["weird", "exhigh", "standard"]

    def test_blank_normalizes_to_exhigh(self):
        assert build_quality_candidates("  ") == [
            "exhigh",
            "higher",
            "standard",
        ]


class TestParsePlaybackResponse:
    """对应 NeteasePlaybackResponseParserTest 的核心分支"""

    def test_requires_login_on_301(self):
        assert parse_playback_response('{"code":301}')["kind"] == "requires-login"

    def test_success_object(self):
        raw = json.dumps(
            {
                "code": 200,
                "data": {
                    "url": "http://m804.music.126.net/a.mp3",
                    "type": "mp3",
                    "level": "lossless",
                    "br": 993640,
                    "size": 12345,
                },
            }
        )
        parsed = parse_playback_response(raw, 300000)
        assert parsed["kind"] == "success"
        assert parsed["url"].startswith("http://")
        assert parsed["level"] == "lossless"
        assert parsed["is_preview"] is False

    def test_success_array_data(self):
        raw = json.dumps({"code": 200, "data": [{"url": "https://a/b.flac"}]})
        parsed = parse_playback_response(raw)
        assert parsed["kind"] == "success"
        assert parsed["url"] == "https://a/b.flac"

    def test_vip_no_url_with_fee(self):
        raw = json.dumps({"code": 200, "data": {"url": None, "fee": 1}})
        parsed = parse_playback_response(raw)
        assert parsed == {"kind": "failure", "reason": "no-permission"}

    def test_vip_cannot_listen_reason(self):
        raw = json.dumps(
            {
                "code": 200,
                "data": {
                    "url": None,
                    "freeTrialPrivilege": {"cannotListenReason": 1},
                },
            }
        )
        assert parse_playback_response(raw)["reason"] == "no-permission"

    def test_data_code_404_no_permission(self):
        raw = json.dumps({"code": 200, "data": {"code": 404, "url": None}})
        assert parse_playback_response(raw)["reason"] == "no-permission"

    def test_null_url_string_treated_as_missing(self):
        raw = json.dumps({"code": 200, "data": {"url": "null", "code": 0}})
        assert parse_playback_response(raw)["reason"] == "no-play-url"

    def test_preview_clip_by_freetrialinfo(self):
        raw = json.dumps(
            {"code": 200, "data": {"url": "https://a/preview.mp3", "freeTrialInfo": {"start": 0, "end": 30000}}}
        )
        parsed = parse_playback_response(raw, 300000)
        assert parsed["kind"] == "success"
        assert parsed["is_preview"] is True

    def test_missing_data_node(self):
        assert parse_playback_response('{"code":200}')["reason"] == "no-play-url"

    def test_invalid_json(self):
        assert parse_playback_response("<html>")["reason"] == "unknown"


class TestParseSongItem:
    def test_full_fields(self):
        song = _parse_song_item(
            {
                "id": 347230,
                "name": " adulthood",
                "ar": [{"name": "A"}, {"name": "B"}],
                "al": {"name": "album"},
                "dt": 250000,
            }
        )
        assert song is not None
        assert song.id == 347230
        assert song.title == " adulthood"
        assert song.artist == "A / B"
        assert song.duration_ms == 250000

    def test_legacy_artists_key(self):
        song = _parse_song_item(
            {"id": 1, "name": "x", "artists": [{"name": "C"}], "duration": 1000}
        )
        assert song is not None
        assert song.artist == "C"
        assert song.duration_ms == 1000

    def test_rejects_invalid(self):
        assert _parse_song_item({"id": 0, "name": "x"}) is None
        assert _parse_song_item({"id": 1, "name": ""}) is None


class TestCookieStore:
    def test_save_and_match(self):
        store = _CookieStore()
        store.save_from_response(
            "https://music.163.com/",
            ["NMTID=abc; Path=/; Domain=music.163.com; Max-Age=3600"],
        )
        cookies = store.cookies_for_url("https://music.163.com/weapi/x")
        assert cookies.get("NMTID") == "abc"

    def test_domain_suffix_match(self):
        store = _CookieStore()
        store.save_from_response(
            "https://interface.music.163.com/",
            ["__csrf=tok; Path=/"],
        )
        assert store.cookies_for_url("https://interface.music.163.com/eapi/x").get(
            "__csrf"
        ) == "tok"

    def test_seed_domain_cookies(self):
        store = _CookieStore()
        store.seed_domain_cookies("music.163.com", {"MUSIC_U": "u", "os": "pc"})
        cookies = store.cookies_for_url("https://music.163.com/")
        assert cookies.get("MUSIC_U") == "u"

    def test_later_cookie_overrides_same_name(self):
        store = _CookieStore()
        store.seed_domain_cookies("music.163.com", {"A": "1"})
        store.save_from_response("https://music.163.com/", ["A=2; Path=/"])
        assert store.cookies_for_url("https://music.163.com/")["A"] == "2"
