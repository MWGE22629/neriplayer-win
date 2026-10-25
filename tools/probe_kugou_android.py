"""酷狗 Android 版签名与歌单接口实测(M6 调研用,非产品代码)。

签名算法来源:开源项目 KuGouMusicApi 的 util/helper.js
(signatureAndroidParams)与 util/request.js 的默认参数注入规则。
本脚本按该规则复现并实测,结论写回 docs/research/M6-kugou-api.md。

用法(仓库根):
    uv run python tools/probe_kugou_android.py
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx

_COOKIE_FILE = Path(__file__).with_name("kugou_cookies.local.json")
# helper.js signatureAndroidParams 里的标准版盐值
_SIGN_SALT = "OIlwieks28dk2k092lksi2UIkp"
_APPID = "1005"
_CLIENTVER = "20489"
_UA = "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"
_GATEWAY = "https://gateway.kugou.com"


def _sorted_params_string(params: dict) -> str:
    parts = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        parts.append(f"{key}={value}")
    return "".join(parts)


def android_signature(params: dict, data: str = "") -> str:
    raw = _SIGN_SALT + _sorted_params_string(params) + data + _SIGN_SALT
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def build_request(
    cookies: dict[str, str],
    extra_params: dict,
    body: dict | None,
    router: str,
) -> tuple[dict, dict, str]:
    dfid = cookies.get("dfid") or cookies.get("kg_dfid") or "-"
    mid = cookies.get("mid") or cookies.get("kg_mid") or ""
    token = cookies.get("t", "")
    userid = cookies.get("KugooID", "")
    clienttime = str(int(time.time()))

    params = {
        "dfid": dfid,
        "mid": mid,
        "uuid": "-",
        "appid": _APPID,
        "clientver": _CLIENTVER,
        "clienttime": clienttime,
    }
    if token:
        params["token"] = token
    if userid and userid != "0":
        params["userid"] = userid
    params.update(extra_params)

    data = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
    params["signature"] = android_signature(params, data)

    headers = {
        "User-Agent": _UA,
        "dfid": dfid,
        "clienttime": clienttime,
        "mid": mid,
        "kg-rc": "1",
        "kg-thash": "5d816a0",
        "kg-rec": "1",
        "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",
        "x-router": router,
        "Content-Type": "application/json",
    }
    return params, headers, data


def call(
    cookies: dict[str, str],
    tag: str,
    path: str,
    extra_params: dict,
    body: dict | None,
    router: str,
) -> dict | None:
    params, headers, data = build_request(cookies, extra_params, body, router)
    url = _GATEWAY + path
    print("=" * 74)
    print(tag, "->", path, "router=", router)
    try:
        with httpx.Client(cookies=cookies, timeout=20, verify=False) as client:
            if body is not None:
                response = client.post(url, params=params, content=data, headers=headers)
            else:
                response = client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        print("  请求失败:", type(exc).__name__, exc)
        return None
    print(f"  status={response.status_code} len={len(response.text)}")
    try:
        payload = response.json()
    except ValueError:
        print("  非 JSON:", response.text[:200])
        return None
    print("  status字段=", payload.get("status"),
          "err=", payload.get("err_code"), "msg=", str(payload.get("msg"))[:40])
    data_part = payload.get("data")
    if isinstance(data_part, dict):
        print("  data 键:", list(data_part)[:16])
        for key in ("info", "lists", "total", "list"):
            if key in data_part:
                print(f"  {key}:",
                      json.dumps(data_part[key], ensure_ascii=False)[:900])
    elif isinstance(data_part, list):
        print("  data 列表长度:", len(data_part))
        if data_part:
            print("  首项:", json.dumps(data_part[0], ensure_ascii=False)[:600])
    else:
        print("  raw:", response.text[:400])
    return payload


def main() -> int:
    cookies = json.loads(_COOKIE_FILE.read_text(encoding="utf-8"))["cookies"]
    userid = cookies.get("KugooID", "")
    token = cookies.get("t", "")

    # 1. 用户歌单列表(开源实现里的路径)
    call(
        cookies,
        "用户歌单列表 v7/get_all_list",
        "/v7/get_all_list",
        {"plat": "1", "userid": userid, "token": token},
        {"userid": int(userid) if userid else 0, "token": token,
         "total_ver": 979, "type": 2, "page": 1, "pagesize": 30},
        "cloudlist.service.kugou.com",
    )

    # 2. 收藏歌单(同样走 cloudlist,type 换值试探)
    for type_value in (0, 1, 3):
        call(
            cookies,
            f"用户歌单 type={type_value}",
            "/v7/get_all_list",
            {"plat": "1", "userid": userid, "token": token},
            {"userid": int(userid) if userid else 0, "token": token,
             "total_ver": 979, "type": type_value, "page": 1, "pagesize": 30},
            "cloudlist.service.kugou.com",
        )

    # 3. 歌单详情
    call(
        cookies,
        "歌单详情 v3/get_list_info",
        "/v3/get_list_info",
        {},
        {"data": [{"global_collection_id": ""}], "userid": userid, "token": token},
        "pubsongs.kugou.com",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
