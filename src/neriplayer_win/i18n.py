"""极简 i18n(中英双语):文案表 + 当前语言 + 变更监听。

设计约定:
- 全部界面文案收敛为本模块 _STRINGS 里的键值对 ``(zh, en)``;UI 代码
  一律经 ``tr(key, **kwargs)`` 取文案,kwargs 走 str.format 命名占位
  (两种语言语序不同,禁止位置占位)。
- 当前语言是模块级全局(默认 zh,与 settings.json 的 language 键联动);
  ``set_language`` 变更成功时通知监听者,MainWindow 注册一个监听器做
  全量重翻译(与主题切换的 theme_changed 全量刷新同套路)。
- 缺键兜底:返回键名本身,绝不抛异常(漏译只影响显示,不影响运行)。
- 本模块不得 import 项目内任何模块(player/ui 都反向依赖它);
  新增语言时在此扩充 VALID_LANGUAGES 与文案表即可(0.7 计划)。
"""

from __future__ import annotations

from typing import Callable

LANGUAGE_ZH = "zh"
LANGUAGE_EN = "en"
VALID_LANGUAGES = (LANGUAGE_ZH, LANGUAGE_EN)
DEFAULT_LANGUAGE = LANGUAGE_ZH

# key → (中文, English);按「通用 / 侧栏 / 表格 / 搜索 / 登录 / 列表 /
# 播放 / 播放条 / 队列 / 托盘 / 设置 / 音质 / 网页登录」分节
_STRINGS: dict[str, tuple[str, str]] = {
    # -- 通用 ------------------------------------------------------------------
    "common.ready": ("就绪", "Ready"),
    "common.stopped": ("已停止", "Stopped"),
    # -- 侧栏 ------------------------------------------------------------------
    "sidebar.search": ("搜索", "Search"),
    "sidebar.recent": ("最近", "Recent"),
    "sidebar.netease_playlists": ("网易云 · 歌单", "NetEase · Playlists"),
    "sidebar.netease_subscribed": ("网易云 · 收藏", "NetEase · Favorites"),
    "sidebar.bili_folders": ("B站 · 收藏夹", "Bilibili · Favorites"),
    "sidebar.netease_login": ("扫码登录", "Sign in"),
    "sidebar.bili_login": ("未登录,点击登录", "Not signed in · click to log in"),
    "sidebar.loading_playlists": ("歌单加载中…", "Loading playlists…"),
    "sidebar.loading_subscribed": ("收藏加载中…", "Loading favorites…"),
    "sidebar.loading_folders": ("收藏夹加载中…", "Loading favorites…"),
    "sidebar.watchlater": ("稍后再看", "Watch Later"),
    "sidebar.settings": ("设置", "Settings"),
    "sidebar.daily": ("每日推荐", "Daily Recommendations"),
    # -- 歌曲表 -----------------------------------------------------------------
    "table.title": ("标题", "Title"),
    "table.artist": ("歌手", "Artist"),
    "table.upper": ("UP主", "Uploader"),
    "table.duration": ("时长", "Duration"),
    # -- 搜索 ------------------------------------------------------------------
    "search.placeholder": ("搜索关键词", "Search keywords"),
    "search.source_netease": ("网易云", "NetEase"),
    "search.source_bili": ("B站", "Bilibili"),
    "search.more": ("加载更多", "Load more"),
    "search.empty_hint": ("输入关键词,回车搜索",
                          "Type keywords and press Enter to search"),
    "search.searching_hint": ("正在搜索…", "Searching…"),
    "search.failed_hint": ("搜索失败,换个关键词试试",
                           "Search failed. Try another keyword"),
    "search.keyword_empty": ("请输入搜索关键词", "Please enter a search keyword"),
    "search.searching": ("正在搜索:{keyword}", "Searching: {keyword}"),
    "search.failed": ("搜索失败:{message}", "Search failed: {message}"),
    "search.result_loaded": ("搜索「{keyword}」· 已加载 {count} 首",
                             "Search \"{keyword}\" · {count} loaded"),
    # 「最近」栈里搜索条目的展示前缀。注意:持久化标题仍用规范前缀
    # 「搜索:」(main_window._SEARCH_TITLE_PREFIX),此处只管展示回放
    "search.recent_prefix": ("搜索:", "Search: "),
    # -- 空态 ------------------------------------------------------------------
    "empty.not_logged_in": (
        "登录网易云或B站后,这里会展示你的歌单与收藏夹",
        "Sign in to NetEase or Bilibili to see your playlists and favorites here",
    ),
    "empty.pick_from_sidebar": (
        "从左侧选择歌单或收藏夹,双击即可播放",
        "Pick a playlist or favorites folder on the left, then double-click to play",
    ),
    # -- 网易云登录 --------------------------------------------------------------
    "login.title": ("登录网易云音乐", "Sign in to NetEase Cloud Music"),
    "login.hint": (
        "点击下方按钮打开网页登录窗口,支持扫码 / 手机号等任意方式;\n"
        "登录成功后会自动返回本应用。",
        "Click the button below to open the web login window (QR code, phone "
        "number, etc.);\nthe app will return automatically once you're signed in.",
    ),
    "login.button": ("打开网页登录", "Open web login"),
    "login.waiting": ("等待网页登录完成…", "Waiting for web login…"),
    "login.success": ("登录成功", "Signed in"),
    "login.no_credentials": ("未检测到登录凭据,请重试",
                             "No login credentials detected. Please try again"),
    "login.status.success_fetching": ("登录成功,正在获取账号信息…",
                                      "Signed in. Fetching account info…"),
    "login.status.expired": ("登录已过期,请重新登录",
                             "Session expired. Please sign in again"),
    "login.status.invalid": ("登录态无效,请重新登录",
                             "Session invalid. Please sign in again"),
    "login.status.stale": ("登录态已失效,请重新登录",
                           "Session expired. Please sign in again"),
    "login.status.check_failed": ("检查登录态失败:{message}",
                                  "Failed to check login status: {message}"),
    "login.status.account_failed": ("获取账号信息失败:{message}",
                                    "Failed to fetch account info: {message}"),
    "login.status.logged_in": ("已登录:{name}", "Signed in: {name}"),
    # -- B站登录 ----------------------------------------------------------------
    "bili.no_credentials": ("未检测到B站登录凭据,请重试",
                            "No Bilibili credentials detected. Please try again"),
    "bili.login_success": ("B站登录成功,正在获取账号信息…",
                           "Signed in to Bilibili. Fetching account info…"),
    "bili.account_failed": ("获取B站账号信息失败:{message}",
                            "Failed to fetch Bilibili account info: {message}"),
    "bili.login_invalid": ("B站登录态无效,请重新登录",
                           "Bilibili session invalid. Please sign in again"),
    "bili.logged_in": ("B站已登录:{name},正在读取收藏夹…",
                       "Signed in to Bilibili as {name}. Loading favorites…"),
    "bili.checking": ("正在检查B站登录态…", "Checking Bilibili login status…"),
    "bili.check_failed": ("检查B站登录态失败:{message}",
                          "Failed to check Bilibili login status: {message}"),
    "bili.expired": ("B站登录已过期,请重新登录",
                     "Bilibili session expired. Please sign in again"),
    "bili.expired_sidebar": (
        "B站登录已过期,请点击侧栏「B站 · 收藏夹」重新登录",
        "Bilibili session expired. Click \"Bilibili · Favorites\" in the sidebar "
        "to sign in again",
    ),
    "bili.auth_expired_sidebar": (
        "B站登录态已失效,请点击侧栏「B站 · 收藏夹」重新登录",
        "Bilibili session expired. Click \"Bilibili · Favorites\" in the sidebar "
        "to sign in again",
    ),
    # -- 列表加载 ----------------------------------------------------------------
    "list.loading": ("正在加载:{title}", "Loading: {title}…"),
    "list.loaded": ("{title} · 共 {count} 首", "{title} · {count} songs"),
    "list.songs_failed": ("加载歌曲失败:{message}",
                          "Failed to load songs: {message}"),
    "list.daily_failed": ("加载每日推荐失败:{message}",
                          "Failed to load daily recommendations: {message}"),
    "list.playlists_failed": ("获取歌单失败:{message}",
                              "Failed to fetch playlists: {message}"),
    "list.bili_folders_failed": ("获取B站收藏夹失败:{message}",
                                 "Failed to fetch Bilibili favorites: {message}"),
    "list.bili_folders_loaded": ("B站收藏夹 · 共 {count} 个",
                                 "Bilibili favorites · {count} folders"),
    "list.bili_skipped": ("(跳过 {count} 条不可播内容)",
                          " ({count} unplayable skipped)"),
    "list.folder_failed": ("加载收藏夹失败:{message}",
                           "Failed to load favorites: {message}"),
    "list.watchlater_failed": ("加载稍后再看失败:{message}",
                               "Failed to load Watch Later: {message}"),
    # -- 播放 ------------------------------------------------------------------
    "play.resolving": ("正在解析播放地址:{title}",
                       "Resolving playback URL: {title}"),
    "play.playing": ("正在播放:{title}{quality}", "Now playing: {title}{quality}"),
    "play.preview": ("当前为试听片段(完整播放需开通 VIP)",
                     "Preview clip (VIP required for full playback)"),
    "play.engine_unavailable": ("播放内核不可用", "Player engine unavailable"),
    "play.play_next_menu": ("下一首播放", "Play next"),
    "play.play_next_done": ("下一首播放:{title}(队列第 {position} 位)",
                            "Playing next: {title} (position {position} in queue)"),
    "play.repeat_one": ("单曲循环:{title}", "Repeat one: {title}"),
    "play.finished": ("播放完毕", "Playback finished"),
    "play.mode": ("播放模式:{name}", "Play mode: {name}"),
    "play.error": ("播放错误:{message}", "Playback error: {message}"),
    "play.failed": ("播放失败:{message}", "Playback failed: {message}"),
    "play.failed_retry": (
        "播放失败,切换备用线路重试({position}/{total}):{title}",
        "Playback failed. Trying backup URL ({position}/{total}): {title}",
    ),
    "play.failed_song": ("播放失败:{title}:{message}",
                         "Playback failed: {title}: {message}"),
    "play.failed_title": ("播放失败", "Playback failed"),
    "play.failed_auto_skip": (
        "播放失败:{title}({message}),即将自动跳到下一首…",
        "Playback failed: {title} ({message}). Skipping to next…",
    ),
    "play.circuit_breaker": (
        "连续 {count} 次播放失败,已暂停自动跳过,请检查网络或手动切歌",
        "{count} consecutive failures. Auto-skip paused — check your network "
        "or switch manually",
    ),
    "play.circuit_breaker_title": ("连续播放失败", "Consecutive playback failures"),
    "play.circuit_breaker_tray": (
        "连续 {count} 次播放失败,已暂停自动跳过,\n请检查网络或手动切歌",
        "{count} consecutive failures. Auto-skip paused.\nCheck your network "
        "or switch manually",
    ),
    "play.failed_end": ("播放失败:队列已到尾,停止播放",
                        "Playback failed: end of queue. Stopped"),
    "play.no_url": ("无可用播放地址", "No playable URL"),
    "play.prefetch_expired": ("预取地址已过期,正在重新解析:{title}",
                              "Prefetched URL expired. Re-resolving: {title}"),
    # -- 播放模式(player.queue display_name/button_label 也走这里)-----------
    "mode.sequence": ("顺序播放", "Sequential"),
    "mode.shuffle": ("随机播放", "Shuffle"),
    "mode.repeat_one": ("单曲循环", "Repeat One"),
    "mode.short.sequence": ("顺序", "Seq"),
    "mode.short.shuffle": ("随机", "Shuf"),
    "mode.short.repeat_one": ("单曲", "One"),
    # -- 播放条 -----------------------------------------------------------------
    "player.not_playing": ("未在播放", "Not playing"),
    "player.mode_tooltip": ("播放模式:{name}", "Play mode: {name}"),
    "player.mode_accessible": ("播放模式", "Play mode"),
    "player.prev": ("上一首", "Previous"),
    "player.prev_accessible": ("上一首", "Previous"),
    "player.play": ("播放", "Play"),
    "player.pause": ("暂停", "Pause"),
    "player.play_accessible": ("播放/暂停", "Play/Pause"),
    "player.next": ("下一首", "Next"),
    "player.next_accessible": ("下一首", "Next"),
    "player.queue": ("打开播放队列", "Open play queue"),
    "player.queue_accessible": ("播放队列", "Play queue"),
    "player.volume": ("音量", "Volume"),
    # -- 队列窗口 ----------------------------------------------------------------
    "queue.title": ("播放队列", "Play Queue"),
    "queue.header": ("共 {count} 首 · {mode}", "{count} songs · {mode}"),
    # -- 托盘 ------------------------------------------------------------------
    "tray.toggle": ("播放 / 暂停", "Play / Pause"),
    "tray.prev": ("上一首", "Previous"),
    "tray.next": ("下一首", "Next"),
    "tray.show_main": ("显示主窗口", "Show main window"),
    "tray.exit": ("退出", "Exit"),
    "tray.minimized_body": (
        "已最小化到托盘:双击托盘图标恢复窗口,右键菜单可退出。",
        "Minimized to tray: double-click the tray icon to restore; "
        "right-click menu to exit.",
    ),
    # -- 设置页 -----------------------------------------------------------------
    "settings.close_group": ("关闭行为", "On Close"),
    "settings.close_tooltip": (
        "点窗口右上角 X 时的行为;托盘菜单「退出」始终真正退出",
        "What the window X button does; tray menu \"Exit\" always quits",
    ),
    "settings.close_exit": ("直接退出", "Quit immediately"),
    "settings.close_tray": ("最小化到托盘", "Minimize to tray"),
    "settings.play_group": ("播放", "Playback"),
    "settings.play_mode": ("播放模式:", "Play mode:"),
    "settings.recent_max": ("最近列表数量:", "Recent list size:"),
    "settings.recent_max_tooltip": (
        "侧栏「最近」分区记录的最近播放列表条数;\n缩小即时裁剪存量,扩大不回填已裁条目",
        "Entries kept in the sidebar \"Recent\" section;\nshrinking trims "
        "immediately, enlarging does not restore trimmed entries",
    ),
    "settings.quality_group": ("音质", "Audio Quality"),
    "settings.quality": ("音质偏好:", "Preferred quality:"),
    "settings.quality.lossless": ("无损 FLAC", "Lossless FLAC"),
    "settings.quality.exhigh": ("极高 320K", "Very High 320K"),
    "settings.quality.standard": ("标准 128K", "Standard 128K"),
    "settings.appearance_group": ("外观", "Appearance"),
    "settings.theme": ("主题:", "Theme:"),
    "settings.theme.dark": ("暗色", "Dark"),
    "settings.theme.light": ("亮色", "Light"),
    "settings.dynamic_color": ("播放时跟随封面取色", "Tint theme from cover while playing"),
    "settings.dynamic_color_tooltip": (
        "播放歌曲时从封面提取主色,整个界面随之柔和变色;关闭则始终使用默认主题",
        "Extract the dominant color from the playing song's cover and softly "
        "retint the UI; turn off to always use the default theme",
    ),
    # 语言分组标题刻意双语并列:两种语言的用户都要能找到切换入口
    "settings.language_group": ("语言 · Language", "语言 · Language"),
    "settings.language": ("界面语言:", "Language:"),
    "settings.about_group": ("关于", "About"),
    "settings.about_version": ("<b>NeriPlayer Win</b> 版本 {version}",
                               "<b>NeriPlayer Win</b> Version {version}"),
    "settings.about_license": (
        "本程序为自由软件,依据 GNU GPL-3.0 及以后版本授权发布。\n"
        "衍生自 NeriPlayer(cwuom),感谢上游项目。",
        "This program is free software, licensed under GNU GPL-3.0 or later.\n"
        "Derived from NeriPlayer (cwuom). Thanks to the upstream project.",
    ),
    "settings.about_upstream": ("上游仓库:{url}", "Upstream repository: {url}"),
    # -- 实际生效音质(状态栏后缀)----------------------------------------------
    "quality.lossless": ("无损 FLAC", "Lossless FLAC"),
    "quality.hires": ("Hi-Res", "Hi-Res"),
    "quality.exhigh": ("320K", "320K"),
    "quality.higher": ("192K", "192K"),
    "quality.standard": ("128K", "128K"),
    "quality.jymaster": ("超清母带", "Master"),
    "quality.jyeffect": ("高清环绕声", "Hi-Res Surround"),
    "quality.sky": ("沉浸环绕声", "Immersive Surround"),
    "quality.dolby": ("杜比", "Dolby"),
    # -- 网页登录对话框 -----------------------------------------------------------
    "weblogin.netease.title": ("网易云音乐 · 网页登录",
                               "NetEase Cloud Music · Web Login"),
    "weblogin.netease.hint": (
        "在下方页面中使用任意方式登录(扫码 / 手机号)。检测到登录成功后会自动完成;"
        "若页面加载缓慢请稍候。",
        "Sign in on the page below using any method (QR code / phone number). "
        "The dialog closes automatically once login is detected; please wait "
        "if the page loads slowly.",
    ),
    "weblogin.bili.title": ("哔哩哔哩 · 网页登录", "Bilibili · Web Login"),
    "weblogin.bili.hint": (
        "在下方页面中使用任意方式登录(扫码 / 手机号 / 密码)。"
        "检测到登录成功后会自动完成;若页面加载缓慢请稍候。",
        "Sign in on the page below using any method (QR code / phone / "
        "password). The dialog closes automatically once login is detected; "
        "please wait if the page loads slowly.",
    ),
    "weblogin.done": ("我已完成登录", "I've finished signing in"),
    "weblogin.cancel": ("取消", "Cancel"),
}

_current: str = DEFAULT_LANGUAGE
_listeners: list[Callable[[str], None]] = []


def current_language() -> str:
    """当前语言("zh" | "en");模块加载起默认 zh。"""
    return _current


def set_language(language: str, notify: bool = True) -> bool:
    """切换当前语言;非法值拒绝(False),值未变化时不通知。"""
    global _current
    if language not in VALID_LANGUAGES:
        return False
    changed = language != _current
    _current = language
    if changed and notify:
        for listener in list(_listeners):
            listener(language)
    return True


def tr(key: str, **kwargs: object) -> str:
    """按当前语言取文案;kwargs 为 str.format 命名占位。缺键返回键名。"""
    entry = _STRINGS.get(key)
    if entry is None:
        return key
    text = entry[0] if _current == LANGUAGE_ZH else entry[1]
    return text.format(**kwargs) if kwargs else text


def add_listener(listener: Callable[[str], None]) -> None:
    """注册语言变更监听(参数为新语言);MainWindow 用它驱动全量重翻译。"""
    if listener not in _listeners:
        _listeners.append(listener)


def remove_listener(listener: Callable[[str], None]) -> None:
    """注销监听(窗口销毁时调用,防测试进程内累积)。"""
    if listener in _listeners:
        _listeners.remove(listener)
