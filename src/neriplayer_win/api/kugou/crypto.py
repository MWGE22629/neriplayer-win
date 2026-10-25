"""酷狗 API 签名与加密。

算法来源与验证方式:

1. H5 签名(网页接口):从 https://staticssl.kugou.com/common/js/min/infSign.min.js
   提取,并用 gateway 对照接口验证通过。
2. Android 签名(客户端接口):对照开源实现 KuGouMusicApi 的
   util/helper.js signatureAndroidParams 与 util/request.js 的默认参数注入,
   并用歌单接口实测通过(签名错时返回 20006,签名对时返回 20017/成功)。
3. AES / RSA:同项目的 util/crypto.js(cryptoAesEncrypt / cryptoRSAEncrypt),
   用于 token 续期。

两个盐值都是官方前端/客户端里的硬编码常量,官方随时可换。改动时只需
替换下方常量,算法本身不用动。

本模块为纯函数实现,不碰网络,便于单测(见 tests/test_kugou_crypto.py)。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import string
from typing import Any, Mapping

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

# ---- 签名盐值(官方硬编码,实测有效) --------------------------------------
# H5 盐值:infSign.min.js 里 `u="NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"`
WEB_SIGN_SALT = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
# Android 盐值:KuGouMusicApi util/helper.js signatureAndroidParams 的标准版
ANDROID_SIGN_SALT = "OIlwieks28dk2k092lksi2UIkp"

# ---- 续期接口的固定 AES 密钥(KuGouMusicApi module/login_token.js) --------
TOKEN_REFRESH_AES_KEY = "90b8382a1bb4ccdcf063102053fd75b8"
TOKEN_REFRESH_AES_IV = "f063102053fd75b8"

# ---- 续期接口的 RSA 公钥(1024 位,PKCS#1 v1.5,明文上限 117 字节) --------
TOKEN_REFRESH_RSA_PUBLIC_KEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDIAG7QOELSYoIJvTFJhMpe1s/gbjDJX51H"
    "BNnEl5HXqTW6lQ7LC8jr9fWZTwusknp+sVGzwd40MwP6U5yDE27M/X1+UR4tvOGOqp94TJtQ1"
    "EPnWGWXngpeIW5GxoQGao1rmYWAu6oi1z9XkChrsUdC6DJE5E221wf/4WLFxwAtRQIDAQAB\n"
    "-----END PUBLIC KEY-----"
)

def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def _param_string(params: Mapping[str, Any]) -> str:
    """把参数按名排序拼成 `k=v` 串;dict/list 值用紧凑 JSON。

    JS 侧规则:`Object.keys(params).sort().map(k => `${k}=${JSON.stringify(v)}`).join('')`,
    其中 JSON.stringify 对对象用紧凑分隔符(无空格)。Python 的
    json.dumps(separators=(",", ":")) 与之等价。
    """
    parts: list[str] = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        parts.append(f"{key}={value}")
    return "".join(parts)

def web_signature(params: Mapping[str, Any], body: str = "") -> str:
    """H5 签名:`盐 + 排序参数串 + 请求体 + 盐`,取 md5。

    body 只在 POST 且 postType=json 时拼进去;GET 传空串。
    """
    raw = WEB_SIGN_SALT + _param_string(params) + body + WEB_SIGN_SALT
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

def android_signature(params: Mapping[str, Any], body: str = "") -> str:
    """Android 签名:同上,但换盐值。

    与 H5 的差异不只是盐:clienttime 用秒(不是毫秒)、uuid 固定 "-",
    且 POST 请求体一定要参与签名。
    """
    raw = ANDROID_SIGN_SALT + _param_string(params) + body + ANDROID_SIGN_SALT
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

def aes_cbc_encrypt_hex(
    data: Any,
    key: str,
    iv: str,
) -> str:
    """AES-CBC/PKCS7 加密,输出 hex。

    data 为 dict 时先转紧凑 JSON(与 JS 的 JSON.stringify 一致)。
    """
    if not isinstance(data, str):
        data = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    return cipher.encrypt(pad(data.encode("utf-8"), AES.block_size)).hex()

def aes_cbc_decrypt_hex(ciphertext_hex: str, key: str, iv: str) -> str:
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    raw = cipher.decrypt(bytes.fromhex(ciphertext_hex))
    return unpad(raw, AES.block_size).decode("utf-8")

def random_session_key() -> str:
    """续期用的临时 AES 密钥:16 位小写字母。

    对应 JS `randomString(16).toLowerCase()`。取字母而非字母数字是有意的:
    下游用 md5(它) 切 32 位做 key、后 16 位做 iv,字符集不影响正确性,
    但与官方实现保持一致便于对照排查。
    """
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(16))

def derive_aes_pair(session_key: str) -> tuple[str, str]:
    """由临时密钥推导 (key, iv):md5 前 32 位作 key,后 16 位作 iv。

    对应 crypto.js cryptoAesEncrypt 的 `key = md5(tempKey)[0:32]`,
    `iv = key[key.length-16:]`。注意 iv 取自 key 的尾部,与 md5 尾部相同。
    """
    digest = md5_hex(session_key)
    key = digest[:32]
    return key, digest[-16:]

def rsa_encrypt_hex(plain: Any) -> str:
    """RSA PKCS#1 v1.5 加密,输出 hex。

    对应 crypto.js cryptoRSAEncrypt。注意 JS 侧 cryptoRSAEncrypt 会先把
    明文右侧补零到密钥长度,但续期接口实测走的是 rsaEncrypt2(PKCS#1 v1.5
    带随机填充),明文直接加密、不补零。这里按实测实现:不补零,
    只在超出 PKCS#1 v1.5 上限(117 字节)时报错。
    """
    if not isinstance(plain, str):
        plain = json.dumps(plain, separators=(",", ":"), ensure_ascii=False)
    raw = plain.encode("utf-8")
    key = RSA.import_key(TOKEN_REFRESH_RSA_PUBLIC_KEY)
    key_length = (key.size_in_bits() + 7) // 8
    if len(raw) > key_length - 11:
        raise ValueError(
            f"RSA 明文过长({len(raw)} 字节),PKCS#1 v1.5 上限 {key_length - 11}"
        )
    return PKCS1_v1_5.new(key).encrypt(raw).hex()

def build_refresh_payload(
    token: str,
    userid: str,
    dfid: str,
    now_ms: int,
) -> tuple[dict[str, Any], str]:
    """构造 /v5/login_by_token 的请求体,返回 (body, session_key)。

    session_key 要留着解密响应里的 secu_params(新 token 在里面)。
    实测要点:
    - p3 用固定密钥加密 {clienttime(秒), token}
    - pk 用 RSA 加密 {clienttime_ms, key: session_key}
    - params 用临时密钥加密空对象
    - t1/t2 为 0(非 lite 客户端)
    """
    session_key = random_session_key()
    aes_key, aes_iv = derive_aes_pair(session_key)
    body = {
        "dfid": dfid,
        "p3": aes_cbc_encrypt_hex(
            {"clienttime": now_ms // 1000, "token": token},
            TOKEN_REFRESH_AES_KEY,
            TOKEN_REFRESH_AES_IV,
        ),
        "plat": 1,
        "t1": 0,
        "t2": 0,
        "t3": "MCwwLDAsMCwwLDAsMCwwLDA=",
        "pk": rsa_encrypt_hex({"clienttime_ms": now_ms, "key": session_key}),
        "params": aes_cbc_encrypt_hex({}, aes_key, aes_iv),
        "userid": userid,
        "clienttime_ms": now_ms,
    }
    return body, session_key

def decode_refresh_response(secu_params: str, session_key: str) -> dict[str, Any]:
    """解密续期响应里的 secu_params,拿到新 token。

    响应是 `{"token": "<新token>"}` 的 JSON;个别版本可能直接返回裸字符串,
    这里两种都兼容。
    """
    aes_key, aes_iv = derive_aes_pair(session_key)
    plain = aes_cbc_decrypt_hex(secu_params, aes_key, aes_iv)
    try:
        decoded = json.loads(plain)
    except ValueError:
        return {"token": plain}
    if isinstance(decoded, dict):
        return decoded
    return {"token": str(decoded)}

def encode_image_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
