"""LocalStore 单测:使用环境变量覆盖数据目录(tmp_path)。"""

from __future__ import annotations

import json

from neriplayer_win.data.store import (
    LocalStore,
    default_data_dir,
    validate_and_sanitize_netease_cookies,
)


def make_store(tmp_path, monkeypatch):
    monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))
    return LocalStore()


class TestValidateCookies:
    def test_rejects_bad_names_and_values(self):
        sanitized, rejected = validate_and_sanitize_netease_cookies(
            {"MUSIC_U": "ok", "bad name": "x", "empty": " ", "semi": "a;b"}
        )
        assert sanitized["MUSIC_U"] == "ok"
        assert set(rejected) == {"bad name", "empty", "semi"}

    def test_fallback_os_appver(self):
        sanitized, _ = validate_and_sanitize_netease_cookies({"MUSIC_U": "u"})
        assert sanitized["os"] == "pc"
        assert sanitized["appver"] == "8.10.35"

    def test_no_fallback_when_empty(self):
        sanitized, _ = validate_and_sanitize_netease_cookies({"bad name": "x"})
        assert sanitized == {}


class TestLocalStore:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        assert store.save_netease(
            {"MUSIC_U": "token", "__csrf": "csrf"},
            profile={"userId": 1, "nickname": "测试"},
        )
        bundle = store.load_netease()
        assert bundle is not None
        assert bundle["cookies"]["MUSIC_U"] == "token"
        assert bundle["profile"]["nickname"] == "测试"
        assert bundle["savedAt"] > 0

    def test_save_without_music_u_rejected(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        assert store.save_netease({"__csrf": "only"}) is False
        assert store.load_netease() is None

    def test_clear(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.save_netease({"MUSIC_U": "token"})
        store.clear_netease()
        assert store.load_netease() is None
        store.clear_netease()  # 幂等

    def test_corrupted_file_returns_none(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.netease_path.parent.mkdir(parents=True, exist_ok=True)
        store.netease_path.write_text("{broken json", encoding="utf-8")
        assert store.load_netease() is None

    def test_no_music_u_in_file_returns_none(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.netease_path.parent.mkdir(parents=True, exist_ok=True)
        store.netease_path.write_text(
            json.dumps({"cookies": {"__csrf": "x"}, "savedAt": 1}), encoding="utf-8"
        )
        assert store.load_netease() is None

    def test_env_override_dir(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        assert store.netease_path.parent == tmp_path
        assert default_data_dir() == tmp_path


class TestLocalStoreBili:
    """B站登录包(bili.json);格式对照 BiliAuthBundle.toJson。"""

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        assert store.save_bili(
            {"SESSDATA": "abc%2C123", "bili_jct": "jct", "DedeUserID": "42", "buvid3": "b3"},
            profile={"mid": 42, "uname": "测试"},
        )
        bundle = store.load_bili()
        assert bundle is not None
        assert bundle["cookies"]["SESSDATA"] == "abc%2C123"
        assert bundle["cookies"]["DedeUserID"] == "42"
        # B站包不注入 os/appver 回退键(那是网易云专用)
        assert "os" not in bundle["cookies"]
        assert bundle["profile"]["mid"] == 42
        assert bundle["savedAt"] > 0

    def test_save_without_sessdata_rejected(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        assert store.save_bili({"bili_jct": "only"}) is False
        assert store.load_bili() is None

    def test_clear(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.save_bili({"SESSDATA": "token"})
        store.clear_bili()
        assert store.load_bili() is None
        store.clear_bili()  # 幂等

    def test_corrupted_file_returns_none(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.bili_path.parent.mkdir(parents=True, exist_ok=True)
        store.bili_path.write_text("{broken json", encoding="utf-8")
        assert store.load_bili() is None

    def test_no_sessdata_in_file_returns_none(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.bili_path.parent.mkdir(parents=True, exist_ok=True)
        store.bili_path.write_text(
            json.dumps({"cookies": {"buvid3": "x"}, "savedAt": 1}), encoding="utf-8"
        )
        assert store.load_bili() is None

    def test_bili_and_netease_files_are_independent(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.save_bili({"SESSDATA": "s"})
        store.save_netease({"MUSIC_U": "m"})
        assert store.load_bili() is not None
        assert store.load_netease() is not None
        assert store.bili_path != store.netease_path
        store.clear_bili()
        assert store.load_bili() is None
        assert store.load_netease() is not None
