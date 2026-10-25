# M6 酷狗接入 · 双源假设盘点与改造清单

调研对象：`D:/Desktop/OpenSourse/NeriPlayer_Win`，代码基线 2026-10-23。
所有行号均取自当前工作区文件实际内容，未凭记忆填写。

## 1. 现状架构（文字版）

```
MainWindow (ui/main_window.py, 3014 行)
├─ 数据层
│   ├─ LocalStore (data/store.py, 435 行)
│   │    ├─ netease.json / bili.json / settings.json
│   │    └─ 设置键白名单 + 校验函数
│   ├─ PlayQueue (player/queue.py, 357 行)  ← 唯一队列，QueueSong 同构条目
│   └─ PlayerEngine (player/engine.py, 338 行) ← mpv，只认 URL + headers
├─ 音源客户端
│   ├─ NeteaseClient (api/netease/client.py, 1202 行)
│   └─ BiliClient    (api/bili/client.py, 1267 行) + selector.py 音质选择
├─ UI
│   ├─ 侧栏 _SidebarTree（QTreeWidget，四分区 + 搜索 + 设置）
│   ├─ 歌曲表（5 列，双击起播 / 右键插播）
│   ├─ 搜索页头（输入框 + _SourceBar 两半来源栏）
│   ├─ 播放条 PlayerBar（封面 CoverLoader 独立实例）
│   └─ 设置页 SettingsPage
└─ 分发中枢（全部集中在 MainWindow）
    ├─ 侧栏点击 → kind 字符串 if 链
    ├─ 列表加载 → 每源一个 _load_xxx 方法
    ├─ 播放解析 → _resolve_song_context（if song.source == "bili"）
    └─ 登录/恢复 → 每源一对 _boot_xxx / _enter_xxx / _handle_xxx_stale
```

关键事实（计数口径：`grep -ci "netease\|bili"`，不区分大小写，按行计）：`main_window.py` 共 3014 行，其中 252 行含 netease/bili 字样（8.4%），35 处是带引号的字面量 `"netease"` / `"bili"`。`data/store.py` 435 行中 52 行，`i18n.py` 344 行中 38 行。全仓没有 `SourceAdapter` / `Protocol` 之类的抽象（`grep -rni "adapter|protocol" src/` 无命中）。

## 2. 逐项改造清单

### 2.1 音源分支与兜底误判（最高风险区）

| # | 位置 | 现状 | 改动性质 |
|---|---|---|---|
| A1 | `ui/main_window.py:2228` `_resolve_song_context` 的 `if song.source == "bili":` | 命中 B站走 `_bili_client.resolve_audio_stream`；**其余全部落到 2245 行的网易云 `resolve_playable_url`**。酷狗歌会被当成网易云 song id 去请求 | 必改。改成三分支或查表分发；`else` 必须换成显式 `elif song.source == "netease"` 并对未知源抛错 |
| A2 | `ui/main_window.py:2245-2261` 网易云分支 | 用 `song.id`（int）+ `song.duration_ms` + `preferred_quality` | 酷狗需要 hash（字符串）与 album_id，int id 放不下 |
| A3 | `ui/main_window.py:2269-2272` `_resolve_and_start` | `if song.source == "bili": _make_bili_play_done else _make_netease_play_done`，**else 同样误判为网易云** | 必改。三分支；`_make_netease_play_done` 里 2296 行调 `_handle_stale_login`（网易云专用登出流程），酷狗必须有自己的 stale 处理器 |
| A4 | `ui/main_window.py:2277` `except (NeteaseAuthRequiredError, BiliAuthRequiredError)` | 只有两种鉴权异常被识别为 `"auth"`；酷狗若抛自己的异常会落到 2279 行的通用 `except Exception` 被当成普通失败，进而自动跳歌而不是提示重新登录 | 必改。要么让酷狗异常继承一个共同的 `SourceAuthRequiredError`，要么在此处追加类型 |
| A5 | `ui/main_window.py:2290-2303` `_make_netease_play_done` / `2305-2318` `_make_bili_play_done` | 两个几乎逐字相同的闭包，只差 stale 处理器与提示文案 | 可合并为参数化工厂 `_make_play_done(song, generation, on_auth)`，或纳入适配器 |
| A6 | `ui/main_window.py:2320-2348` `_start_play` | 只吃 `rotator/headers/status`，本身**无音源判断**，酷狗可直接复用（这是现有设计最干净的一处） | 无需改。但 2346 行 `engine.play_url(url, headers)` 意味着酷狗若需要特殊请求头（签名 Cookie/Referer），由 `_resolve_song_context` 返回的 headers 承担 |
| A7 | `ui/main_window.py:2492-2522` `_on_load_failed` | 逻辑为「有备用候选就轮换 → 预取来源就先重解析一次 → 否则失败」。**无音源分支**，对酷狗同样适用 | 无需改，只要酷狗解析层能给出 `candidate_urls`（参考 B站 `BackupUrlRotator`） |
| A8 | `ui/main_window.py:2524-2564` `_handle_play_failure` | 无音源分支，统一失败出口 | 无需改 |
| A9 | `ui/main_window.py:2566-2576` `_advance_after_failure` | 纯队列语义 | 无需改 |
| A10 | `ui/main_window.py:2431-2468` `_schedule_prefetch_next` | 通过 `_resolve_song_context` 解析，天然继承 A1 的误判 | 无需单独改，修好 A1 即自动正确 |
| A11 | `ui/main_window.py:2470-2486` `_take_prefetched` | 命中校验用 `(source, id)` 二元组。酷狗若 hash 放不进 `id`，此处校验会失真（两首不同 hash 同 id 会被当成同一首） | 必改（随 QueueSong 字段调整） |
| A12 | `ui/main_window.py:1934-1941` `_update_playing_row` | 行匹配同样用 `source + id` | 同上 |
| A13 | `ui/main_window.py:1867-1871` `_fill_song_table` | `if self._table_header_source == "bili"` else 网易云表头；酷狗会显示成「歌手」列（对酷狗是合理的，可接受） | 低优先级，可显式加 `elif` 或保持默认 |
| A14 | `ui/main_window.py:1405-1415` `_brand_header_icon` | `if source == "netease" ... else self._bili_account is not None`，**传 "kugou" 会误判成 B站登录态**，且 1413 行 `_SOFT_BRAND_COLORS[source]` 会 KeyError | 必改 |
| A15 | `ui/main_window.py:1838` `_update_table_empty_state` | `if self._account is None and self._bili_account is None` 判「全未登录」；酷狗登录后仍会显示「登录网易云或B站」空态文案 | 必改 |
| A16 | `ui/main_window.py:1693-1695` 侧栏点击兜底 | `_PAGE_LOGIN if self._account is None else _PAGE_TABLE`，只看网易云账号 | 必改（或该分支不再被酷狗条目命中） |

### 2.2 侧栏构建

结构事实（`_rebuild_sidebar` 从 `1445` 行开始到 `1562` 行结束）：

| # | 位置 | 内容 | 改动性质 |
|---|---|---|---|
| B1 | `main_window.py:195-200` `_HEADER_SECTION` | `"netease-header"/"netease-subscribed-header"/"bili-header"/"recent-header"` → 分区键 | 必加 `"kugou-header": "kugou"` |
| B2 | `main_window.py:643-647` `_SidebarTree._SECTION_CHILD_KIND` | 分区键 → 可拖拽子节点 kind。**酷狗分区不加入则其子项不可拖拽排序**（`682` 行白名单过滤） | 按需加 `"kugou": "kugou-playlist"` |
| B3 | `main_window.py:1449-1454` 搜索顶层项 | `("search", None)` | 无需改 |
| B4 | `main_window.py:1458-1471` 「最近」分区 | 依赖 `self._recent` 非空 | 无需改 |
| B5 | `main_window.py:1473-1493` 网易云分区 | 分区头 + 登录占位/歌单/加载占位，kind 分别是 `netease-login` / `netease-playlist` / `netease-loading` | 酷狗分区照抄这一段的骨架 |
| B6 | `main_window.py:1496-1516` 收藏分区 | 仅网易云登录时出现；子项 `netease-daily` 固定首位 | 若酷狗也有「收藏/喜欢」可并入同一分区或另开 |
| B7 | `main_window.py:1518-1543` B站分区 | 分区头 + 登录占位/稍后再看/收藏夹/加载占位，kind `bili-login` / `bili-watchlater` / `bili-folder` / `bili-loading` | 酷狗分区的直接模板 |
| B8 | `main_window.py:1545-1550` 「设置」顶层项 | 固定在末尾 | **酷狗分区必须插在 1545 行之前**，否则「设置」被挤到中间 |
| B9 | `main_window.py:1554-1560` 折叠状态回填 | `expanded.get(section, True)`，对未登记 section 默认展开，容错 | 无需改，但建议在 `store._SIDEBAR_SECTIONS` 登记 `kugou`（见 C1） |
| B10 | `main_window.py:1564-1574` `_SIDEBAR_KIND_ICON` | kind → 图标名映射；**未登记的 kind 得到 None，不设图标**（不报错，但子项空白） | 加 `kugou-login` / `kugou-playlist` 等 |
| B11 | `main_window.py:1576-1580` `_HEADER_BRAND` | 分区头 kind → 品牌标名（要等于 `assets/icons/<name>.svg` 的文件名） | 加 `"kugou-header": "kugou"`，并新增 `assets/icons/kugou.svg` |
| B12 | `main_window.py:1403` `_SOFT_BRAND_COLORS` | `{"netease": "#E05A5A", "bilibili": "#5AA9E0"}` | 加 `"kugou": "#3B8FE8"`（酷狗品牌蓝，取值以设计确认为准） |
| B13 | `main_window.py:1440` `_add_child_item` | 登录占位白名单 `("netease-login", "bili-login")` 决定「可选但不参与拖拽」 | 加 `"kugou-login"` |
| B14 | `main_window.py:1609-1627` `_sidebar_list_ref` | 按 kind 分支查内存列表取干净标题；**未知 kind 走 1627 行兜底 `item.text(0)`**（会把计数括号带进「最近」栈） | 加 `elif kind == "kugou-playlist"` 分支 |
| B15 | `main_window.py:1629-1646` `_open_list_ref` | kind if 链：`netease-playlist`/`netease-subscribed-playlist`/`netease-daily`/`bili-folder`/`bili-watchlater`/`search`；**未知 kind 静默无动作** | 加 `elif list_ref.kind == "kugou-playlist"` |
| B16 | `main_window.py:1648-1695` `_on_sidebar_item_clicked` | 每条 kind 一个 if；`bili-login` 有专门分支（1684-1689） | 加 `kugou-login` 分支 |
| B17 | `main_window.py:1652` 分区头判定 | `kind in _HEADER_SECTION`，靠 B1 自动生效 | 无需改 |
| B18 | `main_window.py:706-708` / `724-729` `_SidebarTree` 拖放与 `section_of` | 全部经 `_HEADER_SECTION` / `_SECTION_CHILD_KIND`，B1+B2 补齐即通 | 无需改 |
| B19 | `main_window.py:1778-1800` `_on_sidebar_order_changed` | `if netease / elif netease-subscribed / else bili`，**else 会把酷狗分区当成 B站并写 `bili_folder_order`** | 必改：换成显式 `elif section == "bili"` + 新增酷狗分支 |
| B20 | `main_window.py:1802-1812` `_apply_stored_order` | 通用纯函数，只需传新的 setting 键 | 无需改 |

隐式契约（新增分区必须满足）：
1. kind 命名 `<source>-<role>`，分区头必须是 `<section>-header`（`find_header` 在 `748-755` 行按 `f"{section}-header"` 拼串查找，硬约束）。
2. payload 类型：可排序子项必须是 `int`（`section_child_ids` 在 `745` 行 `int(payload)`）；固定项用 `None`；「最近」条目用三元组 `(kind, id, title)`（`1470` 行）。
3. section key 必须同时出现在 `_HEADER_SECTION`（main_window）与 `store._SIDEBAR_SECTIONS`（store），否则折叠状态不落盘且默认值缺键。
4. 登录占位 kind 必须进 `_add_child_item` 的 1440 行白名单，否则会被拖拽。
5. 排序设置键需进 `store._SETTING_ID_ORDERS`（`store.py:87-91`），否则保存时被白名单丢弃。

### 2.3 双源搜索

| # | 位置 | 现状 | 改动性质 |
|---|---|---|---|
| C1 | `main_window.py:921-936` | 两个按钮硬编码，`_search_group` 只加两个 | 加第三个按钮 + `addButton` |
| C2 | `main_window.py:582-622` `_SourceBar` | 构造函数签名写死 `(netease_button, bili_button)`，`598-602` 行两个 `addWidget(..., 1)` | 必改：改成接收 `list[QPushButton]` 均分，否则第三个按钮没位置 |
| C3 | `main_window.py:2032-2041` `_on_search_source_changed` | `setChecked(source == "netease")` / `(source == "bili")` 两行 | 改成按按钮表遍历 |
| C4 | `main_window.py:2043-2060` `_update_source_indicator` | `button = bili if source == "bili" else netease`，**未知来源（酷狗）会落到网易云按钮**，指示线画错位置 | 必改：`{source: button}` 查表 |
| C5 | `main_window.py:2078-2113` `_run_search` | `if source == "bili": ... else: 网易云` | 加 `elif source == "kugou"`，`else` 收紧 |
| C6 | `main_window.py:2703-2704` `_on_language_changed` | 逐按钮 `setText` | 加一行 |
| C7 | `i18n.py:51-52` | `search.source_netease` / `search.source_bili` | 加 `search.source_kugou` |
| C8 | `theme.py:309-329` | `#searchSourceBtn` / `#searchSourceIndicator` QSS，与按钮数量无关 | 无需改 |
| C9 | `main_window.py:927` | 初始选中网易云 | 无需改 |

指示线对位逻辑：`2050-2051` 行用按钮自身文字宽算线宽（`max(24, min(button.width(), text_width + 28))`），三等分后每段变窄，「酷狗」两字仍够 24px 下限，安全。`_SourceBar` 均分依赖 `addWidget(button, 1)`，C2 不改则第三个按钮宽度为 0。

### 2.4 音质

| # | 位置 | 现状 | 改动性质 |
|---|---|---|---|
| D1 | `data/store.py:70` `_VALID_PLAY_QUALITIES = ("standard", "exhigh", "lossless")` | 三档，且是**网易云档位键** | 决策点：沿用三档（酷狗做换算）还是扩档。若扩档需同步 `settings_page._QUALITY_ITEMS`（`49-53`）、`test_store.py:175-184` 的非法值断言 |
| D2 | `ui/settings_page.py:49-53` `_QUALITY_ITEMS` | 三项下拉 | 若扩档在此加项 + `i18n` 加 `settings.quality.*` |
| D3 | `ui/settings_page.py:47-48` 注释 | 明写「B站播放时由 selector 换算」 | 注释需补酷狗换算位置 |
| D4 | `api/bili/selector.py:112-125` `bili_quality_key_from_netease_level` | 网易云 → B站偏好键，`105-109` 行映射表 | 酷狗同理需要 `kugou_quality_key_from_netease_level`，**建议放 `api/kugou/selector.py`，与 B站同构**（纯函数、便于单测） |
| D5 | `main_window.py:2227` | `self._settings.get(SETTING_PLAY_QUALITY, "lossless")` | 无需改 |
| D6 | `main_window.py:204-213` `_NETEASE_LEVEL_KEYS` + `222-228` `_netease_quality_suffix` | 网易云实际生效档位 → 状态栏后缀 | 酷狗需 `_KUGOU_LEVEL_KEYS` + `_kugou_quality_suffix` |
| D7 | `main_window.py:216-219` `_BILI_QUALITY_TAG_KEYS` + `231-238` `_bili_quality_suffix` | B站后缀（tag + bitrate） | 同上，酷狗通常有「标准/高品/无损/Hi-Res」标签 |
| D8 | `main_window.py:2240-2243` / `2252-2256` | `tr("play.playing", title=..., quality=...)` | 复用，无需改 |
| D9 | `i18n.py:273-281` | `quality.*` 文案表（lossless/hires/exhigh/higher/standard/jymaster/jyeffect/sky/dolby） | 酷狗档位名若与现有键重合可直接复用；新增档位（如 `quality.kugou_hires`）在此加 |

### 2.5 登录与启动恢复

| # | 位置 | 现状 | 改动性质 |
|---|---|---|---|
| E1 | `data/store.py:28-29` | `_NETEASE_FILE = "netease.json"` / `_BILI_FILE = "bili.json"` | 加 `_KUGOU_FILE = "kugou.json"` |
| E2 | `data/store.py:233-235` | 路径属性在 `__init__` 里预置 | 加 `self._kugou_path` |
| E3 | `data/store.py:251-301` netease 三件套 / `305-362` bili 三件套 | `load_xxx` / `save_xxx` / `clear_xxx`，各约 50 行，几乎同构，差异只在判定 cookie 名（`112` 行 `MUSIC_U` vs `326` 行 `SESSDATA`）与是否注入 `os/appver`（`266` 行 vs `323-325`） | 酷狗要复制第三套；也可先抽 `_load_bundle(path, required_keys)` 通用实现 |
| E4 | `data/store.py:112-114` | `_LOGIN_COOKIE_KEYS` / `_BILI_LOGIN_COOKIE_KEYS`；后者实际未被引用（`267`/`283` 只用前者，`326`/`344` 硬编码 `"SESSDATA"`） | 加 `_KUGOU_LOGIN_COOKIE_KEYS`（酷狗网页登录判据通常是 `KuGoo` / `kg_mid`，须实测锚定） |
| E5 | `ui/browser_login.py:51-63` `WebLoginConfig` | 已是通用结构：`title/login_url/success_cookie/cookie_host_suffix/hint/user_agent/interceptor_factory` | **无需改**，新增 `kugou_web_login()` 工厂即可（参照 `86-96` 行 `bili_web_login`） |
| E6 | `ui/browser_login.py:73-83` / `86-96` | 两个配置工厂 | 加第三个 |
| E7 | `ui/login_page.py:19-90` | 只服务网易云（`24` 行 `client: NeteaseClient`，`79` 行写死 `netease_web_login()`） | 酷狗若走侧栏登录（B站模式）则**无需改**；若要复用登录页需泛化 |
| E8 | `main_window.py:1067-1078` `_boot_from_store` / `1086-1097` `_boot_bili_from_store` | 各 12 行同构 | 加 `_boot_kugou_from_store` |
| E9 | `main_window.py:1056-1057` `__init__` 末尾 | 两次调用 | 加第三次调用 |
| E10 | `main_window.py:1080-1084` / `1099-1103` | `_on_boot_status` / `_on_bili_boot_status` | 加酷狗版 |
| E11 | `main_window.py:1125-1136` `_enter_logged_in` / `1181-1191` `_enter_bili_logged_in` | 两者行为不同：网易云版会清表、清队列、切到 TABLE 页；B站版只重建侧栏 + 拉收藏夹 | 酷狗应对齐 B站版（不抢占主区域） |
| E12 | `main_window.py:1138-1151` `_handle_stale_login` / `1193-1200` `_handle_bili_stale_login` | 网易云版会踢回登录页；B站版只重建侧栏 | 酷狗对齐 B站版 |
| E13 | `main_window.py:1202-1221` `_handle_bili_section_click` + `_on_bili_section_status` | 「有残留 cookie 先验证，否则弹登录」的两段式 | 酷狗照抄 |
| E14 | `main_window.py:1155-1173` `_open_bili_login_dialog` / `_on_bili_cookies` | 弹窗 + 判 `SESSDATA` + 存 + 异步验账号 | 酷狗照抄（判据 cookie 换） |
| E15 | `main_window.py:3012-3013` `closeEvent` | `self._client.close()` / `self._bili_client.close()` | 加 `self._kugou_client.close()` |
| E16 | `main_window.py:789-799` `__init__` 的客户端字段 | `_client/_account/_playlists/_subscribed_playlists` + `_bili_client/_bili_account/_bili_folders/_bili_watchlater_count` | 加 `_kugou_client/_kugou_account/_kugou_playlists`（酷狗无「稍后再看」则不需要计数字段） |
| E17 | `i18n.py:103-127` B站登录文案 12 条 | 每源一套 | 酷狗需 6~8 条（成功/失效/检查失败/账号失败/未登录侧栏/过期侧栏/鉴权过期侧栏） |

链路是否可抽象：网易云与 B站的**登录链路差异是真实的**（网易云抢主区域、B站不抢；网易云走 `LoginPage`，B站走侧栏弹窗）。真正同构的只有 store 三件套（E3）与 boot 三件套（E8-E10），各约 50 行 + 36 行。抽通用的收益集中在 store 层。

### 2.6 播放队列 QueueSong

`player/queue.py:58-68`：

```python
source: str   # "netease" | "bili"
id: int       # 网易云 song id / B站 avid
bvid: str     # B站 only
title/artist/duration_ms/cover_url
```

酷狗的两个标识：`hash`（32 位十六进制字符串）与 `album_id`（int）。
结论：**`id: int` 装不下 hash，必须加字段**。

| # | 方案 | 影响 |
|---|---|---|
| F1 | 加 `extra_id: str = ""`（酷狗 hash 放这里，`id` 放 album_id 或 0） | `frozen dataclass` 带默认值，现有 11 处构造（源码 2 处：`main_window.py:245`、`254`；测试 9 处：`tests/test_queue.py:36`、`test_covers.py:279`、`test_dynamic_theme.py:206`、`test_motion.py:44/469/490`、`test_main_window_layout.py:716/1085`、`test_search.py:390`）全部不用改，零破坏 |
| F2 | 把 `id` 改成 `int | str` | 破坏 `section_child_ids` 的 `int(payload)`、`_take_prefetched` 的元组比较语义（虽仍可比较，但 `id` 类型混杂），不推荐 |
| F3 | 加 `kugou_hash` 具名字段 | 语义最清楚，但等于把第三源写进核心数据结构，M7 本地歌单再扩源时要重复 |

对 `replace` / `insert_next` / 序列化的影响：
- `replace`（`124-132`）与 `insert_next`（`142-164`）只搬引用，不看字段，**无影响**。
- 队列**不做持久化**（`grep -n "queue|QueueSong" data/store.py` 仅命中注释），无序列化影响。
- 受影响的是**标识比较**：`main_window.py:1938`（行高亮）与 `2479-2482`（预取命中）。若 hash 放在 `extra_id`，这两处需改成 `(source, id, extra_id)` 三元组，否则同 album 不同歌会互相误匹配。

### 2.7 封面

| # | 位置 | 现状 | 改动性质 |
|---|---|---|---|
| G1 | `ui/covers.py:113-122` `build_thumb_url` | 只认 `music.126.net`/`163.com`（`117`）与 `hdslb.com`（`120`） | 加酷狗 CDN 规则（如 `imge.kugou.com` 的 `/storage/uploadimg/` 缩略约定）。不认识就原样返回，功能可用但流量与内存偏大 |
| G2 | `ui/covers.py:205-227` `_load_sync` | URL → sha1 → 磁盘缓存；与音源无关 | 无需改 |
| G3 | `ui/covers.py:35-44` | 96px 缩略、32MiB LRU、512 条内存 | 无需改 |
| G4 | `main_window.py:835-843` | 三个 CoverLoader 实例（表格/播放条/种子） | 无需改 |
| G5 | `main_window.py:1894-1896` `_preload_table_covers` | 按行序传 `song.cover_url` | 无需改，前提是 `_kugou_song_to_queue` 填了 `cover_url` |
| G6 | `ui/player_bar.py:551-568` `set_cover` / `_on_cover_ready` | 与音源无关 | 无需改 |
| G7 | `main_window.py:2380-2396` `_request_dynamic_seed` | 与音源无关，无封面则回退静态 | 无需改 |
| G8 | 歌曲表封面列 | `main_window.py:881` 建空 item，`1898-1924` 回调点亮 | 无需改 |

### 2.8 「最近」列表

| # | 位置 | 现状 | 改动性质 |
|---|---|---|---|
| H1 | `data/store.py:78-85` `RECENT_KINDS` | 六项白名单，**校验在 `196` 行 `kind not in RECENT_KINDS` 直接丢弃** | 必加 `"kugou-playlist"`，否则酷狗列表起播后压不进「最近」且重启即丢 |
| H2 | `main_window.py:2352-2376` `_push_recent` | 去重键 `(kind, id)`（`2363`），与音源无关 | 无需改（但若酷狗 id 是 0，同 kind 多列表会互相覆盖，需 hash 或唯一 id） |
| H3 | `main_window.py:1458-1471` 最近分区渲染 | `ref.kind == "search"` 特殊处理，其余按 `_SIDEBAR_KIND_ICON.get(role[1][0])` 取图标 | 加图标映射即可 |
| H4 | `main_window.py:1677-1683` 最近条目点击 | 走 `_open_list_ref`，靠 B15 分发 | 无需改 |
| H5 | `main_window.py:2646-2657` `_on_settings_recent_max` | 与音源无关 | 无需改 |

### 2.9 其他隐性双源假设

| # | 位置 | 内容 |
|---|---|---|
| I1 | `ui/icons.py:52` `BRAND_ICON_NAMES = ("bilibili", "netease")` | 该常量目前**无人引用**（`grep` 只在定义处命中），加酷狗时顺手更新或删除 |
| I2 | `ui/icons.py:30-51` `ICON_NAMES` | 测试 `test_ui_assets.py:36` 逐项断言文件存在；新增 `kugou` 品牌标需同步 |
| I3 | `scripts/make_assets.py:65-71` `_BRAND_ICONS` | 品牌标来源清单；酷狗标可从 `reference/NeriPlayer-Android/site/assets/img/desktop/icons/ic_kugou.svg`（已存在，479 字节）取用 |
| I4 | `ui/main_window.py:1576-1580` `_HEADER_BRAND` 与 `icons.py` 文件名的耦合 | 映射值必须是图标文件名 |
| I5 | `i18n.py:68-71` `empty.not_logged_in` | 文案写死「网易云或B站」 |
| I6 | `i18n.py:35-37` 侧栏文案 | 每源 2 条（分区头 + 登录占位） |
| I7 | `main_window.py:825` / `828` | `_table_header_source` / `_search_source` 默认 `"netease"`，可接受 |

## 3. 抽象方案对比与建议

### 方案 A：沿用分支式，加第三分支

工作量估算（按需改动行数）：

| 模块 | 行数 |
|---|---|
| `_resolve_song_context` + `_resolve_and_start` + `_make_*_play_done` | 约 60 行内新增/改写 |
| 侧栏 `_rebuild_sidebar` 一段 + 5 张映射表 | 约 45 行 |
| 搜索按钮 + `_SourceBar` + 3 个函数 | 约 40 行 |
| 音质映射 + 后缀 + 设置页 | 约 40 行 |
| 登录/恢复/失效 6 个方法 + store 三件套 | 约 170 行 |
| 队列字段 + 2 处标识比较 | 约 15 行 |
| 封面 CDN 规则 + 图标资产 | 约 20 行 |
| i18n 文案 | 约 20 条 |
| **合计** | **约 410 行新增，其中复制粘贴型约 300 行** |

### 方案 B：抽 `SourceAdapter` 协议

协议面（按现有调用点归纳，全部来自实际代码）：

```
login_status() -> Account | None
playlists() -> list[Playlist]
tracks(list_id) -> list[Song]
resolve(song, quality) -> (candidate_urls, headers, status_text)   # 对应 _resolve_song_context
search(keyword, page) -> (songs, has_more)                         # 对应 _run_search 两分支
quality_key(quality) -> str                                        # 对应 bili_quality_key_from_netease_level
auth_error_type / stale_handler / section_meta(kind, icon, brand)
```

改造代价：
- 需要把 `_resolve_song_context`（`2218-2261`，44 行）、`_run_search`（`2065-2122`，58 行）、`_open_list_ref`（`1629-1646`，18 行）、`_on_sidebar_item_clicked`（`1648-1695`，48 行）四处 if 链改成分发表，并同步改 12 处测试的猴补路径（`_forbid_network` 在 `test_main_window_layout.py:876-883` 直接 patch `window._client.resolve_playable_url` 与 `window._bili_client.resolve_audio_stream`，改成适配器后这些 patch 全部失效）。
- 净收益要到第四源才明显。当前是第三源。

判断依据：
1. **改动点数量**：分支式约 410 行，其中 300 行是可直接复制的模板；适配器式前期约 350 行框架 + 250 行实现，总量相近但框架部分要同时改动既有两源的全部调用点（约 200 行存量代码），**回归面翻倍**。
2. **现有耦合程度**：分发逻辑 100% 集中在 `MainWindow` 单类内，方法边界清晰（`_resolve_song_context` 已是「一源一函数」的雏形），但 UI 状态（`_account`/`_bili_account`）直接写在 `MainWindow` 字段里，抽适配器必须连带迁移这些状态，收益被稀释。
3. **回归风险**：现有测试对 `window._client` / `window._bili_client` 的属性名有 15 处以上直接依赖（`_capture_async` / `_forbid_network` / `_mock_netease_search` / `_mock_bili_search`），适配器改名会连带改测试，把「新增功能」变成「重构 + 新增」两件事。

**建议：方案 A 为主，附带两个局部抽象。**

1. 音源接入用分支式，第三分支显式写 `elif`，**禁止 `else` 兜底**（A1/A3 是本次最高优先修复项）。
2. 局部抽象一：`data/store.py` 的 cookie 三件套（`251-362`，112 行）抽成 `_load_auth_bundle(path, required_keys, fallback)` / `_save_auth_bundle` / `_clear_auth_bundle`，酷狗直接复用，省约 50 行复制。
3. 局部抽象二：音质换算按 B站现成模式，新建 `api/kugou/selector.py`，纯函数 `kugou_quality_key_from_netease_level` + `select_stream_by_preference`，与 `api/bili/selector.py:112-237` 同构（该文件 237 行，其中音质选择 169-237 可作模板）。
4. 若 M7 本地歌单再引入跨源混收，届时再做 `SourceAdapter`（那时 `QueueSong` 的 `source` 字段已经成为多值，重构窗口更合适）。

## 4. 测试影响清单

### 4.1 必然失效（断言数量或顶层结构）

| 文件 | 用例 | 行 | 失效原因 |
|---|---|---|---|
| `tests/test_main_window_layout.py` | `TestSidebarTreeStructure::test_three_sections_logged_out` | 83（断言 90-91） | `topLevelItemCount() == 4` 与四元组解包，加酷狗分区后变 5 |
| 同上 | `test_loading_placeholders` | 111（断言 116、126） | `topLevelItem(1)` / `topLevelItem(3)` 的硬索引 |
| 同上 | `TestSidebarCollapse::test_header_click_collapses_and_persists` | 224（断言 242-247） | 断言 `sidebar_expanded` 完整字典只有 4 键 |
| 同上 | `test_stored_collapsed_state_applied_on_startup` | 255（断言 264-265） | 硬索引 1/2 |
| 同上 | `TestSubscribedSection::test_subscribed_rendered_parallel_to_created` | 374（断言 390-392） | `topLevelItemCount() == 5` 与 `range(1,5)` |
| 同上 | `test_empty_subscribed_shows_placeholder` | 414（断言 421） | `topLevelItem(2)` |
| 同上 | `test_stale_login_clears_subscribed` | 431（断言 441） | `topLevelItemCount() == 4` |
| 同上 | `test_subscribed_playlist_click_starts_load` | 445（断言 455） | `topLevelItem(2).child(0)` |
| 同上 | `TestStoredOrderApplied::test_playlists_sorted_and_pruned` | 277（断言 289） | `topLevelItem(1)` |
| 同上 | `test_subscribed_playlists_sorted_and_pruned` | 309（断言 323） | `topLevelItem(2)` |
| 同上 | `TestRecentSection::test_start_play_pushes_queue_list` | 1343（断言 1352） | `topLevelItem(1)` 假定「最近」在搜索之下 |
| 同上 | `test_browse_only_never_pushes` | 1368（断言 1374） | `topLevelItem(1)` 断言是 netease-header |
| 同上 | `test_recent_item_click_loads_list` | 1431（断言 1439） | `topLevelItem(1).child(0)` |
| 同上 | `test_recent_survives_restart` | 1454（断言 1465） | `topLevelItem(1)` |
| 同上 | `TestDailyRecommendSection::_daily_item` | 1478（断言 1481） | `topLevelItem(2)` |
| 同上 | `TestSidebarCollapse::test_every_section_arrow_rotates_on_toggle` | 166（循环 182） | 若断言列表扩为 5 分区需同步；不改则漏测酷狗角标 |
| 同上 | `test_header_icons_carry_hidpi_backing` | 203（循环 214） | 同上 |
| `tests/test_store.py` | `TestLocalStoreSettings::test_other_settings_survive_appearance_save` | 186（断言 189-205） | 整字典等值断言，加键即失败 |
| 同上 | `TestSidebarExpandedSetting::test_defaults` | 313（断言 315-320） | 整字典等值 |
| 同上 | `test_roundtrip` | 322（断言 328-329） | 整字典等值 |
| 同上 | `test_per_key_fallback_on_bad_values` | 331（断言 342-347） | 整字典等值 |
| 同上 | `test_non_dict_falls_back` | 349（断言 355-360） | 整字典等值 |
| `tests/test_queue.py` | `TestSettingsStore::test_unknown_keys_ignored_on_save` | 558（断言 565-577） | `set(raw)` 精确集合 |
| `tests/test_i18n.py` | `TestI18nModule::test_every_key_has_both_languages` | 94 | 新文案键必须双语齐全（会失败，属期望行为） |
| `tests/test_ui_assets.py` | `TestIconAssets::test_icon_file_exists_and_loads`（参数化 `ICON_NAMES`） | 36-39 | 新增 `kugou` 到 `ICON_NAMES` 后需有对应 SVG |
| 同上 | `test_total_asset_size_budget` | 46-53 | 当前资产 50 620 字节，预算 512 000，加 1 个 SVG（<2KB）安全 |

### 4.2 条件失效（取决于实现方式）

| 文件 | 用例 | 行 | 条件 |
|---|---|---|---|
| `tests/test_search.py` | `TestSearchPageWiring::test_source_indicator_slides_to_selection` | 353（断言 364、368） | 断言「网易云在半宽左、B站在半宽右」。`_SourceBar` 改三等分后，B站按钮中心移向右侧但仍在 `width/2` 右侧，断言 `center > bar.width()/2` 仍成立；若改成不等宽则失败 |
| 同上 | `test_open_shows_header_with_idle_hint` | 234（断言 243） | 只断言网易云按钮高度，安全 |
| `tests/test_i18n.py` | `TestMainWindowLanguageSwitch::test_toggle_switch_retranslates_and_persists` | 228（断言 243） | 只断言网易云按钮文字，安全；但若改成遍历按钮表，测试不受影响 |
| `tests/test_main_window_layout.py` | `TestPlayFailureHandling` 全组（736-846） | 见备注 | 若把 `_make_netease_play_done` 重命名/合并，`test_stale_resolve_result_ignored`（790，调 `_make_netease_play_done` 于 802/807）直接 AttributeError |
| 同上 | `TestPrefetchScheduling` / `TestPrefetchConsume` / `TestPrefetchStaleUrlFallback` 全组（901-1220） | 见备注 | `_forbid_network`（876-883）patch 的是 `window._client.resolve_playable_url` 与 `window._bili_client.resolve_audio_stream`；若抽适配器，此处需改 patch 目标 |
| `tests/test_main_window_layout.py` | `TestSidebarDropConstraints` / `TestSidebarMoveChild`（605-680） | 见备注 | 用独立构造的树（`_make_standalone_tree` 562-587），只有 3 个顶层项，不受酷狗分区影响，安全 |
| `tests/test_store.py` | `test_play_quality_roundtrip` / `test_invalid_play_quality_falls_back`（166、175） | 见备注 | 若音质档位扩域，`"hires"` 由非法变合法，断言 `load_settings()["play_quality"] == "lossless"`（181 行）失败 |
| `tests/test_queue.py` | `song()` 工厂（35-39） | 见备注 | 若 `QueueSong` 加必填字段则 9 处构造全崩；用带默认值的新字段则安全（F1 方案） |

## 5. 开工顺序建议

1. **修兜底误判**（A1、A3、A4、A14、A15、B19）：纯防御性改动，不新增音源也能做，可先合并一批。这一步做完，「酷狗歌被当成网易云」这类事故面就关了。
2. **数据结构与持久化**（F1 加 `extra_id`、H1 `RECENT_KINDS`、E1-E4 store 三件套、C1 `_SIDEBAR_SECTIONS`）：一次性把 schema 变更做完，配套改测试 4.1 中的整字典断言，避免后续反复动测试。
3. **API 层**（`api/kugou/` 新模块 + `selector.py` 音质换算）：按 AGENTS.md 要求，接口契约以本机抓包实测锚定，不凭猜测写。参考实现里酷狗只有歌词模块（`reference/.../KugouLyricsClient.kt` 等），无播放音源可对照。
4. **侧栏与登录**（B1-B20、E5-E17）：酷狗分区照 B站分区模板，登录照 B站侧栏两段式。此阶段可独立验证「能登录、能列出歌单」。
5. **播放链路**（A2、A5-A11、F1 的两处标识比较）：`_start_play`/`_on_load_failed`/`_handle_play_failure` 无需改，只需补 `_resolve_song_context` 的第三分支与 `_make_kugou_play_done`。
6. **搜索**（C1-C9）：按钮、`_SourceBar` 改造、`_run_search` 第三分支、指示线查表。
7. **音质与封面**（D1-D9、G1）：档位映射、状态栏后缀、`build_thumb_url` CDN 规则、图标资产。
8. **文案与测试补齐**：i18n 双语、`ICON_NAMES`、新增用例覆盖「酷狗歌不被当成网易云」（直接断言 `_resolve_song_context` 对 `source="kugou"` 不调 `_client`）。

## 6. 附：改动点计数汇总

| 模块 | 必改处数 | 约行数 |
|---|---|---|
| `ui/main_window.py` 音源分支 | 6 | 60 |
| `ui/main_window.py` 侧栏 | 11 | 45 |
| `ui/main_window.py` 搜索 | 5 | 40 |
| `ui/main_window.py` 登录/恢复 | 10 | 90 |
| `ui/main_window.py` 音质后缀 | 2 | 25 |
| `data/store.py` | 6 | 50 |
| `player/queue.py` | 1 | 1 |
| `ui/covers.py` | 1 | 5 |
| `ui/icons.py` + 资产 | 2 | 3 |
| `i18n.py` | 约 20 条 | 20 |
| `api/kugou/`（新建） | 见备注 | 约 800（估计，视接口而定） |
| 测试改动 | 26 个用例 | 约 60 |
