"""B站客户端纯函数单测:断言移植/对照 reference 的
BiliClient.kt(WBI 签名、playurl 解析、收藏夹解析)、
BiliQrLoginClient.kt(Set-Cookie 解析)、
data/auth/bili/BiliCookieRepository.kt(登录 cookie 判定)。

WBI 金标值独立验证:
- mixin key 72936206... 来自 bilibili-API-collect 官方示例 URL 的公认结果;
- w_rid / hmac 用 md5sum / openssl dgst 独立计算核对,非实现自证。
"""

from __future__ import annotations

import json

from neriplayer_win.api.bili.client import (
    BiliPlayInfo,
    DEFAULT_AUDIO_QUALITY,
    PlayOptions,
    build_html5_fallback_options,
    build_mixin_key_from_urls,
    build_signed_wbi_query,
    ensure_https,
    md5_hex,
    parse_dash_item,
    parse_fav_folder,
    parse_fav_folder_list_result,
    parse_fav_resource_page,
    parse_page_list_response,
    parse_play_info,
    put_common_play_params,
    should_retry_empty_audio_fetch,
    to_audio_stream_infos,
    to_progressive_fallback_stream_infos,
    wbi_filter_value,
    wbi_url_encode,
    web_ticket_hmac_sha256_hex,
)
from neriplayer_win.api.bili.models import (
    BiliDolbyAudio,
    BiliDurl,
    BiliFavItem,
    BiliFlacAudio,
)
from neriplayer_win.api.bili.qr_login import parse_set_cookie_header
from neriplayer_win.api.bili.selector import (
    bili_quality_from_key,
    bili_quality_key_from_netease_level,
    is_bili_stream_host,
    is_bili_stream_url,
    prioritize_bili_stream_urls,
    select_stream_by_preference,
)
from neriplayer_win.api.bili.models import BiliAudioStreamInfo


# ---------------------------------------------------------------------------
# WBI 签名(对照 signWbiUrl / ensureValidMixin / webTicketHmacSha256Hex)
# ---------------------------------------------------------------------------

class TestWbiSigning:
    # bilibili-API-collect 官方示例图片 URL
    _IMG = "https://i0.hdslb.com/bfs/wbi/653657f524a547ac9896d5b06b899817.png"
    _SUB = "https://i0.hdslb.com/bfs/wbi/6e4909c702f846728e64c600ce9d48ad.png"

    def test_mixin_key_known_vector(self):
        # 公认示例输出(bilibili-API-collect wbi 签名文档)
        assert (
            build_mixin_key_from_urls(self._IMG, self._SUB)
            == "72936206c6a79669987ee4f689a74c27"
        )

    def test_mixin_key_blank_url_raises(self):
        import pytest

        from neriplayer_win.api.bili import BiliApiError

        with pytest.raises(BiliApiError):
            build_mixin_key_from_urls("", self._SUB)

    def test_filter_value_strips_special_chars(self):
        # 对应 filterValue:剔除 !'()*
        assert wbi_filter_value("a!b'c(d)*e") == "abcde"
        assert wbi_filter_value("普通关键词") == "普通关键词"

    def test_url_encode_matches_java_urlencoder(self):
        # 空格 → %20(不是 +);. _ - * 保留
        assert wbi_url_encode("a b") == "a%20b"
        assert wbi_url_encode("a.b-c_d*e") == "a.b-c_d*e"
        assert wbi_url_encode("中文") == "%E4%B8%AD%E6%96%87"

    def test_signed_query_known_vector(self):
        # w_rid 由 md5sum 独立核对:md5("bar=514&baz=1919810&foo=114&wts=1702204800" + mixin)
        pairs, w_rid = build_signed_wbi_query(
            {"foo": "114", "bar": "514", "baz": 1919810},
            "72936206c6a79669987ee4f689a74c27",
            wts=1702204800,
        )
        assert pairs == [
            ("bar", "514"),
            ("baz", "1919810"),
            ("foo", "114"),
            ("wts", "1702204800"),
        ]
        assert w_rid == "55ecc2623ec462b5ed56a088c6565795"
        assert md5_hex("bar=514&baz=1919810&foo=114&wts=1702204800" + "72936206c6a79669987ee4f689a74c27") == w_rid

    def test_signed_query_filters_values_before_signing(self):
        pairs, _ = build_signed_wbi_query({"kw": "hello!'()*"}, "k" * 32, wts=1)
        assert dict(pairs)["kw"] == "hello"

    def test_web_ticket_hmac_known_vector(self):
        # openssl dgst -sha256 -hmac "XgwSnGZ1p" 独立核对
        assert (
            web_ticket_hmac_sha256_hex("ts1702204800")
            == "264d88b7e42509f095176be9b846afa4ab8a0fee9a50e1887402c4b291dde945"
        )


# ---------------------------------------------------------------------------
# PlayOptions / playurl 公共参数(对照 putCommonParams)
# ---------------------------------------------------------------------------

class TestPlayOptions:
    def test_default_opts(self):
        # 默认 fnval = DASH|DOLBY = 16|256 = 272,platform=pc
        opts = PlayOptions()
        assert opts.fnval == 272
        assert opts.platform == "pc"
        params: dict[str, str] = {"bvid": "BV1xx", "cid": "123"}
        put_common_play_params(params, opts)
        assert params["fnval"] == "272"
        assert params["fnver"] == "0"
        assert params["fourk"] == "0"
        assert params["otype"] == "json"
        assert params["platform"] == "pc"
        assert "qn" not in params
        assert "high_quality" not in params

    def test_html5_fallback_options(self):
        # 对应 buildHtml5FallbackOptions
        opts = PlayOptions(qn=64, session="s")
        fallback = build_html5_fallback_options(opts)
        assert fallback.fnval == 0
        assert fallback.platform == "html5"
        assert fallback.high_quality == 1
        assert fallback.fourk == 0
        assert fallback.qn == 64
        assert fallback.session == "s"


# ---------------------------------------------------------------------------
# playurl 响应解析(对照 parseDurl/parseDashItem/parsePlayInfo)
# ---------------------------------------------------------------------------

_DASH_AUDIO_SAMPLE = [
    {
        "id": 30216,
        "baseUrl": "https://upos-sz-mirrorhw.bilivideo.com/upgcxcode/1.m4s",
        "backupUrl": ["https://b-demo.edge.mountaintoys.cn/upgcxcode/1.m4s"],
        "bandwidth": 53261,
        "mimeType": "audio/mp4",
        "codecs": "mp4a.40.2",
    },
    {
        "id": 30280,
        "baseUrl": "https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/2.m4s",
        "backupUrl": [],
        "bandwidth": 171559,
        "mimeType": "audio/mp4",
        "codecs": "mp4a.40.2",
    },
]


def _play_info_sample() -> dict:
    return {
        "code": 0,
        "message": "0",
        "data": {
            "quality": 64,
            "format": "dash",
            "timelength": 300000,
            "accept_description": ["清晰 360P"],
            "accept_quality": [16, 32, 64],
            "dash": {
                "video": [
                    {
                        "id": 64,
                        "baseUrl": "https://upos-sz-mirrorhw.bilivideo.com/v.m4s",
                        "backupUrl": [],
                        "bandwidth": 500000,
                        "mimeType": "video/mp4",
                        "codecs": "avc1",
                        "width": 640,
                        "height": 360,
                        "frameRate": "",
                        "codecid": 7,
                    }
                ],
                "audio": _DASH_AUDIO_SAMPLE,
                "dolby": {"type": 1, "audio": []},
                "flac": {"display": True, "audio": None},
            },
        },
    }


class TestPlayInfoParsing:
    def test_parse_play_info_fields(self):
        info = parse_play_info(_play_info_sample())
        assert isinstance(info, BiliPlayInfo)
        assert info.code == 0
        assert info.qn_selected == 64
        assert info.format == "dash"
        assert info.time_length_ms == 300000
        assert info.accept_quality == [16, 32, 64]
        assert info.accept_description == ["清晰 360P"]
        assert len(info.dash_video) == 1
        assert info.dash_video[0].codecid == 7
        assert [a.id for a in info.dash_audio] == [30216, 30280]
        assert isinstance(info.dolby, BiliDolbyAudio) and info.dolby.type == 1
        assert isinstance(info.flac, BiliFlacAudio) and info.flac.display
        assert info.flac.audio is None

    def test_parse_dash_item_requires_base_url(self):
        assert parse_dash_item({"id": 1, "bandwidth": 5}) is None
        # camelCase / snake_case 双兼容
        a = parse_dash_item({"id": 1, "base_url": "https://x/y.m4s"})
        assert a is not None and a.base_url == "https://x/y.m4s"

    def test_to_audio_stream_infos_bitrate_and_candidates(self):
        info = parse_play_info(_play_info_sample())
        streams = to_audio_stream_infos(info)
        assert len(streams) == 2
        by_id = {s.id: s for s in streams}
        # bitrateKbps = bandwidth / 1000(非负保护)
        assert by_id[30216].bitrate_kbps == 53
        assert by_id[30280].bitrate_kbps == 171
        # 候选 URL 重排:upos 镜像优先,mountaintoys 殿后
        assert by_id[30216].url.startswith("https://upos-sz-mirrorhw")
        assert by_id[30216].candidate_urls[-1].startswith("https://b-demo.edge.mountaintoys")
        assert all(s.quality_tag is None for s in streams)

    def test_to_audio_stream_infos_dolby_and_flac_tags(self):
        sample = _play_info_sample()
        dash = sample["data"]["dash"]
        dash["dolby"]["audio"] = [
            {"id": 30250, "baseUrl": "https://upos.example.bilivideo.com/d.m4s", "bandwidth": 384000}
        ]
        dash["flac"]["audio"] = {
            "id": 30251,
            "baseUrl": "https://upos.example.bilivideo.com/f.m4s",
            "bandwidth": 1411000,
        }
        streams = to_audio_stream_infos(parse_play_info(sample))
        tags = {s.id: s.quality_tag for s in streams}
        assert tags[30250] == "dolby"
        assert tags[30251] == "hires"
        # mime 缺省:dolby→audio/eac3,flac→audio/flac
        mimes = {s.id: s.mime_type for s in streams}
        assert mimes[30250] == "audio/eac3"
        assert mimes[30251] == "audio/flac"

    def test_should_retry_empty_audio_fetch(self):
        info = parse_play_info(_play_info_sample())
        assert not should_retry_empty_audio_fetch(info)
        sample = _play_info_sample()
        sample["data"]["dash"]["audio"] = []
        empty = parse_play_info(sample)
        # 无音轨但有视频轨 → 需要重试
        assert should_retry_empty_audio_fetch(empty)

    def test_progressive_fallback_single_durl(self):
        sample = {
            "code": 0,
            "data": {
                "durl": [
                    {
                        "order": 1,
                        "length": 300000,
                        "size": 12000000,
                        "url": "https://upos-sz-mirror08c.bilivideo.com/upgcxcode/x.mp4",
                        "backupUrl": ["https://b1.bilivideo.com/upgcxcode/x.mp4"],
                    }
                ]
            },
        }
        info = parse_play_info(sample)
        assert info.durl[0] == BiliDurl(
            order=1, length_ms=300000, size_bytes=12000000,
            url="https://upos-sz-mirror08c.bilivideo.com/upgcxcode/x.mp4",
            backup_urls=["https://b1.bilivideo.com/upgcxcode/x.mp4"],
        )
        streams = to_progressive_fallback_stream_infos(info)
        assert len(streams) == 1
        # size*8/lengthMs = 12000000*8/300000 = 320
        assert streams[0].bitrate_kbps == 320
        assert streams[0].mime_type == "video/mp4"

    def test_progressive_fallback_requires_single_durl(self):
        info = parse_play_info({"code": 0, "data": {"durl": []}})
        assert to_progressive_fallback_stream_infos(info) == []


# ---------------------------------------------------------------------------
# 收藏夹解析(对照 parseFavFolder / parseFavResourcePage / list-all)
# ---------------------------------------------------------------------------

class TestFavParsing:
    def test_parse_fav_folder_list_all(self):
        data = {
            "count": 2,
            "list": [
                {
                    "id": 111,
                    "fid": 111,
                    "mid": 42,
                    "title": "默认收藏夹",
                    "cover": "//i0.hdslb.com/bfs/archive/x.jpg",
                    "media_count": 10,
                    "cnt_info": {"thumb_up": 1, "play": 2, "collect": 3},
                    "upper": {"mid": 42, "name": "someone"},
                    "attr": 0,
                    "state": 0,
                    "type": 11,
                },
                # 合集形态:无 id,靠 season_id 兜底
                {
                    "season_id": 777,
                    "mid": 43,
                    "name": "某合集",
                    "cover": "",
                    "total": 5,
                    "type": 21,
                },
            ],
        }
        count, folders = parse_fav_folder_list_result(data)
        assert count == 2
        assert len(folders) == 2
        default = folders[0]
        assert default.media_id == 111
        assert default.title == "默认收藏夹"
        assert default.count == 10
        assert default.like_count == 1 and default.play_count == 2 and default.collect_count == 3
        assert default.upper_name == "someone"
        assert default.item_type == 11
        # "//" 开头补 https:
        assert default.cover_url == "https://i0.hdslb.com/bfs/archive/x.jpg"
        season = folders[1]
        assert season.media_id == 777
        assert season.title == "某合集"
        assert season.item_type == 21

    def test_parse_fav_resource_page(self):
        data = {
            "info": {"id": 111, "fid": 111, "mid": 42, "title": "默认收藏夹", "media_count": 2},
            "medias": [
                {
                    "type": 2,
                    "id": 9,
                    "bvid": "BV1xx411c7mD",
                    "title": "demo",
                    "cover": "http://i2.hdslb.com/bfs/x.jpg",
                    "duration": 213,
                    "upper": {"mid": 1, "name": "uper"},
                    "cnt_info": {"play": 100, "danmaku": 5},
                    "fav_time": 1700000000,
                },
                {"type": 21, "id": 5, "bv_id": "BV1collected", "title": "合集条目"},
                {"type": 12, "id": 7, "title": "音频条目(无 bvid)"},
            ],
            "has_more": False,
        }
        page = parse_fav_resource_page(data)
        assert page.info.media_id == 111
        assert page.info.count == 2
        assert page.has_more is False
        assert len(page.items) == 3
        first: BiliFavItem = page.items[0]
        assert first.playable
        assert first.bvid == "BV1xx411c7mD"
        assert first.duration_sec == 213
        assert first.upper_name == "uper"
        assert first.play == 100 and first.danmaku == 5
        assert first.fav_time == 1700000000
        # bv_id 兜底(对应 bvid ?: bv_id)
        assert page.items[1].bvid == "BV1collected"
        assert not page.items[1].playable  # type=21 合集不可直接播
        assert not page.items[2].playable  # 无 bvid 不可播
        # 歌单视角映射:标题/UP主/时长
        song = first.to_song()
        assert (song.title, song.upper_name, song.duration_sec, song.duration_ms) == (
            "demo", "uper", 213, 213000,
        )
        assert song.bvid == "BV1xx411c7mD" and song.avid == 9

    def test_parse_page_list(self):
        root = {
            "code": 0,
            "data": [
                {"cid": 111, "page": 1, "part": "P1", "duration": 100,
                 "dimension": {"width": 16, "height": 9}},
                {"cid": 222, "page": 2, "part": "P2", "duration": 50},
            ],
        }
        pages = parse_page_list_response(root)
        assert [p.cid for p in pages] == [111, 222]
        assert pages[0].width == 16 and pages[1].width == 0


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_ensure_https(self):
        assert ensure_https("//i0.hdslb.com/x.jpg") == "https://i0.hdslb.com/x.jpg"
        assert ensure_https("https://x") == "https://x"
        assert ensure_https(None) == ""
        assert ensure_https("") == ""

    def test_default_quality_key(self):
        assert DEFAULT_AUDIO_QUALITY == "high"


# ---------------------------------------------------------------------------
# QR 客户端 Set-Cookie 解析(对照 parseSetCookieHeader)
# ---------------------------------------------------------------------------

class TestQrSetCookieParsing:
    def test_plain_cookie(self):
        assert parse_set_cookie_header(
            "SESSDATA=abc%2C123; Path=/; Domain=.bilibili.com; HttpOnly"
        ) == ("SESSDATA", "abc%2C123", False)

    def test_empty_value_marks_removed(self):
        assert parse_set_cookie_header("k=; Path=/") == ("k", "", True)

    def test_max_age_zero_marks_removed(self):
        assert parse_set_cookie_header("k=v; Max-Age=0; Path=/") == ("k", "v", True)

    def test_epoch_expires_marks_removed(self):
        assert parse_set_cookie_header(
            "k=v; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/"
        ) == ("k", "v", True)

    def test_future_expires_kept(self):
        assert parse_set_cookie_header(
            "k=v; Expires=Fri, 01 Jan 2100 00:00:00 GMT"
        ) == ("k", "v", False)

    def test_malformed_returns_none(self):
        assert parse_set_cookie_header("nonsense") is None
        assert parse_set_cookie_header("") is None


# ---------------------------------------------------------------------------
# 音频流候选与选轨(断言移植自 BiliAudioSelectorTest.kt)
# ---------------------------------------------------------------------------

class TestStreamPrioritize:
    def test_prefers_bilivideo_hosts_over_mountaintoys(self):
        prioritized = prioritize_bili_stream_urls(
            "https://b-demo.edge.mountaintoys.cn/upgcxcode/demo.m4s",
            [
                "https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/demo.m4s",
                "https://xy123x45x67x89xy.mcdn.bilivideo.cn:8082/v1/resource/demo.m4s",
            ],
        )
        assert prioritized[0] == "https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/demo.m4s"
        assert "mountaintoys.cn" in prioritized[-1]

    def test_matches_mountaintoys_edge_domain(self):
        assert is_bili_stream_host("b-demo.edge.mountaintoys.cn")
        assert is_bili_stream_url("https://b-demo.edge.mountaintoys.cn/upgcxcode/demo.m4s")
        assert not is_bili_stream_host("example.com")
        assert not is_bili_stream_host("")


class TestStreamSelection:
    """对照 BiliAudioSelectorTest.selectStreamByPreference_*"""

    @staticmethod
    def _stream(sid: int, kbps: int, url: str, tag: str | None = None, mime: str = "audio/mp4"):
        return BiliAudioStreamInfo(
            id=sid, mime_type=mime, bitrate_kbps=kbps, quality_tag=tag, url=url
        )

    def test_uses_realistic_bili_bitrates(self):
        medium = self._stream(30232, 92, "https://xy.example.bilivideo.cn/30232.m4s")
        low = self._stream(30216, 48, "https://upos.example.bilivideo.com/30216.m4s")
        high = self._stream(30280, 200, "https://xy.example.mcdn.bilivideo.cn/30280.m4s")
        streams = [low, medium, high]
        assert select_stream_by_preference(streams, "high").id == 30280
        assert select_stream_by_preference(streams, "medium").id == 30232
        assert select_stream_by_preference(streams, "low").id == 30232

    def test_lossless_prefers_real_flac_track(self):
        flac = self._stream(30251, 1411, "https://upos.example.bilivideo.com/30251.m4s",
                            tag="hires", mime="audio/flac")
        high = self._stream(30280, 200, "https://upos.example.bilivideo.com/30280.m4s")
        assert select_stream_by_preference([high, flac], "lossless").id == 30251

    def test_normalizes_quality_tags_before_downgrade(self):
        hires = self._stream(30251, 1411, "https://upos.example.bilivideo.com/30251-hires.m4s",
                             tag="HIRES")
        high = self._stream(30280, 200, "https://upos.example.bilivideo.com/30280.m4s")
        assert select_stream_by_preference([high, hires], "hires").id == 30251

    def test_unknown_key_falls_back_to_high(self):
        streams = [
            self._stream(30216, 48, "https://upos.example.bilivideo.com/30216.m4s"),
            self._stream(30280, 200, "https://upos.example.bilivideo.com/30280.m4s"),
        ]
        assert select_stream_by_preference(streams, "nonexistent").id == 30280

    def test_empty_returns_none(self):
        assert select_stream_by_preference([], "high") is None


class TestNeteaseToBiliQualityKey:
    """M5 音质偏好:网易云档位 → B站偏好键的换算(纯函数)。"""

    def test_known_levels_map_to_tiers(self):
        # standard→最低档 / exhigh→中档(常规有损最高)/ lossless→最高档
        assert bili_quality_key_from_netease_level("standard") == "low"
        assert bili_quality_key_from_netease_level("exhigh") == "high"
        assert bili_quality_key_from_netease_level("lossless") == "lossless"

    def test_all_mapping_results_are_valid_selector_keys(self):
        # 映射产物必须落在 bili_quality_from_key 的合法键集合内,否则会被静默回退
        for level in ("standard", "exhigh", "lossless"):
            key = bili_quality_key_from_netease_level(level)
            assert bili_quality_from_key(key).key == key

    def test_unknown_or_empty_falls_back_to_high(self):
        # 与 BiliClient.DEFAULT_AUDIO_QUALITY 一致的兜底
        assert bili_quality_key_from_netease_level("jymaster") == "high"
        assert bili_quality_key_from_netease_level("") == "high"
        assert bili_quality_key_from_netease_level("  LOSSLESS ") == "lossless"


# ---------------------------------------------------------------------------
# 播放请求头(供 mpv 逐文件 http-header-fields)
# ---------------------------------------------------------------------------

class TestStreamHeaders:
    def test_headers_contain_referer_and_ua(self):
        from neriplayer_win.api.bili import build_bili_stream_headers

        headers = build_bili_stream_headers()
        assert headers["Referer"] == "https://www.bilibili.com"
        assert "Chrome/" in headers["User-Agent"]

    def test_mpv_header_option_escapes_ua_comma(self):
        """浏览器 UA 含 "(KHTML, like Gecko)";mpv loadfile 选项需双层逗号处理。

        形式(经真实 mpv v0.41 实测):
        http-header-fields=<无逗号头>,http-header-fields-append=%<字节长>%<含逗号头>
        - 第一个头走普通赋值;后续头用 -append(全值语义,不被字符串列表拆逗号)
        - 含逗号的值用 %len% 长度前缀转义(loadfile options 的 keyvalue 解析)
        """
        from neriplayer_win.player.engine import build_http_header_fields_option

        from neriplayer_win.api.bili import build_bili_stream_headers

        headers = build_bili_stream_headers()
        option = build_http_header_fields_option(headers)
        referer = f"Referer: {headers['Referer']}"
        ua = f"User-Agent: {headers['User-Agent']}"
        assert "," in ua  # 转义不是空转
        assert option.startswith(f"http-header-fields={referer}")
        expected_ua_part = f"http-header-fields-append=%{len(ua.encode('utf-8'))}%{ua}"
        assert option.endswith(expected_ua_part)

    def test_mpv_header_option_single_header_no_escape(self):
        from neriplayer_win.player.engine import build_http_header_fields_option

        option = build_http_header_fields_option({"Range": "bytes=0-1"})
        assert option == "http-header-fields=Range: bytes=0-1"
