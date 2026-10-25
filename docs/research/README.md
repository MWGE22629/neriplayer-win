# M6 / M7 调研总览

本目录四份报告对应 `TODO.md` 的两个立项，供编码时按需查阅。

| 文档 | 对应需求 | 内容 |
|---|---|---|
| `M6-kugou-api.md` | M6 酷狗接入 | 酷狗接口契约。搜索、播放、歌单、收藏全部本机实测打通 |
| `M6-integration-points.md` | M6 酷狗接入 | Windows 代码里所有「只有两个音源」的假设，逐条 file:line 改造清单 |
| `M6-reference-inventory.md` | M6 酷狗接入 | Android 参考实现盘点。酷狗只覆盖歌词，多源架构可借鉴六条 |
| `M7-local-playlists.md` | M7 本地歌单 | 数据模型、侧栏集成、封面方案、六步开工顺序 |

---

## M6 的关键判断

**技术上全部可行，不需要安装酷狗客户端。**

已打通（全部本机实测，用真实会员账号）：

| 能力 | 接口 |
|---|---|
| 搜索 | `songsearch.kugou.com/song_search_v2`，连签名都不校验 |
| 播放地址 | `wwwapi.kugou.com/play/songinfo`，四档音质全通 |
| 音频流 | CDN 不绑 cookie，支持 Range 拖动 |
| 歌单列表 | `gateway/v7/get_all_list` |
| 歌单曲目 | `gateway/v4/get_list_all_file_v3`，单页 300 首 |
| 歌单详情 | `gateway/v3/get_list_info` |
| 登录校验 | `gateway/v1/user_verify` |

**最需要注意的三条**：

**一、酷狗有两套互不通用的登录态。**

| | 网页登录 | 扫码登录（客户端） |
|---|---|---|
| 凭据 | `KugooID` + `t` | `userid` + `token` |
| 签名盐值 | `NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt` | `OIlwieks28dk2k092lksi2UIkp` |
| 可用能力 | 搜索、播放地址 | 歌单、收藏、用户校验 |
| 用错表现 | — | `35005 token invalid` |

扫码登录接口 `login-user.kugou.com/v2/qrcode` 在授权成功时直接下发客户端 token，所以扫一次码就把两套打通了，无需装 PC 客户端。实测工具是 `tools/probe_kugou_qr_login.py`。

**二、凭据 24 小时过期，过期表现像限流。** 过期后所有播放请求返回 `30020`，但无鉴权的元数据接口仍正常，极易误判为签名错。启动时必须预检 `ct + 24h`，否则用户会在某天打开软件时突然发现什么都播不了。

**三、播放必须走网页接口，不要用客户端接口。** 客户端 `v6/priv_url` 返回的是 `.mgg` 加密容器（需 `en_ekey` 解密），不是能直接交给 mpv 的文件。网页 `play/songinfo` 返回明文 mp3/flac，实测四档音质全通。

**代码改造量**（`M6-integration-points.md` §6）：`main_window.py` 约 260 行、`store.py` 约 50 行、新建 `api/kugou/` 约 800 行、i18n 约 20 条、测试改 26 个用例。

三个最高风险点（误判会把酷狗歌当网易云处理）：
1. `main_window.py:2228` `_resolve_song_context` 的 `else` 兜底
2. `main_window.py:2269` `_resolve_and_start` 的 `else` 兜底
3. `main_window.py:2277` 只捕获两种鉴权异常

---

## M7 的关键判断

**没有外部依赖，可以独立开工，与 M6 并行。**

核心决策：新建独立文件 `local_playlists.json`，不塞进 `settings.json`。理由是该文件每次整份覆盖且调用点有 11 处，混入歌单会放大损坏面。

条目标识用 `(source, id)` 加自由 `extra: dict`，B站 `bvid` 与将来酷狗的 `hash`/`album_id` 都放 extra，新增音源不改结构。存完整快照而非只存标识，决定性理由是封面需求。

侧栏「+」按钮推荐自绘命中区（`local-header` 行右侧画「+」，`mousePressEvent` 先判命中再 `super()`），与现有折叠三角同套路。

三个易错点：`_on_sidebar_order_changed` 的 `else` 兜底要改成显式 `elif bili`；`RECENT_KINDS` 必须加 `"local-playlist"`；`_section_header_icon` 不能传 `"local"`（会 KeyError）。

六步开工顺序：数据层 → 侧栏只读展示 → 新建与点开 → 右键添加到 → 封面自动预取 → 手动封面。1→2→3 硬依赖。

---

## 建议的开工顺序

M6 现在没有阻塞点，两个需求可以按依赖自由排。建议：

1. **M7 第一步**（数据层）。无外部依赖，不碰网络，纯本地逻辑，测试好写。
2. **M6 全部**。先按 `M6-integration-points.md` 把三处分派改成显式三分支，再写 `api/kugou/client.py`。契约照抄 `M6-kugou-api.md` §11 的两段可复现代码（已实测跑通）。
3. **M7 第二到六步**。在 M6 改完分派之后做，避免两次大改 `main_window.py` 冲突。

M6 内部建议顺序：登录（两套凭据）→ 搜索 → 播放 → 歌单 → 侧栏分区。

---

## 调研工具

`tools/` 下六个探测脚本，非产品代码，建议保留以便接口变更时重新锚定契约：

| 脚本 | 用途 |
|---|---|
| `probe_kugou_login.py` | 网页登录，导出 cookie |
| `probe_kugou_qr_login.py` | 扫码登录，导出客户端 token |
| `probe_kugou_android.py` | Android 签名 + 歌单接口实测框架 |
| `probe_kugou_api.py` | 带登录态批量发 GET |
| `probe_kugou_capture.py` | 拦截页面真实 XHR 流量 |
| `probe_kugou_inpage.py` | 页面上下文内签名并发请求 |

`tools/*.local.json` 含登录 cookie，已被 `.gitignore` 的 `*.local.json` 覆盖，不进版本库。若不再需要，删除这些 `.local.json` 即可，脚本本身无害。
