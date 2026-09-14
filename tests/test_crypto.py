"""crypto 单测:对照 reference 的 NeteaseCrypto.kt 移植可确定的断言。

- 加密输出格式(base64 / hex、长度)
- 确定性(注入固定 secret_key,RSA 无填充本身是确定性的)
- weapi / linuxapi / eapi 的 round-trip 解密还原
"""

from __future__ import annotations

import base64
import binascii
import json

import pytest
from Crypto.Cipher import AES

from neriplayer_win.api.netease import crypto

BASE62 = crypto.BASE62


def _aes_cbc_decrypt(cipher_b64: str, key: str, iv: str) -> bytes:
    data = base64.b64decode(cipher_b64)
    plain = AES.new(key.encode(), AES.MODE_CBC, iv.encode()).decrypt(data)
    pad = plain[-1]
    assert 1 <= pad <= 16
    return plain[:-pad]


class TestRandomKey:
    def test_length_and_alphabet(self):
        for _ in range(20):
            key = crypto.random_key()
            assert len(key) == 16
            assert all(ch in BASE62 for ch in key)

    def test_randomness(self):
        keys = {crypto.random_key() for _ in range(20)}
        assert len(keys) > 1


class TestMd5Hex:
    def test_known_vector(self):
        assert crypto.md5_hex("test") == "098f6bcd4621d373cade4e832627b4f6"

    def test_hex_format(self):
        digest = crypto.md5_hex("网易云")
        assert len(digest) == 32
        int(digest, 16)  # 纯十六进制

    def test_unicode_bytes(self):
        # MD5 按 UTF-8 字节计算
        import hashlib

        assert crypto.md5_hex("中文") == hashlib.md5("中文".encode("utf-8")).hexdigest()


class TestWeapiEncrypt:
    PAYLOAD = {"ids": "[347230]", "br": "320000"}

    def test_output_keys_and_format(self):
        result = crypto.weapi_encrypt(self.PAYLOAD)
        assert set(result) == {"params", "encSecKey"}
        # params 是合法 base64
        base64.b64decode(result["params"], validate=True)
        # encSecKey 是 256 位小写 hex(RSA-1024 无填充,128 字节)
        assert len(result["encSecKey"]) == 256
        int(result["encSecKey"], 16)
        assert result["encSecKey"] == result["encSecKey"].lower()

    def test_deterministic_with_fixed_key(self):
        # 与 Kotlin 一致:无填充 RSA 对相同输入结果确定
        first = crypto.weapi_encrypt(self.PAYLOAD, secret_key="0123456789abcdef")
        second = crypto.weapi_encrypt(self.PAYLOAD, secret_key="0123456789abcdef")
        assert first == second

    def test_roundtrip_layers(self):
        secret_key = "abcdefghijklmnop"
        result = crypto.weapi_encrypt(self.PAYLOAD, secret_key=secret_key)
        # 外层:随机 key + IV 解出 enc1
        enc1 = _aes_cbc_decrypt(result["params"], secret_key, crypto.IV).decode()
        # 内层:PRESET_KEY + IV 解出原始 JSON
        payload_json = _aes_cbc_decrypt(enc1, crypto.PRESET_KEY, crypto.IV).decode()
        assert json.loads(payload_json) == self.PAYLOAD

    def test_random_key_changes_params(self):
        first = crypto.weapi_encrypt(self.PAYLOAD, secret_key="aaaaaaaaaaaaaaaa")
        second = crypto.weapi_encrypt(self.PAYLOAD, secret_key="bbbbbbbbbbbbbbbb")
        assert first["params"] != second["params"]
        assert first["encSecKey"] != second["encSecKey"]

    def test_json_serialization_matches_kotlin_jsonutil(self):
        # Kotlin JsonUtil:紧凑分隔符、保序、不转义非 ASCII、bool/数字直接字面量
        assert (
            crypto._json_dumps({"type": 1, "noCheckToken": True, "s": "云"})
            == '{"type":1,"noCheckToken":true,"s":"云"}'
        )


class TestLinuxApi:
    def test_roundtrip(self):
        payload = {"username": "a", "password": "b"}
        result = crypto.linuxapi_encrypt(payload)
        assert set(result) == {"eparams"}
        assert binascii.unhexlify(result["eparams"])  # 合法 hex
        assert crypto.linuxapi_decrypt(result["eparams"]) == json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )


class TestEapi:
    PAYLOAD = {"ids": "[1]", "level": "lossless", "encodeType": "flac"}
    URL_PATH = "/eapi/song/enhance/player/url/v1"

    def test_output_format(self):
        result = crypto.eapi_encrypt(self.URL_PATH, self.PAYLOAD)
        assert set(result) == {"params"}
        assert result["params"] == result["params"].upper()  # Kotlin: hex.uppercase()
        body = bytes.fromhex(result["params"])
        assert len(body) % 16 == 0  # AES 块对齐

    def test_message_layout(self):
        result = crypto.eapi_encrypt(self.URL_PATH, self.PAYLOAD)
        plain = crypto.eapi_decrypt(result["params"])
        data = json.dumps(self.PAYLOAD, ensure_ascii=False, separators=(",", ":"))
        api_path = "/api/song/enhance/player/url/v1"  # /eapi → /api
        expected = "%s-36cd479b6b5-%s-36cd479b6b5-%s" % (
            api_path,
            data,
            crypto.md5_hex(crypto.EAPI_SALT % (api_path, data)),
        )
        assert plain == expected


class TestRsaEncrypt:
    def test_hex_length(self):
        assert len(crypto.rsa_encrypt("0123456789abcdef")) == 256

    def test_leading_zero_padding_kept(self):
        # 整数结果可能小于模数,Kotlin 侧补零到 keySize;hex 长度恒为 256
        value = crypto.rsa_encrypt("zzzzzzzzzzzzzzzz")
        assert len(value) == 256
        int(value, 16)


class TestAesEncrypt:
    def test_base64_and_hex_output(self):
        text = "hello"
        b64 = crypto.aes_encrypt(text, crypto.PRESET_KEY, crypto.IV, "cbc", "base64")
        hexed = crypto.aes_encrypt(text, crypto.PRESET_KEY, crypto.IV, "cbc", "hex")
        upper = crypto.aes_encrypt(text, crypto.PRESET_KEY, crypto.IV, "cbc", "HEX")
        assert base64.b64decode(b64) == bytes.fromhex(hexed)
        assert upper == hexed.upper()

    def test_unsupported_mode(self):
        with pytest.raises(ValueError):
            crypto.aes_encrypt("x", "k" * 16, "", "cfb", "hex")


class TestSessionCookieValue:
    def test_hex_32(self):
        value = crypto.new_session_cookie_value()
        assert len(value) == 32
        int(value, 16)
        assert value == value.lower()
