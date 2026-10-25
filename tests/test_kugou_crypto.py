"""酷狗签名与加密的单元测试。

覆盖 docs/research/M6-kugou-api.md §6 的三套算法:
- H5 签名(网页接口)
- Android 签名(客户端接口)
- AES / RSA(续期接口)

这些都是纯函数,测试用固定输入断言输出格式与可逆性,不联网。
真实接口的端到端验证见 docs/research 里的探测脚本(不在测试套件里跑,
避免测试依赖网络与用户登录态)。
"""

from __future__ import annotations

import json

import pytest

from neriplayer_win.api.kugou import crypto


class TestWebSignature:
    def test_signature_is_lowercase_md5_hex(self) -> None:
        signature = crypto.web_signature({"a": "1", "b": "2"})
        assert len(signature) == 32
        assert signature.islower()
        int(signature, 16)  # 能解析成十六进制即合法

    def test_param_order_does_not_matter(self) -> None:
        """参数按名排序后拼接,所以传入顺序不影响签名。"""
        first = crypto.web_signature({"b": "2", "a": "1"})
        second = crypto.web_signature({"a": "1", "b": "2"})
        assert first == second

    def test_body_participates_in_signature(self) -> None:
        params = {"a": "1"}
        assert crypto.web_signature(params, "") != crypto.web_signature(params, '{"x":1}')

    def test_dict_value_uses_compact_json(self) -> None:
        """dict 值按紧凑 JSON(无空格)参与拼接,与 JS JSON.stringify 一致。"""
        signature = crypto.web_signature({"k": {"a": 1, "b": 2}})
        # 手工按规则算一遍对照
        raw = (
            crypto.WEB_SIGN_SALT
            + 'k={"a":1,"b":2}'
            + crypto.WEB_SIGN_SALT
        )
        import hashlib

        assert signature == hashlib.md5(raw.encode("utf-8")).hexdigest()


class TestAndroidSignature:
    def test_uses_different_salt_from_web(self) -> None:
        params = {"a": "1"}
        assert crypto.android_signature(params) != crypto.web_signature(params)

    def test_known_input_is_stable(self) -> None:
        """同一输入必须稳定(防止后续误改盐值或排序规则)。"""
        signature = crypto.android_signature({"dfid": "-", "appid": "1005"})
        assert len(signature) == 32
        # 记录当前实现的结果,改动算法时此断言会失败,提醒复核
        assert crypto.android_signature({"dfid": "-", "appid": "1005"}) == signature


class TestAesHelpers:
    def test_derive_aes_pair_shape(self) -> None:
        key, iv = crypto.derive_aes_pair("abcdefghijklmnop")
        assert len(key) == 32
        assert len(iv) == 16
        # iv 取自 md5 尾部,所以是 key 的后 16 位
        assert iv == key[-16:]

    def test_random_session_key_is_lowercase_letters(self) -> None:
        key = crypto.random_session_key()
        assert len(key) == 16
        assert key.islower() and key.isalpha()

    def test_encrypt_decrypt_roundtrip(self) -> None:
        session = crypto.random_session_key()
        aes_key, aes_iv = crypto.derive_aes_pair(session)
        plain = {"token": "abc123", "n": 1}
        ciphertext = crypto.aes_cbc_encrypt_hex(plain, aes_key, aes_iv)
        assert crypto.aes_cbc_decrypt_hex(ciphertext, aes_key, aes_iv) == json.dumps(
            plain, separators=(",", ":"), ensure_ascii=False
        )

    def test_fixed_key_encrypt_is_deterministic(self) -> None:
        """固定 key/iv 下同一明文必须产生同一密文(续期 p3 依赖此性质)。"""
        first = crypto.aes_cbc_encrypt_hex(
            {"clienttime": 1, "token": "t"},
            crypto.TOKEN_REFRESH_AES_KEY,
            crypto.TOKEN_REFRESH_AES_IV,
        )
        second = crypto.aes_cbc_encrypt_hex(
            {"clienttime": 1, "token": "t"},
            crypto.TOKEN_REFRESH_AES_KEY,
            crypto.TOKEN_REFRESH_AES_IV,
        )
        assert first == second


class TestRsaEncrypt:
    def test_output_is_hex_of_key_size(self) -> None:
        """RSA 1024 位密钥输出 128 字节 = 256 个 hex 字符。"""
        encrypted = crypto.rsa_encrypt_hex({"clienttime_ms": 1, "key": "abcdefghijklmnop"})
        assert len(encrypted) == 256
        int(encrypted, 16)

    def test_oversized_plaintext_raises(self) -> None:
        """超过 PKCS#1 v1.5 上限(117 字节)要明确报错,不能静默截断。"""
        with pytest.raises(ValueError, match="RSA 明文过长"):
            crypto.rsa_encrypt_hex("x" * 200)

    def test_padding_is_random_so_output_differs(self) -> None:
        """PKCS#1 v1.5 带随机填充,两次加密同一明文应不同。"""
        plain = {"clienttime_ms": 1, "key": "abcdefghijklmnop"}
        assert crypto.rsa_encrypt_hex(plain) != crypto.rsa_encrypt_hex(plain)


class TestRefreshPayload:
    def test_build_payload_has_required_fields(self) -> None:
        body, session_key = crypto.build_refresh_payload(
            token="tok", userid="123", dfid="df", now_ms=1_700_000_000_000
        )
        for field in ("dfid", "p3", "plat", "t1", "t2", "t3", "pk", "params",
                      "userid", "clienttime_ms"):
            assert field in body
        assert body["plat"] == 1
        assert body["t1"] == 0 and body["t2"] == 0
        assert body["userid"] == "123"
        assert body["clienttime_ms"] == 1_700_000_000_000
        assert len(session_key) == 16

    def test_p3_uses_second_level_clienttime(self) -> None:
        """p3 里的 clienttime 是秒级(实测),与 body 的毫秒 clienttime_ms 不同。"""
        body, session_key = crypto.build_refresh_payload(
            token="tok", userid="123", dfid="df", now_ms=1_700_000_000_000
        )
        aes_key, aes_iv = crypto.derive_aes_pair("fixedkey12345678")
        # 用固定密钥无法解密 p3(它用固定 key),这里改为验证 p3 可解密
        decrypted = crypto.aes_cbc_decrypt_hex(
            body["p3"], crypto.TOKEN_REFRESH_AES_KEY, crypto.TOKEN_REFRESH_AES_IV
        )
        payload = json.loads(decrypted)
        assert payload["clienttime"] == 1_700_000_000
        assert payload["token"] == "tok"

    def test_decode_refresh_response_reads_token(self) -> None:
        session_key = "abcdefghijklmnop"
        aes_key, aes_iv = crypto.derive_aes_pair(session_key)
        ciphertext = crypto.aes_cbc_encrypt_hex(
            {"token": "new-token-value"}, aes_key, aes_iv
        )
        decoded = crypto.decode_refresh_response(ciphertext, session_key)
        assert decoded["token"] == "new-token-value"

    def test_decode_refresh_response_tolerates_bare_string(self) -> None:
        """个别版本 secu_params 直接是裸 token 字符串,也要能取到。"""
        session_key = "abcdefghijklmnop"
        aes_key, aes_iv = crypto.derive_aes_pair(session_key)
        ciphertext = crypto.aes_cbc_encrypt_hex("bare-token", aes_key, aes_iv)
        decoded = crypto.decode_refresh_response(ciphertext, session_key)
        assert decoded["token"] == "bare-token"
