"""酷狗 API 客户端(同步 httpx 实现)。

契约来源:2026-10-23/24 本机实测锚定,详见 docs/research/M6-kugou-api.md。
reference/NeriPlayer-Android 里酷狗只作为歌词源存在,没有播放实现可对照,
所以本模块没有 Kotlin 对照物,不能照抄(TODO.md M6 立项说明)。

两类接口,两套签名(实测,不可混用):

1. 网页接口(H5 签名,盐 WEB_SIGN_SALT)
   - 搜索:songsearch.kugou.com/song_search_v2(连签名都不校验)
   - 播放地址:wwwapi.kugou.com/play/songinfo
   - 歌曲元数据:m.kugou.com/app/i/getSongInfo.php(无鉴权)

2. 客户端接口(Android 签名,盐 ANDROID_SIGN_SALT)
   - 网关统一入口 gateway.kugou.com,目标服务由 x-router 指定
   - 歌单列表:v7/get_all_list        -> x-router: cloudlist.service.kugou.com
   - 歌单曲目:v4/get_list_all_file_v3 -> 同上
   - 歌单详情:v3/get_list_info       -> x-router: pubsongs.kugou.com
   - 登录校验:v1/user_verify         -> x-router: trackercdngz.kugou.com
   - token 续期:login.user.kugou.com/v5/login_by_token(唯一走 http 的接口)

登录态设计(对应用户要求「不要频繁登录」):

- 只用扫码登录的 token。网页登录 cookie 只有 24 小时且无续期接口,
  扫码 token 有效期 30 天(t_expire_time)且可用 refresh_token() 无感续期。
- 一套 token 同时服务两类接口。调 play/songinfo 时 token 必须**同时**
  出现在 query 的 token 与 cookie 的 t 里,实测只放 query 会返回 30020。
- 遇到 30020 / 35005 时自动续期并重试一次;续期失败才抛
  KugouAuthRequiredError 让上层提示重新扫码。这段逻辑集中在本模块的
  _with_auth_retry(),不要在调用方重复。

失败语义对齐现有两源:网络/HTTP 异常抛 KugouApiError,登录失效抛
KugouAuthRequiredError,无播放地址抛 KugouNoPlayUrlError。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping, TypeVar

import httpx

from ...log import get_logger
from .crypto import (
    android_signature,
    build_refresh_payload,
    decode_refresh_response,
    web_signature,
)
from .models import (
    AUTH_ERROR_CODES,
    ERR_TOKEN_INVALID,
    ERR_UNAUTHORIZED,
    KugouAccount,
    KugouApiError,
    KugouAuthRequiredError,
    KugouCredentials,
    KugouNoPlayUrlError,
    KugouPlayableUrl,
    KugouPlaylist,
    KugouQrCheckResult,
    KugouQrSession,
    KugouRateLimitedError,
    KugouSong,
)
from .selector import (
    DEFAULT_QUALITY,
    hash_field_bitrate,
    select_hash,
)

logger = get_logger("kugou")

# ---- 接口地址(实测有效,改动前先按 docs/research 里的探测脚本复核) --------
SEARCH_URL = "https://songsearch.kugou.com/song_search_v2"
PLAY_URL = "https://wwwapi.kugou.com/play/songinfo"
SONG_INFO_URL = "https://m.kugou.com/app/i/getSongInfo.php"
GATEWAY_URL = "https://gateway.kugou.com"
QRCODE_URL = "https://login-user.kugou.com/v2/qrcode"
QRCODE_CHECK_URL = "https://login-user.kugou.com/v2/get_userinfo_qrcode"
# 唯一走 http 的接口:该域名 https 证书与域名不匹配(实测
# CERTIFICATE_VERIFY_FAILED: Hostname mismatch),只能 http。
TOKEN_REFRESH_URL = "http://login.user.kugou.com/v5/login_by_token"

WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ANDROID_UA = "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"
KUGOU_REFERER = "https://www.kugou.com/"
# 客户端网关要求的固定业务头,缺失会被风控拦(实测)
GATEWAY_HEADERS = {
    "kg-rc": "1",
    "kg-thash": "5d816a0",
    "kg-rec": "1",
    "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",
}

# H5 侧固定参数(实测:srcappid 只能是 2919)
H5_SRCAPPID = "2919"
H5_APPID = "1005"
H5_CLIENTVER = "20000"
ANDROID_APPID = "1005"
ANDROID_CLIENTVER = "20489"

# 搜索分页上限(接口允许更大,这里与两源对齐)
SEARCH_PAGE_SIZE = 30
# 歌单曲目单页上限:实测 300 可一次取回,避免多次翻页
PLAYLIST_TRACK_PAGE_SIZE = 300
# 距过期不足该秒数时主动续期(3 天),避免用户使用中途失效
REFRESH_AHEAD_SECONDS = 3 * 24 * 3600

_SEARCH_QUALITY_FIELDS = (
    "FileHash", "HQFileHash", "SQFileHash", "ResFileHash", "SuperFileHash",
)

T = TypeVar("T")

def _now_ms() -> int:
    return int(time.time() * 1000)

def _now_s() -> int:
    return int(time.time())

def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _as_str(value: Any) -> str:
    return "" if value is None else str(value)

def _normalize_cover(url: str, size: int = 400) -> str:
    """把 `{size}` 占位替换成像素值。

    实测酷狗封面统一形如 `http://imge.kugou.com/stdmusic/{size}/....jpg`,
    直接把占位换掉即可,不需要网易云那种 CDN 改写规则。
    """
    if not url:
        return ""
    return url.replace("{size}", str(size))

def parse_search_response(payload: Mapping[str, Any]) -> tuple[list[KugouSong], int]:
    """解析搜索结果,返回 (歌曲列表, 总数)。

    实测字段(均为该接口原始拼写):
    FileHash/HQFileHash/SQFileHash/ResFileHash、EMixSongID(播放主键)、
    AlbumID、SongName、SingerName、Duration(秒)、Image、Privilege、PayType。
    """
    data = payload.get("data") or {}
    total = _as_int(data.get("total"))
    songs: list[KugouSong] = []
    for item in data.get("lists") or []:
        if not isinstance(item, Mapping):
            continue
        title = _as_str(item.get("SongName")).strip()
        file_hash = _as_str(item.get("FileHash")).strip()
        if not title or not file_hash:
            continue
        hashes = {
            field: _as_str(item.get(field)).strip()
            for field in _SEARCH_QUALITY_FIELDS
            if _as_str(item.get(field)).strip()
        }
        songs.append(
            KugouSong(
                hash=file_hash,
                title=title,
                artist=_as_str(item.get("SingerName")).strip(),
                duration_ms=_as_int(item.get("Duration")) * 1000,
                encode_album_audio_id=_as_str(item.get("EMixSongID")).strip(),
                album_id=_as_str(item.get("AlbumID")).strip(),
                hashes=hashes,
                cover_url=_normalize_cover(_as_str(item.get("Image"))),
                privilege=_as_int(item.get("Privilege")),
                pay_type=_as_int(item.get("PayType")),
            )
        )
    return songs, total

def parse_playlist_list_response(payload: Mapping[str, Any]) -> list[KugouPlaylist]:
    """解析 v7/get_all_list 的 data.info。

    实测 info[] 关键字段:listid、global_collection_id、name、count、
    pic、is_def(1 默认收藏 / 2 我喜欢 / None 普通)、list_create_userid。
    """
    data = payload.get("data") or {}
    playlists: list[KugouPlaylist] = []
    for item in data.get("info") or []:
        if not isinstance(item, Mapping):
            continue
        listid = _as_int(item.get("listid"))
        name = _as_str(item.get("name")).strip()
        gid = _as_str(item.get("global_collection_id")).strip()
        if not listid or not name:
            continue
        raw_is_def = item.get("is_def")
        playlists.append(
            KugouPlaylist(
                listid=listid,
                global_collection_id=gid or f"collection_3_{item.get('list_create_userid')}_{listid}_0",
                name=name,
                count=_as_int(item.get("count")),
                cover_url=_normalize_cover(_as_str(item.get("pic"))),
                is_def=_as_int(raw_is_def) if raw_is_def is not None else None,
                creator_id=_as_int(item.get("list_create_userid")),
            )
        )
    return playlists

def parse_playlist_tracks_response(payload: Mapping[str, Any]) -> list[KugouSong]:
    """解析 v4/get_list_all_file_v3 的 data.info。

    实测字段:hash(128k 档)、name(文件名,含扩展名)、singerinfo[].name、
    timelen(毫秒)、cover、album_id、mixsongid、media_privilege、
    media_pay_type、trans_param.ogg_320_hash(320k 档)。

    注意接口只给 128k hash;偏好更高音质时调用方需再调 get_song_info()
    补各档 hash。这里先把已知的两档填进 hashes。
    """
    data = payload.get("data") or {}
    songs: list[KugouSong] = []
    for item in data.get("info") or []:
        if not isinstance(item, Mapping):
            continue
        raw_name = _as_str(item.get("name")).strip()
        file_hash = _as_str(item.get("hash")).strip()
        if not raw_name or not file_hash:
            continue
        # name 是「歌手 - 歌名.mp3」形式,去掉扩展名
        title = raw_name
        for ext in (".mp3", ".flac", ".m4a", ".ogg", ".mgg"):
            if title.lower().endswith(ext):
                title = title[: -len(ext)]
                break
        singer_parts = [
            _as_str(singer.get("name")).strip()
            for singer in (item.get("singerinfo") or [])
            if isinstance(singer, Mapping) and _as_str(singer.get("name")).strip()
        ]
        trans = item.get("trans_param") or {}
        # 只认 FileHash。trans_param.ogg_320_hash 是 ogg 容器格式、与
        # play/songinfo 的 mp3 档位不通用(实测传它返回 status=1 但无 url),
        # 高音质档位一律靠 _lookup_song_by_hash 反查搜索接口获得。
        hashes = {"FileHash": file_hash}
        album_info = item.get("albuminfo") or {}
        songs.append(
            KugouSong(
                hash=file_hash,
                title=title,
                artist=" / ".join(singer_parts),
                duration_ms=_as_int(item.get("timelen")),
                encode_album_audio_id=_as_str(item.get("emixsongid")).strip(),
                album_id=_as_str(item.get("album_id") or album_info.get("id")).strip(),
                hashes=hashes,
                cover_url=_normalize_cover(
                    _as_str(item.get("cover") or trans.get("union_cover"))
                ),
                privilege=_as_int(item.get("media_privilege")),
                pay_type=_as_int(item.get("media_pay_type")),
            )
        )
    return songs

def parse_song_info_response(payload: Mapping[str, Any]) -> dict[str, str]:
    """解析 getSongInfo.php 的各档 hash(用于给歌单曲目补音质档位)。

    实测 extra 字段:128hash / 320hash / sqhash / highhash。
    """
    extra = payload.get("extra") or {}
    mapping = {
        "FileHash": _as_str(extra.get("128hash") or payload.get("hash")).strip(),
        "HQFileHash": _as_str(extra.get("320hash")).strip(),
        "SQFileHash": _as_str(extra.get("sqhash")).strip(),
        "ResFileHash": _as_str(extra.get("highhash")).strip(),
    }
    return {key: value for key, value in mapping.items() if value}

def parse_play_response(
    payload: Mapping[str, Any], preferred_quality: str
) -> KugouPlayableUrl:
    """解析 play/songinfo 的 data。

    实测 data 字段:play_url、play_backup_url、bitrate、timelength(ms)、
    privilege、img、song_name、author_name、album_id、encode_album_audio_id。

    失败分支:
    - 30020 且没有网页登录态 -> 抛 KugouAuthRequiredError(缺 KuGoo 串)
    - 30020 但有网页登录态 -> 抛 KugouRateLimitedError(实测密集请求会
      触发,退避重试可恢复,不是凭据问题)
    - 其它错误码 -> 抛 KugouNoPlayUrlError(带错误码)
    - status == 1 但 play_url 为空 -> 抛 KugouNoPlayUrlError(无版权/需付费)
    """
    err_code = _as_int(payload.get("err_code"))
    if _as_int(payload.get("status")) != 1:
        if err_code == ERR_TOKEN_INVALID:
            raise KugouAuthRequiredError(
                "酷狗登录已失效,请重新登录", code=err_code
            )
        if err_code == ERR_UNAUTHORIZED:
            raise KugouRateLimitedError(
                f"酷狗播放接口拒绝请求(错误码 {err_code})", code=err_code
            )
        raise KugouNoPlayUrlError(
            f"酷狗返回错误码 {err_code}", code=err_code
        )
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise KugouNoPlayUrlError("酷狗播放接口返回结构异常")
    url = _as_str(data.get("play_url")).strip()
    if not url:
        raise KugouNoPlayUrlError("该歌曲没有可播放地址(可能无版权或需要购买)")
    bitrate = _as_int(data.get("bitrate"))
    # 用实际返回的 bitrate 反查档位名,便于状态栏提示
    quality_key = preferred_quality
    for field, value in (
        ("FileHash", 128), ("HQFileHash", 320), ("SQFileHash", 937), ("ResFileHash", 1642)
    ):
        if bitrate and abs(bitrate - value) <= 40:
            quality_key = field
            break
    return KugouPlayableUrl(
        url=url,
        backup_url=_as_str(data.get("play_backup_url")).strip(),
        bitrate_kbps=bitrate,
        quality_key=quality_key,
        duration_ms=_as_int(data.get("timelength")),
    )

class KugouClient:
    """酷狗客户端。线程约束:与两源一致,同步方法由上层 run_async 调度。"""

    def __init__(
        self,
        credentials: KugouCredentials | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._timeout = timeout or httpx.Timeout(15.0)
        self._credentials = credentials or KugouCredentials(userid="", token="", mid="", dfid="")
        # 续期后回调上层落盘;由 MainWindow 注入,避免本模块碰磁盘
        self._on_credentials_refreshed: Callable[[KugouCredentials], None] | None = None

    # -- 凭据 ---------------------------------------------------------------

    def set_credentials(self, credentials: KugouCredentials) -> None:
        self._credentials = credentials

    def credentials(self) -> KugouCredentials:
        return self._credentials

    def set_refresh_callback(
        self, callback: Callable[[KugouCredentials], None] | None
    ) -> None:
        """注册续期成功后的落盘回调(参数是新凭据)。"""
        self._on_credentials_refreshed = callback

    def has_login(self) -> bool:
        return self._credentials.has_login

    def logout(self) -> None:
        self._credentials = KugouCredentials(
            userid="", token="", mid=self._credentials.mid, dfid=self._credentials.dfid
        )

    # -- HTTP 基础设施 ------------------------------------------------------

    def _client(self, headers: Mapping[str, str] | None = None) -> httpx.Client:
        merged = {"User-Agent": WEB_UA, "Referer": KUGOU_REFERER}
        if headers:
            merged.update(headers)
        return httpx.Client(timeout=self._timeout, headers=merged)

    def _play_cookies(self) -> dict[str, str]:
        """调网页播放接口时的 cookie。

        实测关键点(见 docs/research §2.6):播放鉴权靠网页登录下发的
        KuGoo 复合串(内含 KugooPwd),不是 query 的 token。把 KuGoo 换成
        自造的简化串、或把里面的 t 换成续期后的新 token,都会返回 30020。
        因此这里原样透传落盘的 KuGoo 串,不做任何改写。
        """
        creds = self._credentials
        if not creds.has_web_login:
            return {}
        jar: dict[str, str] = {
            "KuGoo": creds.kugoo_cookie,
            "KugooID": creds.userid,
            "t": creds.token,
        }
        # 这几项有就带上:实测缺了不影响,但完整一点更接近真实浏览器
        if creds.mid:
            jar["mid"] = creds.mid
            jar["kg_mid"] = creds.mid
        if creds.dfid:
            jar["dfid"] = creds.dfid
            jar["kg_dfid"] = creds.dfid
        if creds.t1:
            jar["t1"] = creds.t1
        return jar

    def _h5_params(self, extra: Mapping[str, Any]) -> dict[str, Any]:
        creds = self._credentials
        params: dict[str, Any] = {
            "srcappid": H5_SRCAPPID,
            "appid": H5_APPID,
            "platid": "1",
            "clientver": H5_CLIENTVER,
            "clienttime": str(_now_ms()),
            "mid": creds.mid or "-",
            "uuid": creds.mid or "-",
            "dfid": creds.dfid or "-",
        }
        if creds.has_login:
            params["userid"] = creds.userid
            params["token"] = creds.token
        params.update(extra)
        params["signature"] = web_signature(params)
        return params

    def _android_params(self, extra: Mapping[str, Any]) -> dict[str, Any]:
        creds = self._credentials
        params: dict[str, Any] = {
            "dfid": creds.dfid or "-",
            "mid": creds.mid or "-",
            "uuid": "-",
            "appid": ANDROID_APPID,
            "clientver": ANDROID_CLIENTVER,
            "clienttime": str(_now_s()),
        }
        if creds.has_login:
            params["userid"] = creds.userid
            params["token"] = creds.token
        params.update(extra)
        return params

    def _gateway_post(
        self,
        path: str,
        router: str,
        body: Mapping[str, Any],
        extra_params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """客户端接口统一入口:Android 签名 + x-router 路由。"""
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        params = self._android_params(extra_params or {})
        params["signature"] = android_signature(params, payload)
        headers = dict(GATEWAY_HEADERS)
        headers["User-Agent"] = ANDROID_UA
        headers["x-router"] = router
        headers["Content-Type"] = "application/json"
        headers["dfid"] = params["dfid"]
        headers["mid"] = params["mid"]
        headers["clienttime"] = params["clienttime"]
        try:
            with self._client(headers) as client:
                response = client.post(
                    f"{GATEWAY_URL}{path}", params=params,
                    content=payload.encode("utf-8"),
                )
        except httpx.HTTPError as exc:
            raise KugouApiError(f"网络请求失败:{exc}") from exc
        return self._parse_gateway(response, path)

    def _gateway_get(
        self, path: str, router: str, extra_params: Mapping[str, Any]
    ) -> dict[str, Any]:
        params = self._android_params(extra_params)
        params["signature"] = android_signature(params)
        headers = dict(GATEWAY_HEADERS)
        headers["User-Agent"] = ANDROID_UA
        headers["x-router"] = router
        headers["dfid"] = params["dfid"]
        headers["mid"] = params["mid"]
        headers["clienttime"] = params["clienttime"]
        try:
            with self._client(headers) as client:
                response = client.get(f"{GATEWAY_URL}{path}", params=params)
        except httpx.HTTPError as exc:
            raise KugouApiError(f"网络请求失败:{exc}") from exc
        return self._parse_gateway(response, path)

    @staticmethod
    def _parse_gateway(response: httpx.Response, path: str) -> dict[str, Any]:
        """解析网关响应。

        实测区分两类失败:路径/x-router 配错返回 HTML 502(kws),签名或
        业务问题返回 JSON 错误码。这里分别给出可读提示。
        """
        if response.status_code != 200:
            raise KugouApiError(
                f"酷狗网关返回 HTTP {response.status_code}(检查 x-router 与接口路径)"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise KugouApiError("酷狗网关返回非 JSON(通常是 x-router 配错)") from exc
        if _as_int(payload.get("status")) != 1:
            code = _as_int(payload.get("error_code"))
            if code in AUTH_ERROR_CODES:
                raise KugouAuthRequiredError("酷狗登录已过期,请重新扫码登录", code=code)
            raise KugouApiError(f"酷狗接口 {path} 失败(错误码 {code})", code=code)
        return payload

    # -- 登录与续期 ---------------------------------------------------------

    def verify_login(self) -> KugouAccount | None:
        """用 v1/user_verify 校验登录态;未登录返回 None。"""
        if not self._credentials.has_login:
            return None
        try:
            payload = self._gateway_get(
                "/v1/user_verify", "trackercdngz.kugou.com", {"module_id": "51"}
            )
        except KugouAuthRequiredError:
            return None
        data = payload.get("data") or {}
        if not data.get("valid"):
            return None
        return KugouAccount(
            userid=_as_int(self._credentials.userid),
            nickname=self._credentials.nickname,
        )

    def refresh_token(self) -> KugouCredentials:
        """用旧 token 换新 token(无感续期)。失败抛 KugouAuthRequiredError。

        这是「用户不要频繁登录」的核心:扫码一次拿到 30 天 token,此后每次
        启动静默续期即可,只要间隔不超 30 天就永远不必重新扫码。
        实测续期后 t_expire_time 会往后推 30 天,可反复续。
        """
        creds = self._credentials
        if not creds.has_login:
            raise KugouAuthRequiredError("尚未登录酷狗")
        now_ms = _now_ms()
        body, session_key = build_refresh_payload(
            creds.token, creds.userid, creds.dfid, now_ms
        )
        payload_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        params = {
            "dfid": creds.dfid or "-",
            "mid": creds.mid or "-",
            "uuid": "-",
            "appid": ANDROID_APPID,
            "clientver": ANDROID_CLIENTVER,
            "clienttime": str(_now_s()),
            "userid": creds.userid,
            "token": creds.token,
        }
        params["signature"] = android_signature(params, payload_text)
        headers = dict(GATEWAY_HEADERS)
        headers["User-Agent"] = ANDROID_UA
        headers["Content-Type"] = "application/json"
        headers["dfid"] = params["dfid"]
        headers["mid"] = params["mid"]
        headers["clienttime"] = params["clienttime"]
        try:
            with self._client(headers) as client:
                response = client.post(
                    TOKEN_REFRESH_URL, params=params,
                    content=payload_text.encode("utf-8"),
                )
        except httpx.HTTPError as exc:
            raise KugouAuthRequiredError(f"续期请求失败:{exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise KugouAuthRequiredError("续期接口返回非 JSON") from exc
        if _as_int(payload.get("status")) != 1:
            raise KugouAuthRequiredError(
                "酷狗登录已过期,请重新扫码登录", code=_as_int(payload.get("error_code"))
            )
        data = payload.get("data") or {}
        secu_params = _as_str(data.get("secu_params"))
        if not secu_params:
            raise KugouAuthRequiredError("续期响应缺少 secu_params")
        try:
            new_token = _as_str(
                decode_refresh_response(secu_params, session_key).get("token")
            )
        except Exception as exc:  # noqa: BLE001 - 解密失败一律按续期失败处理
            raise KugouAuthRequiredError("续期响应解密失败") from exc
        if not new_token:
            raise KugouAuthRequiredError("续期未返回新 token")
        refreshed = KugouCredentials(
            userid=creds.userid,
            token=new_token,
            t1=_as_str(data.get("t1")) or creds.t1,
            vip_token=_as_str(data.get("vip_token")) or creds.vip_token,
            vip_type=_as_int(data.get("vip_type"), creds.vip_type),
            t_expire_time=_as_int(data.get("t_expire_time")),
            nickname=_as_str(data.get("nickname")) or creds.nickname,
            mid=creds.mid,
            dfid=creds.dfid,
        )
        self._credentials = refreshed
        logger.info(
            "酷狗 token 已续期,新过期时间 %s", refreshed.t_expire_time
        )
        if self._on_credentials_refreshed is not None:
            self._on_credentials_refreshed(refreshed)
        return refreshed

    def ensure_fresh_credentials(self) -> None:
        """启动时调用:距过期不足 3 天(或过期时间未知)就静默续期。

        续期失败静默吞掉(保持旧凭据),等真正用到接口时再由
        _with_auth_retry 抛 KugouAuthRequiredError 提示重登。
        这样网络不好时不会因续期失败就判用户需要重登。
        """
        creds = self._credentials
        if not creds.has_login:
            return
        left = creds.seconds_left(_now_s())
        if left >= REFRESH_AHEAD_SECONDS:
            return
        try:
            self.refresh_token()
        except KugouApiError as exc:
            logger.warning("酷狗 token 预续期失败(保留旧凭据):%s", exc)

    def _retry_rate_limited(self, call: Callable[[], T]) -> T:
        """限流退避重试(最多 3 次,间隔 1.2s / 2.4s)。

        实测酷狗播放接口在密集请求时会返回 30020(看起来像凭据失效),
        短暂退避即可恢复。这条与凭据续期是两件事:限流不该让用户重登,
        所以先退避,退避耗尽才把异常抛给上层。
        见 docs/research/M6-kugou-api.md §2.6 实现要求 3。
        """
        delay = 1.2
        for attempt in range(3):
            try:
                return call()
            except KugouRateLimitedError:
                if attempt == 2:
                    raise
                logger.info("酷狗播放接口限流,%.1fs 后重试(%d/3)", delay, attempt + 1)
                time.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    def _with_auth_retry(self, call: Callable[[], T]) -> T:
        """执行一次请求;遇到凭据失效则续期后重试一次。

        这是「30020 一律先续期重试」的落点(见 docs/research §2.6 实现要求 5)。
        重试仍失败时把 KugouAuthRequiredError 抛给上层提示重新扫码。
        """
        try:
            return call()
        except KugouAuthRequiredError:
            # 未登录就别白试续期了
            if not self._credentials.has_login:
                raise
            logger.info("酷狗凭据失效,尝试续期后重试")
            self.refresh_token()
            return call()

    # -- 扫码登录 -----------------------------------------------------------

    def create_qr_session(self) -> KugouQrSession:
        """取登录二维码。实测返回 data.qrcode(key) 与 data.qrcode_img(base64 PNG)。"""
        params = {
            "appid": "1001",
            "type": "1",
            "plat": "4",
            "qrcode_txt": "https://h5.kugou.com/apps/loginQRCode/html/index.html?appid=1005&",
            "srcappid": H5_SRCAPPID,
            "clienttime": str(_now_s()),
            "dfid": "-",
            "mid": "-",
            "uuid": "-",
            "clientver": H5_CLIENTVER,
        }
        params["signature"] = web_signature(params)
        try:
            with self._client() as client:
                response = client.get(QRCODE_URL, params=params)
        except httpx.HTTPError as exc:
            raise KugouApiError(f"获取二维码失败:{exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise KugouApiError("二维码接口返回非 JSON") from exc
        data = payload.get("data") or {}
        key = _as_str(data.get("qrcode"))
        if not key:
            raise KugouApiError("二维码接口未返回 key")
        raw_image = _as_str(data.get("qrcode_img"))
        image_base64 = raw_image.split(",", 1)[1] if "," in raw_image else raw_image
        return KugouQrSession(
            key=key,
            image_base64=image_base64,
            url=_as_str(data.get("qrcode_txt") or data.get("url")),
        )

    def check_qr_session(self, session: KugouQrSession) -> KugouQrCheckResult:
        """轮询扫码状态;status=4 时把 token 组装成凭据返回。

        实测凭证字段:data.token(64 位)、data.userid。设备指纹沿用本机
        cookie 里的 mid/dfid(没有则留空,后续接口按实测仍可用 "-")。
        """
        params = {
            "plat": "4",
            "appid": ANDROID_APPID,
            "srcappid": H5_SRCAPPID,
            "qrcode": session.key,
            "clienttime": str(_now_s()),
            "dfid": "-",
            "mid": "-",
            "uuid": "-",
            "clientver": H5_CLIENTVER,
        }
        params["signature"] = web_signature(params)
        try:
            with self._client() as client:
                response = client.get(QRCODE_CHECK_URL, params=params)
        except httpx.HTTPError as exc:
            raise KugouApiError(f"轮询扫码状态失败:{exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise KugouApiError("扫码状态接口返回非 JSON") from exc
        data = payload.get("data") or {}
        status = _as_int(data.get("status"))
        if status != 4:
            message = {
                0: "二维码已过期",
                1: "等待扫码",
                2: "已扫码,请在手机上确认",
            }.get(status, f"未知状态 {status}")
            return KugouQrCheckResult(status=status, message=message)
        token = _as_str(data.get("token")).strip()
        userid = _as_str(data.get("userid")).strip()
        if not token or not userid:
            raise KugouApiError("扫码成功但未返回 token/userid")
        creds = KugouCredentials(
            userid=userid,
            token=token,
            t1=_as_str(data.get("t1")),
            vip_token=_as_str(data.get("vip_token")),
            vip_type=_as_int(data.get("vip_type")),
            t_expire_time=_as_int(data.get("t_expire_time")),
            nickname=_as_str(data.get("nickname")),
            mid=self._credentials.mid,
            dfid=self._credentials.dfid,
        )
        self._credentials = creds
        return KugouQrCheckResult(status=status, credentials=creds, message="登录成功")

    # -- 搜索 ---------------------------------------------------------------

    def search_songs(
        self, keyword: str, limit: int = SEARCH_PAGE_SIZE, offset: int = 0
    ) -> tuple[list[KugouSong], int]:
        """搜索歌曲。该接口实测不校验签名,但仍按 H5 规则带上以防收紧。"""
        page = offset // max(limit, 1) + 1
        params = {
            "keyword": keyword,
            "page": str(page),
            "pagesize": str(limit),
            "platform": "WebFilter",
            "userid": self._credentials.userid or "0",
            "clientver": "2000",
            "iscorrection": "1",
            "privilege_filter": "0",
            "filter": "10",
            "area_code": "1",
            "token": self._credentials.token or "",
            "appid": "1014",
        }
        params["signature"] = web_signature(params)
        try:
            with self._client() as client:
                response = client.get(SEARCH_URL, params=params)
        except httpx.HTTPError as exc:
            raise KugouApiError(f"搜索请求失败:{exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise KugouApiError("搜索接口返回非 JSON") from exc
        if _as_int(payload.get("status")) != 1:
            raise KugouApiError(
                f"搜索失败(错误码 {_as_int(payload.get('error_code'))})"
            )
        return parse_search_response(payload)

    # -- 播放地址 -----------------------------------------------------------

    def resolve_playable_url(
        self,
        song: KugouSong,
        preferred_quality: str = DEFAULT_QUALITY,
    ) -> KugouPlayableUrl:
        """解析播放地址。

        实测要点:
        - srcappid 只能是 2919,且 token 必须同时进 query 与 cookie 的 t
        - 档位靠传不同 hash;搜索/歌单结果里没有的档先补 get_song_info()
        - 链接有时效,调用方不要缓存
        """
        hashes = dict(song.hashes)
        encode_id = song.encode_album_audio_id
        # 歌单曲目只带 128k(及 ogg_320)档,且没有 EMixSongID。要拿
        # 完整档位必须用 hash 反查搜索接口:实测 getSongInfo.php 返回的
        # 各档 hash 与播放接口不配套(拿它当 320k/flac 传会返回无 url),
        # 只有搜索结果里的档位才与播放接口一致。
        if not encode_id or len(hashes) < 3:
            matched = self._lookup_song_by_hash(song)
            if matched is not None:
                hashes = {**matched.hashes, **hashes}
                encode_id = matched.encode_album_audio_id
                if not song.album_id and matched.album_id:
                    song = KugouSong(
                        hash=song.hash,
                        title=song.title,
                        artist=song.artist,
                        duration_ms=song.duration_ms,
                        encode_album_audio_id=encode_id,
                        album_id=matched.album_id,
                        hashes=hashes,
                        cover_url=song.cover_url,
                        privilege=song.privilege,
                        pay_type=song.pay_type,
                    )
        chosen_hash, field_name = select_hash(hashes, preferred_quality)
        target_hash = chosen_hash or song.hash

        def _request() -> KugouPlayableUrl:
            extra: dict[str, Any] = {}
            if encode_id:
                extra["encode_album_audio_id"] = encode_id
            extra["hash"] = target_hash
            if song.album_id:
                extra["album_id"] = song.album_id
            params = self._h5_params(extra)
            cookies = self._play_cookies()
            try:
                with self._client() as client:
                    response = client.get(PLAY_URL, params=params, cookies=cookies)
            except httpx.HTTPError as exc:
                raise KugouApiError(f"播放地址请求失败:{exc}") from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise KugouApiError("播放接口返回非 JSON") from exc
            playable = parse_play_response(payload, preferred_quality)
            # 实际生效档位可能与请求的不同(服务端自行降档),按返回码率纠正
            if not field_name and playable.bitrate_kbps:
                for candidate, bitrate in (
                    ("FileHash", 128), ("HQFileHash", 320),
                    ("SQFileHash", 937), ("ResFileHash", 1642),
                ):
                    if abs(playable.bitrate_kbps - bitrate) <= 40:
                        playable = KugouPlayableUrl(
                            url=playable.url,
                            backup_url=playable.backup_url,
                            bitrate_kbps=playable.bitrate_kbps,
                            quality_key=candidate,
                            duration_ms=playable.duration_ms,
                        )
                        break
            return playable

        return self._with_auth_retry(lambda: self._retry_rate_limited(_request))

    def _lookup_song_by_hash(self, song: KugouSong) -> KugouSong | None:
        """用 hash 反查搜索接口,拿到完整档位与 EMixSongID。

        为什么不用 getSongInfo.php:实测它返回的各档 hash 与播放接口
        不配套(传 320k/flac hash 会返回 status=1 但 play_url 为空),
        而搜索结果里的档位与播放接口一致。歌单曲目只带 128k 档,
        想播更高音质必须走这条路。

        匹配判据是 FileHash 相等(酷狗 hash 全局唯一),不做标题模糊匹配,
        避免匹配到同名翻唱。查不到返回 None,调用方回落已知 hash。
        """
        target = (song.hash or "").upper()
        if not target:
            return None
        # 歌单曲目的 name 常是「歌手 - 歌名」形式,直接拼 artist 会把
        # 关键词变成「歌手 - 歌名 歌手」,实测搜不到。按由窄到宽依次试:
        # 纯标题 → 去掉「歌手 - 」前缀的标题 → 标题 + 歌手。
        title = (song.title or "").strip()
        candidates_keywords: list[str] = []
        if title:
            candidates_keywords.append(title)
            if " - " in title:
                candidates_keywords.append(title.split(" - ", 1)[1].strip())
            if song.artist:
                candidates_keywords.append(f"{title} {song.artist}")
        for keyword in candidates_keywords:
            if not keyword:
                continue
            try:
                candidates, _ = self.search_songs(keyword, limit=10)
            except KugouApiError:
                continue
            for candidate in candidates:
                if (candidate.hashes.get("FileHash") or "").upper() == target:
                    return candidate
        return None

    def get_song_info(self, file_hash: str) -> dict[str, str]:
        """取各档 hash。该接口无鉴权,失败不抛(调用方按可选处理)。"""
        if not file_hash:
            return {}
        try:
            with self._client() as client:
                response = client.get(
                    SONG_INFO_URL, params={"cmd": "playInfo", "hash": file_hash}
                )
        except httpx.HTTPError as exc:
            raise KugouApiError(f"歌曲信息请求失败:{exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            return {}
        if not isinstance(payload, Mapping):
            return {}
        return parse_song_info_response(payload)

    # -- 歌单 ---------------------------------------------------------------

    def get_user_playlists(self) -> list[KugouPlaylist]:
        """读取用户全部自建歌单(含「我喜欢」)。

        实测:type=2 返回全部自建歌单;info[] 里 is_def==2 的是「我喜欢」。
        """
        def _request() -> list[KugouPlaylist]:
            body = {
                "userid": _as_int(self._credentials.userid),
                "token": self._credentials.token,
                "total_ver": 979,
                "type": 2,
                "page": 1,
                "pagesize": 100,
            }
            payload = self._gateway_post(
                "/v7/get_all_list", "cloudlist.service.kugou.com", body,
                {"plat": "1", "userid": self._credentials.userid,
                 "token": self._credentials.token},
            )
            return parse_playlist_list_response(payload)

        if not self._credentials.has_login:
            raise KugouAuthRequiredError("尚未登录酷狗")
        return self._with_auth_retry(_request)

    def get_playlist_tracks(self, playlist: KugouPlaylist) -> list[KugouSong]:
        """读取歌单曲目(单页 300 首,实测足够覆盖个人歌单)。"""
        def _request() -> list[KugouSong]:
            body = {
                "listid": playlist.listid,
                "userid": self._credentials.userid,
                "type": 0,
                "page": 1,
                "pagesize": PLAYLIST_TRACK_PAGE_SIZE,
                "area_code": 1,
                "allplatform": 1,
                "show_cover": 1,
                "token": self._credentials.token,
            }
            payload = self._gateway_post(
                "/v4/get_list_all_file_v3", "cloudlist.service.kugou.com", body
            )
            return parse_playlist_tracks_response(payload)

        if not self._credentials.has_login:
            raise KugouAuthRequiredError("尚未登录酷狗")
        return self._with_auth_retry(_request)

    def close(self) -> None:
        """与两源一致:本客户端每请求现开 Client,无长期连接需释放。"""
