"""播放内核:python-mpv(libmpv)音频播放。

职责:
- 加载一个音频流地址并播放(纯音频,不嵌入任何 Qt 渲染窗口)
- 提供播放/暂停、seek、音量、切歌、停止控制
- 通过 Qt 信号上报播放进度与播放结束事件,供 UI 与队列管理消费

实现说明:
- mpv-2.dll 不随系统分发,放在仓库 bin/ 目录(gitignore)。首次使用需从
  https://github.com/shinchiro/mpv-winbuild-cmake/releases 下载
  mpv-dev-x86_64-*.7z 并解压出 mpv-2.dll 放入 bin/。import mpv 前先把
  bin/ 绝对路径插进 %PATH% 并用 ctypes.CDLL(绝对路径)预加载。
- 线程模型:不注册任何 python-mpv 回调,结束/失败检测完全依赖 UI 线程
  QTimer(250ms)的确定性属性快照:播完时 mpv 转入 idle 且 time-pos 归零,
  因此用「曾经播过 + idle」判定结束;从未开播而 idle 超过宽限期判定
  加载失败。所有 Qt 信号都从 UI 线程发出,天然线程安全。
- 退出:直接 terminate() 会与 mpv 事件线程(mpv_wait_event)竞争,实测
  随机崩溃;按 python-mpv 文档推荐顺序:先发 quit 命令并等待事件线程
  自行退出,再 terminate 收尾。
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


class PlayerEngineError(RuntimeError):
    """播放内核初始化/运行错误,携带用户可读的中文指引。"""


def build_http_header_fields_option(headers) -> str:
    """把 HTTP 头字典编译成 loadfile 的逐文件选项串。

    两层解析都要绕开逗号:
    1. loadfile 的 options 参数本身是 key=value,key=value 列表(逗号分隔),
       值支持 %<字节长度>% 长度前缀转义(m_option.c read_subparam);
    2. http-header-fields 又是字符串列表选项(逗号拆项),所以第二个及之后
       的头必须用 -append 后缀(全值语义,不拆逗号)。
    实测(mpv v0.41)该形式可携带含逗号的浏览器 UA "(KHTML, like Gecko)"
    正常取流。
    """
    parts: list[str] = []
    for index, (key, value) in enumerate(headers.items()):
        entry = f"{key}: {value}"
        option = "http-header-fields" if index == 0 else "http-header-fields-append"
        if "," in entry:
            parts.append(f"{option}=%{len(entry.encode('utf-8'))}%{entry}")
        else:
            parts.append(f"{option}={entry}")
    return ",".join(parts)


def _candidate_dll_paths() -> list[Path]:
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    return [
        repo_root / "bin" / "mpv-2.dll",
        Path.cwd() / "bin" / "mpv-2.dll",
    ]


def _preload_mpv_dll() -> Path:
    for dll_path in _candidate_dll_paths():
        if dll_path.is_file():
            ctypes.CDLL(str(dll_path))
            return dll_path
    raise PlayerEngineError(
        "未找到 mpv 运行库(mpv-2.dll)。\n"
        "请到 https://github.com/shinchiro/mpv-winbuild-cmake/releases 下载\n"
        "mpv-dev-x86_64-*.7z,解压出其中的 mpv-2.dll 放到本仓库 bin/ 目录后重启应用。"
    )


_mpv = None
_dll_path: Path | None = None


def _load_mpv_module():
    global _mpv, _dll_path
    if _mpv is not None:
        return _mpv
    _dll_path = _preload_mpv_dll()
    # python-mpv 通过 ctypes.util.find_library 按 %PATH% 定位 dll,
    # 因此先把 bin/ 绝对路径插到 PATH 最前,再显式预加载一次。
    os.environ["PATH"] = (
        str(_dll_path.parent) + os.pathsep + os.environ.get("PATH", "")
    )
    ctypes.CDLL(str(_dll_path))
    import mpv  # 延迟导入:此时 find_library 能在 bin/ 命中 mpv-2.dll

    _mpv = mpv
    return _mpv


class PlayerEngine(QObject):
    """libmpv 播放引擎;应在 UI 线程创建,QTimer 在同线程轮询上报。"""

    progress = Signal(float, float)  # (当前秒, 总秒;总秒未知时为 0)
    track_ended = Signal()  # 一首播完(eof)
    playing_changed = Signal(bool)
    error = Signal(str)

    _LOAD_FAIL_GRACE_TICKS = 20  # ~5s,冷启动慢加载不算失败

    def __init__(
        self,
        parent: QObject | None = None,
        poll_interval_ms: int = 250,
        audio_output: str | None = None,
    ) -> None:
        super().__init__(parent)
        try:
            mpv = _load_mpv_module()
        except (PlayerEngineError, OSError, ImportError) as exc:
            raise PlayerEngineError(str(exc)) from exc

        options: dict[str, object] = {
            "audio_display": "no",  # 纯音频,不弹视频/封面窗口
        }
        if audio_output is not None:
            # 例如测试传 "null":不碰真实音频设备,规避驱动/线程问题
            options["ao"] = audio_output
        self._player = mpv.MPV(
            **options,
            input_default_bindings=False,
            input_vo_keyboard=False,
            input_terminal=False,
            terminal=False,
            osc=False,
            load_scripts=False,
            ytdl=False,
            config=False,
        )
        self._player.volume = 70.0
        self._has_media = False
        self._end_emitted = False
        self._idle_ticks = 0
        self._ever_played = False

        self._timer = QTimer(self)
        self._timer.setInterval(poll_interval_ms)
        self._timer.timeout.connect(self._on_tick)

    # -- 播放控制 ------------------------------------------------------------

    def play_url(self, url: str, headers: dict[str, str] | None = None) -> None:
        """加载 URL 并立即播放(切歌 = replace)。

        headers: 逐文件 HTTP 请求头(loadfile 逐文件选项 http-header-fields)。
        B站音频流(upos mirror 的 m4s)要求 Referer + 浏览器 UA,否则 403;
        网易云不传 headers,行为不变。
        """
        self._has_media = True
        self._end_emitted = False
        self._idle_ticks = 0
        self._ever_played = False
        try:
            if headers:
                options = build_http_header_fields_option(headers)
                version = tuple(getattr(self._player, "mpv_version_tuple", (0, 0, 0)))
                if version >= (0, 38, 0):
                    self._player.command("loadfile", url, "replace", -1, options)
                else:
                    self._player.command("loadfile", url, "replace", options)
            else:
                self._player.loadfile(url, "replace")
        except Exception as exc:  # noqa: BLE001 - libmpv 异常统一转错误信号
            self.error.emit(f"加载播放地址失败: {exc}")
            return
        self.playing_changed.emit(True)
        self._timer.start()

    def set_paused(self, paused: bool) -> None:
        try:
            self._player.pause = paused
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"切换播放状态失败: {exc}")
            return
        self.playing_changed.emit(not paused)

    def toggle_pause(self) -> None:
        try:
            self._player.pause = not self._player.pause
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"切换播放状态失败: {exc}")
            return
        self.playing_changed.emit(not self._player.pause)

    def seek(self, position_s: float) -> None:
        try:
            self._player.seek(max(0.0, position_s), "absolute")
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"跳转进度失败: {exc}")

    def set_volume(self, volume: int) -> None:
        try:
            self._player.volume = max(0, min(100, volume))
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"调节音量失败: {exc}")

    def volume(self) -> int:
        try:
            return int(self._player.volume)
        except Exception:  # noqa: BLE001
            return 70

    def stop(self) -> None:
        self._has_media = False
        self._end_emitted = False
        self._idle_ticks = 0
        self._ever_played = False
        self._timer.stop()
        try:
            self._player.command("stop")
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"停止播放失败: {exc}")
        self.playing_changed.emit(False)

    def shutdown(self) -> None:
        """退出前终止 mpv 实例(幂等)。

        直接 terminate() 会与事件线程竞争(实测随机崩溃),按 python-mpv
        文档推荐:先 quit 让事件线程自行收到 SHUTDOWN 退出,再 terminate。
        """
        self._timer.stop()
        try:
            self._player.command("quit")
        except Exception:  # noqa: BLE001 - 核心可能已在退出
            pass
        thread = getattr(self._player, "_event_thread", None)
        if thread is not None:
            thread.join(timeout=2.0)
        try:
            self._player.terminate()
        except Exception:  # noqa: BLE001 - terminate 可能因已退出而抛错
            pass

    # -- 状态查询 ------------------------------------------------------------

    def is_playing(self) -> bool:
        if not self._has_media:
            return False
        try:
            return not bool(self._player.pause) and not bool(self._player.idle_active)
        except Exception:  # noqa: BLE001
            return False

    def position(self) -> float:
        try:
            value = self._player.time_pos
            return float(value) if value is not None else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def duration(self) -> float:
        try:
            value = self._player.duration
            return float(value) if value is not None else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    # -- 轮询上报 ------------------------------------------------------------

    def _on_tick(self) -> None:
        if not self._has_media:
            return

        position_s = self.position()
        if position_s > 0:
            self._ever_played = True
        self.progress.emit(position_s, self.duration())
        if self._end_emitted:
            return

        try:
            eof = bool(self._player.eof_reached)
            idle = bool(self._player.idle_active)
        except Exception:  # noqa: BLE001
            return

        if eof:
            self._emit_ended()
        elif idle:
            # 播完转 idle:time-pos 已归零,靠 ever_played 判定
            if self._ever_played:
                self._emit_ended()
            else:
                # 从未真正开播:加载失败(死链/过期/无音轨),给冷启动留宽限
                self._idle_ticks += 1
                if self._idle_ticks >= self._LOAD_FAIL_GRACE_TICKS:
                    self._end_emitted = True
                    self.playing_changed.emit(False)
                    self.error.emit(
                        "播放失败:无法加载该音频流(链接可能已过期或无音轨)"
                    )
        else:
            self._idle_ticks = 0

    def _emit_ended(self) -> None:
        self._end_emitted = True
        self.playing_changed.emit(False)
        self.track_ended.emit()
