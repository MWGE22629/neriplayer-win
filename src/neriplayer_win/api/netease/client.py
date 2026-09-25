"""网易云客户端(同步 httpx 实现)。

逐字对照 reference/NeriPlayer-Android 翻译:

- app/src/main/java/moe/ouom/neriplayer/core/api/netease/NeteaseClient.kt
  (请求管线、cookie 合并、weapi/eapi/linuxapi 调用、歌单/歌曲/播放地址;
  歌单分流 created/subscribed 对照 getUserCreatedPlaylists /
  getUserSubscribedPlaylists)
- app/src/main/java/moe/ouom/neriplayer/core/api/netease/NeteaseQrLoginClient.kt
  (扫码登录:unikey 申请、二维码内容、轮询、803 后的 cookie 校验)
- app/src/main/java/moe/ouom/neriplayer/core/player/resolver/netease/
  NeteasePlaybackResponseParser.kt(播放地址解析与 VIP/试听识别)
- app/src/main/java/moe/ouom/neriplayer/core/player/url/PlayerUrlResolver.kt
  (音质回退顺序 buildNeteaseQualityCandidates)
- app/src/main/java/moe/ouom/neriplayer/ui/viewmodel/playlist/
  NeteaseCollectionDetailViewModel.kt(歌单详情/歌曲解析、trackIds 补拉)

与 Kotlin 版的取舍(偏差点):
- 单账号桌面场景,省略 NeteaseRequestSessionStore 的多会话指纹换栈逻辑,
  只保留 replace_persisted_cookies 时重置运行时 cookie 的行为。
- okhttp CookieJar 换成自管的域 cookie 存储,请求时手工拼 Cookie 头;
  httpx 显式 Cookie 头优先级高于其内置 jar,行为确定。
- Accept-Encoding 交给 httpx 自动协商(gzip/deflate),不用 brotli。
- ydDeviceToken 依赖 Android WebView 指纹,桌面端没有;按 Kotlin 中
  provider 失败时的回退路径处理:token 为空串、deviceId 用 unknown-<rand>。
"""

from __future__ import annotations

import json
import random
import threading
import time
from http.cookies import CookieError, SimpleCookie
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse
from urllib.request import getproxies

import httpx

from ...log import get_logger
from . import crypto
from .models import (
    NeteaseAccount,
    NeteaseApiError,
    NeteaseAuthRequiredError,
    NeteaseNoPermissionError,
    NeteaseNoPlayUrlError,
    NeteasePlaylist,
    NeteaseSong,
    NeteaseUserPlaylists,
    NeteaseYdSnapshot,
    PlayableUrl,
    QrLoginCheckResult,
    QrLoginSession,
)

NETEASE_MAIN_HOST = "music.163.com"
NETEASE_REQUEST_OS = "pc"
NETEASE_REQUEST_APP_VERSION = "8.10.35"

_log = get_logger("netease")

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# Kotlin NeteaseClient 的默认请求头
_CLIENT_UA = (
    "Mozilla/5.0 (Linux; Android 10; NeriPlayer) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)

# Kotlin NeteaseQrLoginClient 的桌面 UA 与请求头
_QR_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
)
_QR_ORIGIN = "https://music.163.com/"
_QR_UNIKEY_PATH = "/weapi/login/qrcode/unikey"
_QR_CHECK_PATH = "/weapi/login/qrcode/client/login"
_QR_ACCOUNT_PATH = "/weapi/w/nuser/account/get"
_QR_REFRESH_TOKEN_HEADER = "x-refresh-token"
_QR_REFRESH_TOKEN_COOKIE = "MUSIC_U"

# PlayerUrlResolver.kt 的 NETEASE_QUALITY_FALLBACK_ORDER
QUALITY_FALLBACK_ORDER = [
    "jymaster",
    "sky",
    "jyeffect",
    "hires",
    "lossless",
    "exhigh",
    "higher",
    "standard",
]
DEFAULT_QUALITY = "lossless"

# 播放响应解析结果(对应 NeteasePlaybackResponseParser 的 PlaybackResult)
_PLAY_REQUIRES_LOGIN = "requires-login"
_PLAY_FAIL_NO_PERMISSION = "no-permission"
_PLAY_FAIL_NO_PLAY_URL = "no-play-url"
_PLAY_FAIL_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 模块级纯函数(与 Kotlin internal fun 一一对应,便于单测)
# ---------------------------------------------------------------------------

def merge_netease_request_cookies(
    persisted_cookies: Mapping[str, str],
    runtime_cookies: Mapping[str, str],
    request_context_cookies: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """对应 mergeNeteaseRequestCookies:context → persisted → runtime,后写覆盖。"""
    merged: dict[str, str] = {}
    for source in (request_context_cookies or {}, persisted_cookies, runtime_cookies):
        for name, value in source.items():
            if name.strip() and value.strip():
                merged[name] = value
    merged.setdefault("os", NETEASE_REQUEST_OS)
    merged.setdefault("appver", NETEASE_REQUEST_APP_VERSION)
    return merged


def should_preheat_netease_weapi_session(
    persisted_cookies: Mapping[str, str],
    request_cookies: Mapping[str, str],
    use_persisted_cookies: bool,
) -> bool:
    """对应 shouldPreheatNeteaseWeapiSession。"""
    return (
        use_persisted_cookies
        and bool(persisted_cookies.get("MUSIC_U"))
        and not request_cookies.get("__csrf")
    )


def build_qr_account_params(csrf: str) -> dict[str, Any]:
    """对应 buildNeteaseQrAccountParams。"""
    params: dict[str, Any] = {"noCheckToken": True}
    if csrf.strip():
        params["csrf_token"] = csrf
    return params


def merge_qr_credential_cookies(
    cookies: Mapping[str, str], refresh_token: str
) -> dict[str, str]:
    """对应 mergeNeteaseQrCredentialCookies。"""
    merged: dict[str, str] = {}
    for key, value in cookies.items():
        if key.strip() and value.strip():
            merged[key] = value
    if not merged.get(_QR_REFRESH_TOKEN_COOKIE, "").strip() and refresh_token.strip():
        merged[_QR_REFRESH_TOKEN_COOKIE] = refresh_token
    return merged


def build_quality_candidates(preferred_quality: str) -> list[str]:
    """对应 buildNeteaseQualityCandidates。"""
    normalized = preferred_quality.strip().lower() or "exhigh"
    try:
        preferred_index = QUALITY_FALLBACK_ORDER.index(normalized)
    except ValueError:
        candidates = [normalized, "exhigh", "standard"]
        return list(dict.fromkeys(candidates))
    return QUALITY_FALLBACK_ORDER[preferred_index:]


def _opt_clean_string(obj: Mapping[str, Any], key: str) -> str | None:
    """对应 NeteasePlaybackResponseParser.optCleanString(排除 JSON null)。"""
    if key not in obj:
        return None
    raw = obj[key]
    if raw is None:
        return None
    value = raw if isinstance(raw, str) else str(raw)
    value = value.strip()
    if not value or value.lower() == "null":
        return None
    return value


def _opt_int_or_none(obj: Mapping[str, Any], key: str) -> int | None:
    raw = obj.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_playback_response(
    raw_response: str, original_duration_ms: int = 0
) -> dict[str, Any]:
    """解析播放地址响应,对应 NeteasePlaybackResponseParser.parsePlayback。

    返回 dict:
    - {"kind": "requires-login"}
    - {"kind": "success", "url", "level", "type", "is_preview", ...}
    - {"kind": "failure", "reason": no-permission / no-play-url / unknown}
    """
    try:
        root = json.loads(raw_response)
    except (json.JSONDecodeError, TypeError):
        return {"kind": "failure", "reason": _PLAY_FAIL_UNKNOWN}
    if not isinstance(root, dict):
        return {"kind": "failure", "reason": _PLAY_FAIL_UNKNOWN}

    code = _opt_int_or_none(root, "code") or -1
    if code == 301:
        return {"kind": _PLAY_REQUIRES_LOGIN}
    if code != 200:
        return {"kind": "failure", "reason": _PLAY_FAIL_UNKNOWN}

    data = root.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return {"kind": "failure", "reason": _PLAY_FAIL_NO_PLAY_URL}

    url = _opt_clean_string(data, "url")
    if not url:
        # classifyFailure:data.code==404 / cannotListenReason==1 / fee>0 → 无权限
        data_code = _opt_int_or_none(data, "code") or -1
        free_trial = data.get("freeTrialPrivilege")
        cannot_listen_reason = None
        if isinstance(free_trial, dict):
            cannot_listen_reason = _opt_int_or_none(free_trial, "cannotListenReason")
        fee = _opt_int_or_none(data, "fee") or 0
        if data_code == 404 or cannot_listen_reason == 1 or fee > 0:
            return {"kind": "failure", "reason": _PLAY_FAIL_NO_PERMISSION}
        return {"kind": "failure", "reason": _PLAY_FAIL_NO_PLAY_URL}

    free_trial_info = data.get("freeTrialInfo")
    is_preview = free_trial_info is not None and original_duration_ms >= 0

    return {
        "kind": "success",
        "url": url,
        "type": _opt_clean_string(data, "type"),
        "level": _opt_clean_string(data, "level"),
        "is_preview": is_preview,
        "size": _opt_int_or_none(data, "size"),
    }


def _parse_song_item(track: Mapping[str, Any]) -> NeteaseSong | None:
    """对应 NeteaseCollectionDetailViewModel.parseSongItem(M1 所需字段)。"""
    song_id = track.get("id") or 0
    name = track.get("name") or ""
    if not song_id or not str(name).strip():
        return None
    artists = track.get("ar")
    if not isinstance(artists, list):
        artists = track.get("artists") or []
    names = []
    for artist in artists:
        if isinstance(artist, dict) and artist.get("name"):
            names.append(str(artist["name"]))
    duration_ms = int(track.get("dt") or track.get("duration") or 0)
    album = track.get("al")
    cover_url = ""
    if isinstance(album, dict):
        cover_url = str(album.get("picUrl") or "")
    return NeteaseSong(
        id=int(song_id),
        title=str(name),
        artist=" / ".join(names),
        duration_ms=duration_ms,
        cover_url=cover_url,
    )


def first_recommended_song_array(root: Mapping[str, Any]) -> list[Any]:
    """推荐类接口响应里的歌曲数组(模块级纯函数,便于单测)。

    回退链对照参考实现 NeteaseHomeRecommendations.kt 的 firstSongArray:
    每日推荐(data.dailySongs)、私人 FM(data)、推荐歌单详情等不同接口
    的落位不同,逐级探测;都缺失时返回空列表。
    """
    data = root.get("data")
    candidates = [
        data.get("dailySongs") if isinstance(data, dict) else None,
        data.get("songs") if isinstance(data, dict) else None,
        data if isinstance(data, list) else None,
        root.get("result"),
        root.get("songs"),
        root.get("playlist").get("tracks") if isinstance(root.get("playlist"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate
    return []


def parse_recommended_songs_response(raw_response: str) -> list[NeteaseSong]:
    """解析每日推荐等推荐类接口响应;301 抛 NeteaseAuthRequiredError。

    歌曲条目复用 _parse_song_item(ar/al/dt 字段与歌单详情一致)。
    """
    try:
        root = json.loads(raw_response)
    except (json.JSONDecodeError, TypeError):
        raise NeteaseApiError("推荐接口响应不是合法 JSON")
    if not isinstance(root, dict):
        raise NeteaseApiError("推荐接口响应格式异常")
    code = root.get("code", -1)
    if code == 301:
        raise NeteaseAuthRequiredError("登录态已失效,请重新扫码登录")
    if code != 200:
        raise NeteaseApiError(f"获取每日推荐失败: code={code}")
    songs: list[NeteaseSong] = []
    for item in first_recommended_song_array(root):
        if isinstance(item, dict):
            song = _parse_song_item(item)
            if song is not None:
                songs.append(song)
    return songs


def split_user_playlists(items: Any, user_id: int) -> NeteaseUserPlaylists:
    """user/playlist 的 playlist 数组按归属分流(模块级纯函数,便于单测)。

    created 过滤规则对应 getUserCreatedPlaylists(creatorId==uid 或未订阅),
    「我喜欢的音乐」置前并打 is_liked;其余(subscribed==true 且 creator
    !=uid)归入 subscribed,对应 getUserSubscribedPlaylists 的语义——
    Kotlin 版两个过滤器独立调用时理论上会把「自己创建且已订阅」的歌单
    双算,此处按并集划分保证条目不重复。
    """
    liked: NeteasePlaylist | None = None
    created: list[NeteasePlaylist] = []
    subscribed: list[NeteasePlaylist] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        creator = item.get("creator") or {}
        creator_id = creator.get("userId") or -1
        is_subscribed = bool(item.get("subscribed", False))
        playlist = NeteasePlaylist(
            id=int(item.get("id") or 0),
            name=str(item.get("name") or ""),
            track_count=int(item.get("trackCount") or 0),
            special_type=int(item.get("specialType") or 0),
        )
        if creator_id == user_id or not is_subscribed:
            is_liked = creator_id == user_id and (
                playlist.special_type == 5 or "我喜欢的音乐" in playlist.name
            )
            if is_liked and liked is None:
                liked = NeteasePlaylist(
                    id=playlist.id,
                    name="我喜欢的音乐",
                    track_count=playlist.track_count,
                    special_type=playlist.special_type,
                    is_liked=True,
                )
            elif is_liked:
                # 后续同名特殊歌单不再重复(与 getLikedPlaylistId 的 break 语义一致)
                continue
            else:
                created.append(playlist)
        else:
            subscribed.append(playlist)
    ordered: list[NeteasePlaylist] = []
    if liked is not None:
        ordered.append(liked)
    ordered.extend(created)
    return NeteaseUserPlaylists(created=ordered, subscribed=subscribed)


# ---------------------------------------------------------------------------
# 运行时 cookie 存储(对应 Kotlin 的 CookieJar 实现,简化为域匹配)
# ---------------------------------------------------------------------------

class _CookieStore:
    """按域存储的运行时 cookie,行为对齐 okhttp CookieJar 的存取语义。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # domain -> list[(name, value, path, expires_at)]
        self._store: dict[str, list[tuple[str, str, str, float]]] = {}

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def seed_domain_cookies(self, host: str, cookies: Mapping[str, str]) -> None:
        """对应 seedCookieJarFromPersistedLocked:把快照种成域 Cookie。"""
        with self._lock:
            bucket = self._store.setdefault(host, [])
            for name, value in cookies.items():
                self._remove_identity_locked(bucket, name)
                bucket.append((name, value, "/", float("inf")))

    def _remove_identity_locked(
        self, bucket: list[tuple[str, str, str, float]], name: str
    ) -> None:
        bucket[:] = [entry for entry in bucket if entry[0] != name]

    def save_from_response(self, url: str, set_cookie_values: list[str]) -> None:
        """对应 saveFromResponse + Cookie.isUsableCookie;url 为请求地址。"""
        parsed_host = urlparse(url)
        host = parsed_host.hostname or ("" if "://" in url else url)
        now = time.time()
        with self._lock:
            for header in set_cookie_values:
                parsed = SimpleCookie()
                try:
                    parsed.load(header)
                except (CookieError, AttributeError):
                    continue
                for morsel in parsed.values():
                    if not morsel.value or not morsel.value.strip():
                        continue
                    domain = morsel["domain"] or host
                    bucket = self._store.setdefault(domain, [])
                    self._remove_identity_locked(bucket, morsel.key)
                    expires_at = float("inf")
                    if morsel["max-age"]:
                        try:
                            expires_at = now + int(morsel["max-age"])
                        except ValueError:
                            pass
                    bucket.append((morsel.key, morsel.value, morsel["path"] or "/", expires_at))

    def cookies_for_url(self, url: str) -> dict[str, str]:
        """对应 requestCookiesForUrlLocked:按域/路径匹配,排序后同名后者覆盖。"""
        now = time.time()
        parsed_url = urlparse(url)
        host = parsed_url.hostname or ""
        path = parsed_url.path or "/"
        matches: list[tuple[int, int, str, str]] = []
        with self._lock:
            for domain, bucket in self._store.items():
                # 过期清除(evictExpiredCookiesLocked)
                bucket[:] = [e for e in bucket if e[3] > now]
                if not self._domain_matches(host, domain):
                    continue
                for name, value, cookie_path, _ in bucket:
                    if path.startswith(cookie_path):
                        matches.append((len(domain), len(cookie_path), name, value))
        matches.sort(key=lambda item: (item[0], item[1], item[2]))
        result: dict[str, str] = {}
        for _, _, name, value in matches:
            result[name] = value
        return result

    @staticmethod
    def _domain_matches(host: str, domain: str) -> bool:
        if not domain:
            return False
        if domain.startswith("."):
            return host.endswith(domain) or host == domain[1:]
        return host == domain or host.endswith("." + domain)


class _RequestSession:
    """对应 NeteaseRequestSession(单账号简化版)。"""

    def __init__(self, persisted_cookies: Mapping[str, str]) -> None:
        self._lock = threading.Lock()
        self._persisted = dict(persisted_cookies)
        self._runtime = _CookieStore()
        self._context_cookies = {
            "__remember_me": "true",
            "_ntes_nuid": crypto.new_session_cookie_value(),
            "NMTID": crypto.new_session_cookie_value(),
        }
        self._seed_persisted()

    def seed_runtime_cookies(self, host: str, cookies: Mapping[str, str]) -> None:
        """把指纹页等运行期 Cookie 种进会话(对应 seedCookieJarFromSnapshot)。"""
        self._runtime.seed_domain_cookies(host, cookies)

    def _seed_persisted(self) -> None:
        self._runtime.seed_domain_cookies(NETEASE_MAIN_HOST, self._persisted)
        self._runtime.seed_domain_cookies("interface.music.163.com", self._persisted)

    def has_login(self) -> bool:
        with self._lock:
            return bool(self._persisted.get("MUSIC_U"))

    def persisted_snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._persisted)

    def replace_persisted(self, snapshot: Mapping[str, str]) -> None:
        with self._lock:
            self._persisted = dict(snapshot)
            self._runtime.clear()
            self._seed_persisted()

    def save_response_cookies(self, url: str, set_cookie_values: list[str]) -> None:
        self._runtime.save_from_response(url, set_cookie_values)

    def request_cookies_for_url(self, url: str) -> dict[str, str]:
        with self._lock:
            return merge_netease_request_cookies(
                persisted_cookies=self._persisted,
                runtime_cookies=self._runtime.cookies_for_url(url),
                request_context_cookies=self._context_cookies,
            )

    def cookies_snapshot(self) -> dict[str, str]:
        """对应 cookiesSnapshot:全部运行时 cookie 摊平。"""
        return self._runtime.cookies_for_url(f"https://{NETEASE_MAIN_HOST}/")

    def needs_weapi_session_preheat(self, url: str, use_persisted_cookies: bool) -> bool:
        return should_preheat_netease_weapi_session(
            persisted_cookies=self.persisted_snapshot(),
            request_cookies=self.request_cookies_for_url(url),
            use_persisted_cookies=use_persisted_cookies,
        )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class NeteaseClient:
    """网易云 API 客户端;线程安全,UI 侧放后台线程调用。"""

    def __init__(self, timeout: httpx.Timeout | None = None) -> None:
        self._timeout = timeout or httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=30.0)
        self._http = httpx.Client(timeout=self._timeout, follow_redirects=True)
        # 直连兜底客户端(语义同 BiliClient._get_with_direct_fallback):
        # 代理残留导致连接类失败时直连重试一次;网易虽可能被海外用户
        # 反向依赖代理,但重试失败仍抛原始异常,对这类用户零副作用
        self._http_direct = httpx.Client(
            timeout=self._timeout, follow_redirects=True, trust_env=False
        )
        self._session = _RequestSession({})
        # 扫码登录使用独立的 cookie 存储(对应 Kotlin 里独立的 NeteaseQrLoginClient)
        self._qr_cookie_store = _CookieStore()
        self._qr_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()
        self._http_direct.close()

    # -- 登录态(cookie)管理 --------------------------------------------------

    def has_login(self) -> bool:
        return self._session.has_login()

    def set_persisted_cookies(self, cookies: Mapping[str, str]) -> None:
        normalized = dict(cookies)
        normalized.setdefault("os", NETEASE_REQUEST_OS)
        normalized.setdefault("appver", NETEASE_REQUEST_APP_VERSION)
        self._session.replace_persisted(normalized)

    def get_cookies(self) -> dict[str, str]:
        return self._session.cookies_snapshot()

    def logout(self) -> None:
        self._session.replace_persisted({})

    # -- 请求管线(对应 request/buildRequest/executeRequest) ------------------

    def _ensure_weapi_session_if_needed(self, mode: str, use_persisted_cookies: bool) -> None:
        if mode != "WEAPI":
            return
        main_url = f"https://{NETEASE_MAIN_HOST}/"
        if not self._session.needs_weapi_session_preheat(main_url, use_persisted_cookies):
            return
        self.ensure_weapi_session()
        if self._session.needs_weapi_session_preheat(main_url, use_persisted_cookies):
            raise NeteaseApiError("网易云会话预热未能获取 __csrf,请稍后重试")

    def ensure_weapi_session(self) -> None:
        """访问一次站点首页,通常会下发 __csrf 等 Cookie。"""
        self.request(
            url=f"https://{NETEASE_MAIN_HOST}/",
            params={},
            mode="API",
            method="GET",
            use_persisted_cookies=True,
        )

    def _build_request(
        self,
        url: str,
        params: Mapping[str, Any],
        mode: str,
        method: str,
        use_persisted_cookies: bool,
    ) -> httpx.Request:
        parsed = urlparse(url)
        if mode == "WEAPI":
            body_params: dict[str, str] = crypto.weapi_encrypt(params)
        elif mode == "EAPI":
            body_params = crypto.eapi_encrypt(parsed.path, params)
        elif mode == "LINUX":
            body_params = crypto.linuxapi_encrypt(params)
        else:  # API
            body_params = {k: str(v) for k, v in params.items()}

        headers = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Connection": "keep-alive",
            "Referer": "https://music.163.com",
            "Host": parsed.netloc,
            "User-Agent": _CLIENT_UA,
        }

        # WEAPI 的 csrf_token 优先用持久化 Cookie(对应 buildRequest)
        query = ""
        if mode == "WEAPI":
            csrf = ""
            if use_persisted_cookies:
                csrf = self._session.request_cookies_for_url(url).get("__csrf", "")
            separator = "&" if parsed.query else "?"
            query = f"{separator}{urlencode({'csrf_token': csrf})}"

        request_url = f"{url}{query}"
        cookie_header = "; ".join(
            f"{k}={v}"
            for k, v in self._session.request_cookies_for_url(url).items()
        )
        headers["Cookie"] = cookie_header

        if method.upper() == "POST":
            return self._http.build_request(
                "POST", request_url, data=body_params, headers=headers
            )
        if method.upper() == "GET":
            extra = urlencode(body_params)
            if extra:
                request_url += ("&" if query else "?") + extra
            return self._http.build_request("GET", request_url, headers=headers)
        raise NeteaseApiError(f"不支持的请求方法: {method}")

    def _send_with_direct_fallback(self, request: httpx.Request) -> httpx.Response:
        """走代理路径的连接类失败(ConnectError/ConnectTimeout)时直连重试一次。

        语义同 BiliClient._get_with_direct_fallback:残留的失效代理配置
        (WinError 10061)不该拖死全部请求;重发同一 request 安全——
        本客户端请求体均为 bytes,httpx 对应的 ByteStream 可重复消费。
        """
        try:
            return self._http.send(request)
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            if not getproxies():
                raise
            _log.warning(
                "代理路径连接失败,降级直连重试 url=%s(%s: %s)",
                request.url, error.__class__.__name__, error,
            )
            try:
                return self._http_direct.send(request)
            except httpx.HTTPError as direct_error:
                _log.warning(
                    "直连重试仍失败 url=%s(%s: %s)",
                    request.url, direct_error.__class__.__name__, direct_error,
                )
                raise error from direct_error

    def _send(self, request: httpx.Request) -> httpx.Response:
        response = self._send_with_direct_fallback(request)
        # 手工采集 Set-Cookie 进自管存储(对齐 CookieJar.saveFromResponse)
        self._session.save_response_cookies(
            str(request.url), response.headers.get_list("set-cookie")
        )
        return response

    @staticmethod
    def _read_response(response: httpx.Response) -> str:
        content = response.content
        if len(content) > _MAX_RESPONSE_BYTES:
            raise NeteaseApiError("网易云接口响应超过 4MB 限制")
        text = content.decode("utf-8", errors="replace")
        if response.status_code < 200 or response.status_code >= 300:
            raise NeteaseApiError(
                f"HTTP {response.status_code}: {text[:200]}", code=response.status_code
            )
        return text

    def request(
        self,
        url: str,
        params: Mapping[str, Any],
        mode: str = "WEAPI",
        method: str = "POST",
        use_persisted_cookies: bool = True,
    ) -> str:
        self._ensure_weapi_session_if_needed(mode, use_persisted_cookies)
        request = self._build_request(url, params, mode, method, use_persisted_cookies)
        try:
            response = self._send(request)
        except httpx.HTTPError as error:
            _log.warning(
                "网易云网络请求失败 url=%s proxies=%r(%s: %s)",
                url, dict(getproxies()), error.__class__.__name__, error,
            )
            raise NeteaseApiError(f"网络请求失败: {url}({error.__class__.__name__}: {error})") from error
        return self._read_response(response)

    def call_weapi(self, path: str, params: Mapping[str, Any], use_persisted_cookies: bool = True) -> str:
        p = path if path.startswith("/") else f"/{path}"
        return self.request(
            f"https://{NETEASE_MAIN_HOST}/weapi{p}", params, "WEAPI", "POST", use_persisted_cookies
        )

    def call_eapi(self, path: str, params: Mapping[str, Any], use_persisted_cookies: bool = True) -> str:
        p = path if path.startswith("/") else f"/{path}"
        return self.request(
            "https://interface.music.163.com/eapi" + p, params, "EAPI", "POST", use_persisted_cookies
        )

    def call_linuxapi(self, path: str, params: Mapping[str, Any], use_persisted_cookies: bool = True) -> str:
        p = path if path.startswith("/") else f"/{path}"
        return self.request(
            f"https://{NETEASE_MAIN_HOST}/api{p}", params, "LINUX", "POST", use_persisted_cookies
        )

    # -- 账号 ----------------------------------------------------------------

    def get_current_user_account_raw(self) -> str:
        """对应 getCurrentUserAccount:/w/nuser/account/get。"""
        return self.call_weapi("/w/nuser/account/get", {})

    def get_login_status(self) -> NeteaseAccount | None:
        """cookie 是否有效;有效时返回账号信息,未登录返回 None。

        网络失败抛 NeteaseApiError,由调用方决定提示方式。
        """
        raw = self.get_current_user_account_raw()
        root = json.loads(raw)
        code = root.get("code", -1)
        if code == 301:
            return None
        if code != 200:
            raise NeteaseApiError(f"获取用户信息失败: code={code}")
        profile = root.get("profile")
        if not isinstance(profile, dict):
            return None
        user_id = profile.get("userId")
        if not user_id:
            return None
        return NeteaseAccount(user_id=int(user_id), nickname=str(profile.get("nickname") or ""))

    def get_current_user_id(self) -> int:
        """对应 getCurrentUserId。"""
        raw = self.get_current_user_account_raw()
        root = json.loads(raw)
        if root.get("code", -1) != 200:
            raise NeteaseApiError(f"获取用户信息失败: code={root.get('code')}")
        profile = root.get("profile") or {}
        user_id = profile.get("userId")
        if not user_id:
            raise NeteaseApiError("未找到 userId")
        return int(user_id)

    # -- 歌单 ----------------------------------------------------------------

    def get_user_playlists_raw(self, user_id: int, offset: int = 0, limit: int = 30) -> str:
        """对应 getUserPlaylists。"""
        return self.request(
            f"https://{NETEASE_MAIN_HOST}/weapi/user/playlist",
            {
                "uid": str(user_id),
                "offset": str(offset),
                "limit": str(limit),
                "includeVideo": "true",
            },
            "WEAPI",
            "POST",
            True,
        )

    def get_user_playlists_grouped(self, user_id: int, limit: int = 1000) -> NeteaseUserPlaylists:
        """用户歌单按归属分流:created(liked 置前)+ subscribed(收藏的他人歌单)。

        对应 getUserCreatedPlaylists / getUserSubscribedPlaylists 共用的
        getUserPlaylists 响应,一次请求两份视图。
        """
        raw = self.get_user_playlists_raw(user_id, 0, limit)
        root = json.loads(raw)
        if root.get("code", 200) != 200:
            raise NeteaseApiError(f"获取歌单列表失败: code={root.get('code')}")
        return split_user_playlists(root.get("playlist") or [], user_id)

    def get_user_playlists(self, user_id: int, limit: int = 1000) -> list[NeteasePlaylist]:
        """用户自建歌单列表(getUserCreatedPlaylists 语义的兼容入口)。"""
        return self.get_user_playlists_grouped(user_id, limit).created

    def get_liked_playlist_id(self, user_id: int) -> int | None:
        """对应 getLikedPlaylistId。"""
        raw = self.get_user_playlists_raw(user_id, 0, 1000)
        root = json.loads(raw)
        for item in root.get("playlist") or []:
            if not isinstance(item, dict):
                continue
            special_type = int(item.get("specialType") or 0)
            name = str(item.get("name") or "")
            creator_id = (item.get("creator") or {}).get("userId") or -1
            if creator_id == user_id and (special_type == 5 or "我喜欢的音乐" in name):
                return int(item.get("id") or 0)
        return None

    def get_playlist_detail_raw(self, playlist_id: int, n: int = 100000, s: int = 8) -> str:
        """对应 getPlaylistDetail(/api/v6/playlist/detail,API 模式)。"""
        return self.request(
            f"https://{NETEASE_MAIN_HOST}/api/v6/playlist/detail",
            {"id": str(playlist_id), "n": str(n), "s": str(s)},
            "API",
            "POST",
            True,
        )

    def get_playlist_tracks(self, playlist_id: int) -> list[NeteaseSong]:
        """歌单内歌曲列表;trackIds 多于 tracks 时按 300/批补拉详情。

        对应 parseDetailFromPlaylist + fetchFullPlaylistTracks。
        """
        raw = self.get_playlist_detail_raw(playlist_id)
        root = json.loads(raw)
        code = root.get("code", -1)
        if code != 200:
            raise NeteaseApiError(f"获取歌单详情失败: code={code}")
        playlist = root.get("playlist")
        if not isinstance(playlist, dict):
            raise NeteaseApiError("歌单详情响应缺少 playlist 节点")

        tracks: list[NeteaseSong] = []
        by_id: dict[int, NeteaseSong] = {}
        for item in playlist.get("tracks") or []:
            song = _parse_song_item(item)
            if song is not None:
                tracks.append(song)
                by_id[song.id] = song

        track_ids: list[int] = []
        for item in playlist.get("trackIds") or []:
            if isinstance(item, dict) and item.get("id"):
                track_ids.append(int(item["id"]))

        if track_ids and len(track_ids) > len(tracks):
            existing = set(by_id)
            missing = [tid for tid in track_ids if tid not in existing]
            for start in range(0, len(missing), 300):
                page = missing[start : start + 300]
                for song in self.get_song_detail(page):
                    by_id[song.id] = song
            return [by_id[tid] for tid in track_ids if tid in by_id]
        return tracks

    def get_song_detail(self, ids: list[int]) -> list[NeteaseSong]:
        """对应 getSongDetail(/weapi/v3/song/detail)。"""
        if not ids:
            raise NeteaseApiError("getSongDetail: ids 不能为空")
        ids_param = ",".join(str(i) for i in ids)
        detail_param = ",".join(f'{{"id":{i}}}' for i in ids)
        raw = self.request(
            f"https://{NETEASE_MAIN_HOST}/weapi/v3/song/detail",
            {"c": f"[{detail_param}]", "ids": f"[{ids_param}]"},
            "WEAPI",
            "POST",
            True,
        )
        root = json.loads(raw)
        if root.get("code", -1) != 200:
            raise NeteaseApiError(f"获取歌曲详情失败: code={root.get('code')}")
        songs: list[NeteaseSong] = []
        for item in root.get("songs") or []:
            song = _parse_song_item(item)
            if song is not None:
                songs.append(song)
        return songs

    # -- 搜索 ----------------------------------------------------------------

    def search_songs_raw(self, keyword: str, limit: int = 30, offset: int = 0) -> str:
        """对应 searchSongs(/weapi/cloudsearch/get/web,type=1 单曲)。"""
        return self.call_weapi(
            "/cloudsearch/get/web",
            {
                "s": keyword,
                "type": "1",
                "limit": str(limit),
                "offset": str(offset),
                "total": "true",
            },
        )

    def search_songs(
        self, keyword: str, limit: int = 30, offset: int = 0
    ) -> tuple[list[NeteaseSong], int]:
        """关键词搜单曲;返回 (歌曲列表, 命中总数 songCount)。

        响应 result.songs[] 的 ar/al/dt 字段与歌单详情一致,条目复用
        _parse_song_item(对应 NeteaseSearchSongParser 的单曲分支)。
        """
        root = json.loads(self.search_songs_raw(keyword, limit, offset))
        code = root.get("code", -1)
        if code != 200:
            raise NeteaseApiError(f"搜索失败: code={code}")
        result = root.get("result")
        if not isinstance(result, dict):
            raise NeteaseApiError("搜索响应缺少 result 节点")
        songs: list[NeteaseSong] = []
        for item in result.get("songs") or []:
            song = _parse_song_item(item)
            if song is not None:
                songs.append(song)
        return songs, int(result.get("songCount") or 0)

    # -- 每日推荐 ------------------------------------------------------------

    def get_daily_recommended_songs_raw(self, afresh: bool = False) -> str:
        """对应 getDailyRecommendedSongs(/v3/discovery/recommend/songs)。

        afresh=True 让服务端换一批推荐(对应 Kotlin 默认 false)。
        """
        return self.call_weapi(
            "/v3/discovery/recommend/songs",
            {"afresh": "true" if afresh else "false"},
            use_persisted_cookies=True,
        )

    def get_daily_recommended_songs(self, afresh: bool = False) -> list[NeteaseSong]:
        """每日推荐歌曲(约 30 首/天);未登录/登录过期抛 NeteaseAuthRequiredError。"""
        return parse_recommended_songs_response(self.get_daily_recommended_songs_raw(afresh))

    # -- 播放地址 ------------------------------------------------------------

    def get_song_download_url_raw(self, song_id: int, level: str = "lossless") -> str:
        """对应 getSongDownloadUrl:eapi /song/enhance/player/url/v1。"""
        params: dict[str, Any] = {
            "ids": f"[{song_id}]",
            "level": level,
            "encodeType": "flac",
        }

        def call(use_persisted_cookies: bool) -> str:
            return self.call_eapi(
                "/song/enhance/player/url/v1",
                params,
                use_persisted_cookies=use_persisted_cookies,
            )

        prefer_persisted = self.has_login()
        resp = call(prefer_persisted)
        try:
            code = json.loads(resp).get("code", -1)
            if code == 301 and self.has_login():
                try:
                    self.ensure_weapi_session()
                except NeteaseApiError:
                    pass
                resp = call(True)
        except (json.JSONDecodeError, TypeError):
            pass
        return resp

    def get_song_url_raw(self, song_id: int, bitrate: int = 320000) -> str:
        """对应 getSongUrl(weapi /song/enhance/player/url,br 版)。"""
        return self.request(
            f"https://{NETEASE_MAIN_HOST}/weapi/song/enhance/player/url",
            {"ids": f"[{song_id}]", "br": str(bitrate)},
            "WEAPI",
            "POST",
            True,
        )

    def resolve_playable_url(
        self, song_id: int, duration_ms: int = 0, preferred_quality: str = DEFAULT_QUALITY
    ) -> PlayableUrl:
        """按音质回退链解析可用播放地址。

        逻辑对应 PlayerManagerUrlExtensions.getNeteaseSongUrl:
        RequiresLogin → 试更低音质,最后抛 NeteaseAuthRequiredError;
        Success(试听)→ 记住并继续试更低音质;Failure → 试更低音质;
        全部失败时优先返回试听片段,否则按原因抛中文异常。
        http:// 链接升级为 https://(对齐 buildNeteaseSuccessResult)。
        """
        candidates = build_quality_candidates(preferred_quality)
        preview_fallback: PlayableUrl | None = None
        requires_login = False
        last_reason: str | None = None

        for index, quality in enumerate(candidates):
            raw = self.get_song_download_url_raw(song_id, level=quality)
            parsed = parse_playback_response(raw, duration_ms)
            kind = parsed.get("kind")

            if kind == _PLAY_REQUIRES_LOGIN:
                requires_login = True
                if index < len(candidates) - 1:
                    continue
                break

            if kind == "success":
                url = parsed["url"]
                if url.startswith("http://"):
                    url = url.replace("http://", "https://", 1)
                playable = PlayableUrl(
                    url=url, level=parsed.get("level"), is_preview=parsed.get("is_preview", False)
                )
                if not playable.is_preview:
                    return playable
                preview_fallback = playable
                if index < len(candidates) - 1:
                    continue

            if kind == "failure":
                last_reason = parsed.get("reason")
                if index < len(candidates) - 1 and last_reason in (
                    _PLAY_FAIL_NO_PERMISSION,
                    _PLAY_FAIL_NO_PLAY_URL,
                ):
                    continue
                break

        if preview_fallback is not None:
            return preview_fallback
        if requires_login:
            raise NeteaseAuthRequiredError("登录态已失效,请重新扫码登录")
        if last_reason == _PLAY_FAIL_NO_PERMISSION:
            raise NeteaseNoPermissionError("该歌曲为 VIP 或版权受限,暂无法播放")
        raise NeteaseNoPlayUrlError("未能获取播放地址,请稍后重试")

    # -- 扫码登录(对应 NeteaseQrLoginClient) --------------------------------

    def reset_qr_login(self) -> None:
        with self._qr_lock:
            self._qr_cookie_store.clear()

    def _qr_current_cookies(self) -> dict[str, str]:
        return self._qr_cookie_store.cookies_for_url(_QR_ORIGIN)

    def _qr_execute_post(
        self, path: str, params: Mapping[str, Any], headers: Mapping[str, str] | None = None
    ) -> tuple[str, str]:
        """对应 executeWeApiPost;返回 (响应文本, x-refresh-token)。"""
        url = _QR_ORIGIN + path.lstrip("/")
        if path == _QR_ACCOUNT_PATH:
            csrf = str(params.get("csrf_token") or "")
            if csrf.strip():
                url = f"{url}?{urlencode({'csrf_token': csrf})}"

        encrypted = crypto.weapi_encrypt(params)
        request_headers = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": _QR_ORIGIN,
            "Origin": _QR_ORIGIN.rstrip("/"),
            "User-Agent": _QR_DESKTOP_UA,
            "x-os": "web",
            "x-channelsource": "undefined",
            "nm-gcore-status": "1",
            "Cookie": "; ".join(
                f"{k}={v}" for k, v in self._qr_current_cookies().items()
            ),
        }
        request_headers.update(headers or {})
        request = self._http.build_request(
            "POST", url, data=encrypted, headers=request_headers
        )
        try:
            response = self._send_with_direct_fallback(request)
        except httpx.HTTPError as error:
            _log.warning(
                "网易云扫码网络请求失败 url=%s proxies=%r(%s: %s)",
                url, dict(getproxies()), error.__class__.__name__, error,
            )
            raise NeteaseApiError(
                f"网络请求失败: {url}({error.__class__.__name__}: {error})"
            ) from error
        self._qr_cookie_store.save_from_response(
            _QR_ORIGIN, response.headers.get_list("set-cookie")
        )
        content = response.content
        if len(content) > _MAX_RESPONSE_BYTES:
            raise NeteaseApiError("网易云接口响应超过 4MB 限制")
        text = content.decode("utf-8", errors="replace")
        if response.status_code < 200 or response.status_code >= 300:
            raise NeteaseApiError(
                f"HTTP {response.status_code}: {text[:200]}", code=response.status_code
            )
        return text, response.headers.get(_QR_REFRESH_TOKEN_HEADER, "")

    def _qr_execute_json_post(
        self, path: str, params: Mapping[str, Any], headers: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        raw, _ = self._qr_execute_post(path, params, headers)
        if not raw.strip():
            raise NeteaseApiError("网易云扫码登录响应为空")
        return json.loads(raw)

    def create_qr_session(self, yd: NeteaseYdSnapshot | None = None) -> QrLoginSession:
        """申请 unikey 并组装二维码内容(对应 createSession)。

        yd: 指纹快照。Cookie 先种进会话再申请 unikey(对应
        seedCookieStoreFromSnapshot),chainId 由 sDeviceId 参与生成,
        令牌随后随轮询请求发送。缺省等价 Kotlin 的空快照回退
        (服务端风控可能拒绝空指纹的登录会话)。
        """
        snapshot = yd or NeteaseYdSnapshot()
        if snapshot.cookies:
            self._session.seed_runtime_cookies(NETEASE_MAIN_HOST, snapshot.cookies)
        result = self._qr_execute_json_post(
            _QR_UNIKEY_PATH, {"type": 1, "noCheckToken": True}
        )
        code = result.get("code", -1)
        key = str(result.get("unikey") or "").strip()
        message = result.get("message") or result.get("msg") or ""
        if code != 200 or not key:
            raise NeteaseApiError(
                f"创建扫码登录会话失败: {message or f'code={code}'}", code=int(code)
            )
        yd_device_token = snapshot.token
        chain_id = self._create_login_chain_id(snapshot.s_device_id)
        return QrLoginSession(
            key=key,
            chain_id=chain_id,
            yd_device_token=yd_device_token,
            qr_content=self._build_scan_login_url(key, chain_id),
        )

    def check_qr_login(self, session: QrLoginSession) -> QrLoginCheckResult:
        """轮询扫码状态(对应 checkLogin)。"""
        text, refresh_token = self._qr_execute_post(
            _QR_CHECK_PATH,
            {
                "type": 1,
                "noCheckToken": True,
                "key": session.key,
                "ydDeviceToken": session.yd_device_token,
            },
            headers={
                "x-loginmethod": "QrCode",
                "x-login-chain-id": session.chain_id,
            },
        )
        payload = json.loads(text)
        code = int(payload.get("code", -1))
        message = str(payload.get("message") or payload.get("msg") or "")
        if code == 803:
            verified = self._verify_confirmed_login(refresh_token)
            cookies = verified or self._qr_current_cookies()
            return QrLoginCheckResult(code=code, message=message, cookies=cookies)
        return QrLoginCheckResult(code=code, message=message)

    def _qr_verify_account_if_possible(self) -> dict[str, str]:
        """对应 verifyAccountIfPossible。"""
        snapshot = self._qr_current_cookies()
        csrf = snapshot.get("__csrf", "")
        params = build_qr_account_params(csrf)
        try:
            payload = self._qr_execute_json_post(_QR_ACCOUNT_PATH, params)
            has_account = isinstance(payload.get("account"), dict) or isinstance(
                payload.get("profile"), dict
            )
            if payload.get("code", -1) == 200 and has_account:
                return self._qr_current_cookies()
        except (NeteaseApiError, json.JSONDecodeError):
            pass
        return {}

    def _verify_confirmed_login(self, refresh_token: str) -> dict[str, str]:
        """对应 verifyConfirmedLogin。"""
        direct = self._qr_verify_account_if_possible()
        if direct:
            return direct

        credential = merge_qr_credential_cookies(self._qr_current_cookies(), refresh_token)
        if not credential.get(_QR_REFRESH_TOKEN_COOKIE, "").strip():
            return {}
        self._qr_cookie_store.seed_domain_cookies(NETEASE_MAIN_HOST, credential)
        refreshed = self._qr_verify_account_if_possible()
        if refreshed:
            return refreshed
        return credential

    @staticmethod
    def _create_login_chain_id(s_device_id: str) -> str:
        """对应 createLoginChainId。"""
        random_part = int(random.random() * 1_000_000)
        device_id = s_device_id.strip() or f"unknown-{random_part}"
        return f"v1_{device_id}_web_login_{int(time.time() * 1000)}"

    @staticmethod
    def _build_scan_login_url(key: str, chain_id: str) -> str:
        """扫码二维码内容,对应 buildScanLoginUrl(scanlogin 新式登录页)。

        注意:指纹(YD 快照)是这套新式流程的前置条件——没有指纹 Cookie/令牌
        创建的 unikey,手机端 scanlogin 页面会初始化失败(表现为"连不上
        官方登录页面");改用传统 login?codekey= 格式则手机确认后轮询会被
        服务端以"请切换其他登录方式或升级新版本"拒绝。两者都实测过,
        因此必须走新式 URL + 真实指纹。
        """
        return (
            f"https://{NETEASE_MAIN_HOST}/st/platform/scanlogin"
            f"?codekey={key}&chainId={chain_id}"
            f"&hdw_device=web&hdw_appid=web&hitExp=1"
        )
