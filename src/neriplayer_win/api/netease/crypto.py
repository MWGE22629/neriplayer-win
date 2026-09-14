"""网易云 API 加密(weapi / eapi / linuxapi)。

逐行对照 reference/NeriPlayer-Android 的
app/src/main/java/moe/ouom/neriplayer/core/api/netease/NeteaseCrypto.kt 翻译:

- random_key()            <- randomKey()
- aes_encrypt()           <- aesEncrypt()(AES-CBC/ECB + PKCS7,base64/hex 输出)
- rsa_encrypt()           <- rsaEncrypt()(无填充 RSA,hex 输出)
- md5_hex()               <- md5Hex()
- weapi_encrypt()         <- weApiEncrypt()
- linuxapi_encrypt()      <- linuxApiEncrypt()
- linuxapi_decrypt()      <- linuxApiDecrypt()
- eapi_encrypt()          <- eApiEncrypt()
- _json_dumps()           <- JsonUtil.toJson(保持插入序、不转义非 ASCII)

pycryptodome 与 javax.crypto 的 PKCS5Padding 均为 PKCS#7 实现,行为一致。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any, Mapping

from Crypto.Cipher import AES
from Crypto.PublicKey import RSA

BASE62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
PRESET_KEY = "0CoJUm6Qyw8W8jud"
IV = "0102030405060708"
LINUX_KEY = "rFgB&h#%2?^eDg:Q"
EAPI_KEY = "e82ckenh8dichen8"
EAPI_FORMAT = "%s-36cd479b6b5-%s-36cd479b6b5-%s"
EAPI_SALT = "nobody%suse%smd5forencrypt"
PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/aKzmFb
t7clFSs6sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQXE9/DDaFt6Rr7iVZ
MldczhC0JNgTz+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45wIDAQAB
-----END PUBLIC KEY-----"""


def random_key() -> str:
    """16 位 [a-zA-Z0-9] 随机密钥(对应 Kotlin randomKey,SecureRandom)。"""
    return "".join(secrets.choice(BASE62) for _ in range(16))


def _reverse_string(text: str) -> str:
    return text[::-1]


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad = block_size - len(data) % block_size
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if not 1 <= pad <= 16:
        raise ValueError("无效的 PKCS7 填充")
    return data[:-pad]


def aes_encrypt(
    text: str,
    key: str,
    iv: str = "",
    mode: str = "cbc",
    fmt: str = "base64",
) -> str:
    """AES 加密,对应 Kotlin aesEncrypt(text, key, ivStr, mode, format)。

    mode: "cbc" / "ecb";fmt: "base64" / "hex" / "HEX"(大写十六进制)。
    """
    key_bytes = key.encode("utf-8")
    padded = _pkcs7_pad(text.encode("utf-8"))
    lower_mode = mode.lower()
    if lower_mode == "cbc":
        cipher = AES.new(key_bytes, AES.MODE_CBC, iv.encode("utf-8"))
    elif lower_mode == "ecb":
        cipher = AES.new(key_bytes, AES.MODE_ECB)
    else:
        raise ValueError(f"未知 AES 模式: {mode}")
    encrypted = cipher.encrypt(padded)

    lower_fmt = fmt.lower()
    if lower_fmt == "base64":
        return base64.b64encode(encrypted).decode("ascii")
    if lower_fmt == "hex":
        hexed = encrypted.hex()
        return hexed.upper() if fmt.isupper() else hexed
    raise ValueError(f"未知加密输出格式: {fmt}")


def _load_rsa_public_key() -> RSA.RsaKey:
    cleaned = (
        PUBLIC_KEY_PEM.replace("-----BEGIN PUBLIC KEY-----", "")
        .replace("-----END PUBLIC KEY-----", "")
    )
    cleaned = "".join(ch for ch in cleaned if not ch.isspace())
    key_bytes = base64.b64decode(cleaned)
    return RSA.import_key(key_bytes)


def rsa_encrypt(text: str) -> str:
    """RSA 加密随机密钥,使用与官方客户端一致的无填充算法(模幂运算)。"""
    try:
        pub_key = _load_rsa_public_key()
        # BigInteger(1, text.toByteArray()) 的 modPow(e, n)
        message = int.from_bytes(text.encode("utf-8"), "big")
        result = pow(message, pub_key.e, pub_key.n)

        key_size = (pub_key.n.bit_length() + 7) // 8
        return result.to_bytes(key_size, "big").hex()
    except Exception as error:  # noqa: BLE001 - 与 Kotlin 行为一致,统一转译
        raise RuntimeError("RSA 加密失败") from error


def md5_hex(data: str) -> str:
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def _json_dumps(payload: Mapping[str, Any]) -> str:
    """对应 Kotlin JsonUtil.toJson:保持插入顺序、紧凑分隔符、不转义非 ASCII。

    Kotlin 端 Number/Boolean 用 toString(),Python 的 json 对 int/float/bool
    输出等价的 JSON 字面量,语义一致。
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def weapi_encrypt(
    payload: Mapping[str, Any], secret_key: str | None = None
) -> dict[str, str]:
    """weapi 加密;secret_key 参数仅供测试注入,缺省随机。"""
    text = _json_dumps(payload)
    key = secret_key if secret_key is not None else random_key()
    enc1 = aes_encrypt(text, PRESET_KEY, IV, "cbc", "base64")
    params = aes_encrypt(enc1, key, IV, "cbc", "base64")
    enc_sec_key = rsa_encrypt(_reverse_string(key))
    return {"params": params, "encSecKey": enc_sec_key}


def linuxapi_encrypt(payload: Mapping[str, Any]) -> dict[str, str]:
    return {"eparams": aes_encrypt(_json_dumps(payload), LINUX_KEY, "", "ecb", "hex")}


def eapi_encrypt(url_path: str, payload: Mapping[str, Any]) -> dict[str, str]:
    """eapi 加密;url_path 为请求的 encodedPath(形如 /eapi/xxx)。"""
    data = _json_dumps(payload)
    api_path = url_path.replace("/eapi", "/api")
    message = EAPI_FORMAT % (
        api_path,
        data,
        md5_hex(EAPI_SALT % (api_path, data)),
    )
    cipher = aes_encrypt(message, EAPI_KEY, "", "ecb", "hex").upper()
    return {"params": cipher}


def linuxapi_decrypt(cipher_text: str) -> str:
    """linuxapi 解密(对应 linuxApiDecrypt),主要用于测试 round-trip。"""
    data = bytes.fromhex(cipher_text)
    cipher = AES.new(LINUX_KEY.encode("utf-8"), AES.MODE_ECB)
    plain = cipher.decrypt(data)
    pad = plain[-1]
    return plain[: len(plain) - pad].decode("utf-8")


def eapi_decrypt(cipher_text: str) -> str:
    """eapi 解密(测试辅助):AES-ECB + PKCS7 还原明文。"""
    cipher = AES.new(EAPI_KEY.encode("utf-8"), AES.MODE_ECB)
    plain = _pkcs7_unpad(cipher.decrypt(bytes.fromhex(cipher_text.lower())))
    return plain.decode("utf-8")


def new_session_cookie_value() -> str:
    """16 字节 SecureRandom 的 32 位小写 hex(对应 newNeteaseSessionCookieValue)。"""
    return secrets.token_hex(16)


__all__ = [
    "BASE62",
    "EAPI_KEY",
    "LINUX_KEY",
    "aes_encrypt",
    "eapi_decrypt",
    "eapi_encrypt",
    "linuxapi_decrypt",
    "linuxapi_encrypt",
    "md5_hex",
    "new_session_cookie_value",
    "random_key",
    "rsa_encrypt",
    "weapi_encrypt",
]
