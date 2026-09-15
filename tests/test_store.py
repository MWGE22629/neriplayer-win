"""LocalStore 单测:使用环境变量覆盖数据目录(tmp_path)。"""

from __future__ import annotations

import json

from neriplayer_win.data.store import (
    LocalStore,
    apply_stored_order,
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


class TestLocalStoreSettings:
    def test_defaults(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        settings = store.load_settings()
        assert settings["close_action"] == "tray"
        assert settings["play_mode"] == "sequence"
        assert settings["appearance"] == "dark"
        assert settings["play_quality"] == "lossless"

    def test_appearance_roundtrip(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        assert store.save_settings({"appearance": "light"}) is True
        assert store.load_settings()["appearance"] == "light"
        assert store.save_settings({"appearance": "dark"}) is True
        assert store.load_settings()["appearance"] == "dark"

    def test_invalid_appearance_falls_back(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.settings_path.parent.mkdir(parents=True, exist_ok=True)
        store.settings_path.write_text(
            json.dumps({"appearance": "solarized"}), encoding="utf-8"
        )
        assert store.load_settings()["appearance"] == "dark"
        # 保存时非法值同样被拒,保持默认
        assert store.save_settings({"appearance": "neo"}) is True
        assert store.load_settings()["appearance"] == "dark"

    def test_play_quality_roundtrip(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        for value in ("standard", "exhigh"):
            assert store.save_settings({"play_quality": value}) is True
            assert store.load_settings()["play_quality"] == value
        # 缺省(只存别的键)时回落默认无损
        assert store.save_settings({"appearance": "light"}) is True
        assert store.load_settings()["play_quality"] == "lossless"

    def test_invalid_play_quality_falls_back(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.settings_path.parent.mkdir(parents=True, exist_ok=True)
        store.settings_path.write_text(
            json.dumps({"play_quality": "hires"}), encoding="utf-8"
        )
        assert store.load_settings()["play_quality"] == "lossless"
        # 保存时非法值同样被拒,保持默认
        assert store.save_settings({"play_quality": "320k"}) is True
        assert store.load_settings()["play_quality"] == "lossless"

    def test_other_settings_survive_appearance_save(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.save_settings({"appearance": "light", "play_mode": "shuffle"})
        assert store.load_settings() == {
            "close_action": "tray",
            "play_mode": "shuffle",
            "appearance": "light",
            "play_quality": "lossless",
            "sidebar_expanded": {
                "netease": True, "netease-subscribed": True, "bili": True,
            },
            "netease_playlist_order": [],
            "netease_subscribed_order": [],
            "bili_folder_order": [],
        }


class TestApplyStoredOrder:
    """M5 分区内排序纯函数:已知排前、新条目追加、失效 id 静默丢弃。"""

    def test_known_first_new_appended_stale_dropped(self):
        # 30/10 按存储序排前,20 是新条目追加尾部,99 已失效被丢弃
        assert apply_stored_order([30, 99, 10], [10, 20, 30]) == [30, 10, 20]

    def test_empty_stored_keeps_current_order(self):
        assert apply_stored_order([], [2, 1, 3]) == [2, 1, 3]

    def test_all_stale_keeps_current_order(self):
        assert apply_stored_order([7, 8], [1, 2]) == [1, 2]

    def test_duplicate_stored_ids_deduped(self):
        assert apply_stored_order([5, 5, 6], [5, 6]) == [5, 6]

    def test_does_not_mutate_inputs(self):
        stored = [3, 1, 2]
        current = [1, 2, 3]
        apply_stored_order(stored, current)
        assert stored == [3, 1, 2]
        assert current == [1, 2, 3]


class TestSidebarExpandedSetting:
    """M5 分区折叠状态:{"netease"/"netease-subscribed"/"bili": bool},默认全展开。"""

    def test_defaults(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        assert store.load_settings()["sidebar_expanded"] == {
            "netease": True,
            "netease-subscribed": True,
            "bili": True,
        }

    def test_roundtrip(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        flags = {"netease": False, "netease-subscribed": True, "bili": False}
        assert store.save_settings({"sidebar_expanded": flags}) is True
        assert store.load_settings()["sidebar_expanded"] == flags

    def test_per_key_fallback_on_bad_values(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.settings_path.parent.mkdir(parents=True, exist_ok=True)
        store.settings_path.write_text(
            json.dumps(
                {"sidebar_expanded": {"netease": "yes", "bili": False}}
            ),
            encoding="utf-8",
        )
        # netease 非 bool 逐键回落默认;netease-subscribed 缺省回落默认;
        # bili 合法保留
        assert store.load_settings()["sidebar_expanded"] == {
            "netease": True,
            "netease-subscribed": True,
            "bili": False,
        }

    def test_non_dict_falls_back(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.settings_path.parent.mkdir(parents=True, exist_ok=True)
        store.settings_path.write_text(
            json.dumps({"sidebar_expanded": [True, False]}), encoding="utf-8"
        )
        assert store.load_settings()["sidebar_expanded"] == {
            "netease": True,
            "netease-subscribed": True,
            "bili": True,
        }


class TestSectionOrderSettings:
    """M5 分区排序键:list[int] 校验(bool 不算 int),非法回落空表。"""

    def test_roundtrip(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        assert store.save_settings({"netease_playlist_order": [30, 10, 20]}) is True
        assert store.load_settings()["netease_playlist_order"] == [30, 10, 20]
        assert store.save_settings({"netease_subscribed_order": [31, 32]}) is True
        assert store.load_settings()["netease_subscribed_order"] == [31, 32]
        assert store.save_settings({"bili_folder_order": [9, 8]}) is True
        assert store.load_settings()["bili_folder_order"] == [9, 8]

    def test_invalid_elements_fall_back(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.settings_path.parent.mkdir(parents=True, exist_ok=True)
        store.settings_path.write_text(
            json.dumps({"netease_playlist_order": ["a", 1]}), encoding="utf-8"
        )
        assert store.load_settings()["netease_playlist_order"] == []
        # 保存时非法值同样被拒(bool 是 int 子类,单独排除)
        assert store.save_settings({"bili_folder_order": [True, 2]}) is True
        assert store.load_settings()["bili_folder_order"] == []

    def test_non_list_falls_back(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, monkeypatch)
        store.settings_path.parent.mkdir(parents=True, exist_ok=True)
        store.settings_path.write_text(
            json.dumps({"bili_folder_order": {"9": 1}}), encoding="utf-8"
        )
        assert store.load_settings()["bili_folder_order"] == []
