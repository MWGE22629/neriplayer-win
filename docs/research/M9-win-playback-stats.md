# M9 Windows 播放统计调研

调研对象：NeriPlayer Win `src/neriplayer_win`，目标功能为设置页入口 + 播放统计列表（表格列把「作者」「时长」换成「播放时长」「播放次数」）。

调研范围：`src/neriplayer_win/` 只读；Android 端口径直接引用同目录 `M9-android-playback-stats.md`，不重复调研。本文只给结论与落点，不改现有代码。

## 1. 需求拆解

原话：「neri win 提供播放统计，播放歌曲的次数，时长日d时:分:秒，入口放在设置页面，点击后显示歌单列表的形式，但把作者和歌曲时长栏换成播放时长、播放次数栏」

拆成可验收条目：

| 编号 | 条目 | 验收方式 |
| --- | --- | --- |
| R1 | 播放一首歌累计「本次实际收听时长」与「播放次数」，进程重启后仍在 | 手工播放 → 退出 → 重启 → 数值不丢 |
| R2 | 时长按 `d天 hh:mm:ss` 展示（不足一天省略「d天」），精确到秒 | 同一条记录两个时刻截图对比 |
| R3 | 设置页新增「播放统计」入口，点击进入统计页 | 侧栏点「设置」→ 页面内找到入口 → 点击切页 |
| R4 | 统计页复用现有歌曲表形态：序号 / 封面 / 标题 / 播放时长 / 播放次数，五列 | 与歌单页并排比对列头宽度与行高 |
| R5 | 「作者」列与「歌曲时长」列在统计页不存在，被播放时长、播放次数替代 | 列头文字与列数检查 |
| R6 | 统计页按播放次数降序排列，列头可点击改排序 | 点列头切换升降序 |
| R7 | 统计不因歌单删除、歌曲改名、音源切换而崩溃或串行 | 删歌单后统计行仍在；同一 id 改标题后下次播放刷新标题 |
| R8 | 提供清空统计入口，二次确认 | 设置页「清空播放统计」→ 弹确认 → 列表变空、文件被重写 |
| R9 | 空数据时显示空态文案，不留空白表 | 全新数据目录下打开统计页 |

「日d时:分:秒」是时长展示格式，不是按天分桶的需求。原文里没有「按天查看/今日统计」这类要求，所以 v1 不做日桶（见 §3 的理由）。

### 1.1 「播放时长」的定义（已与用户确认）

「播放时长」= **这首歌累计被收听的时长**，不是「最近一次播放的时间点」，也不是歌曲本身的时长。三条边界：

1. **暂停不计时**。暂停、缓冲卡顿、加载中、切歌途中的空档，一律不累加。只有音频确实在出声推进时才计时。
2. **跨会话累加**。今天听 30 分钟、明天听 20 分钟，显示 50 分钟；退出重开继续累加。
3. **seek 不多算**。拖动进度条跳过的部分不算收听；反复拖拽也不会虚增。

因此统计页里**没有「最近播放」列**。`last_played_at` 只在数据层保留，用于默认排序的次级键与将来的筛选，不展示给用户（见 §5 的列定义）。

计时实现口径见 §4「计时：墙钟增量 + 位置推进门槛」，与 Android 端一致（`M9-android-playback-stats.md` 的「时长累加规则」：墙钟增量、暂停停表、seek 不计）。

## 2. 现有落点清单

| 能力 | 现有实现位置 | 复用还是新建 |
| --- | --- | --- |
| 数据目录 `%APPDATA%/neriplayer-win` | `data/store.py:158-165` `default_data_dir()`，支持 `NERIPLAYER_WIN_DATA_DIR` 覆盖 | 复用 |
| 独立变长数据文件范式（版本号 + 校验 + 原子写） | `data/store.py:863-888` `save_local_playlists()`；`data/store.py:553-612` `_validated_local_playlists()` | 复用范式，新建 play_stats 的读/写/校验三件套 |
| 设置白名单读写 | `data/store.py:763-800` `load_settings()`、`data/store.py:802-838` `save_settings()` | 不改；统计不进 settings（§3） |
| 设置页分组容器与插入点 | `ui/settings_page.py:185-313`，分组按 `ui/settings_page.py:278-288` 的 root 顺序装配 | 复用：新增一个分组，插在「语言」与「关于」之间 |
| 设置页信号 → 持久化模式 | `ui/main_window.py:4381-4404` `_wire_settings_page()` | 复用：新信号接一个只做切页的槽，不落盘 |
| 设置页打开方式 | `ui/main_window.py:3049-3051`（侧栏 `kind == "settings"` → `setCurrentIndex(_PAGE_SETTINGS)`） | 复用当前页面栈，统计页作为第 4 页 |
| 页面栈与转场 | `ui/main_window.py:152-153` `_PAGE_*`；`ui/main_window.py:1535-1538` `central_stack`；转场 `_SlideStack` `ui/main_window.py:628` | 复用：`addWidget` 一个统计页即可获得同款转场 |
| 歌曲表列定义 | `ui/main_window.py:184-192` `_header_netease()` / `_header_bili()` | 新建统计页专用表头（两列语义不同） |
| 歌曲表尺寸常量 | `ui/main_window.py:194-200`（序号列/封面列 50px，缩略 20px，行高 `ui/main_window.py:202`） | 同上，随委托一起抽取 |
| 歌曲表装配 | `ui/main_window.py:1410-1432` | 参考复制，不共用实例 |
| 封面列委托 | `ui/main_window.py:548` `_CoverColumnDelegate`（QStyledItemDelegate，逻辑上不依赖 MainWindow） | 需先抽取：该类和 `_TABLE_*` 常量都在 `ui/main_window.py` 内，而 main_window 要导入 stats_page，直接复用会成环。做法见 §5 末「模块归属」 |
| 封面加载与整表预取 | `ui/covers.py:125-192` `CoverLoader.request/preload`；`ui/main_window.py:3357-3359` `_preload_table_covers` | 复用：统计页独立一个 CoverLoader 实例（理由同侧栏实例 `ui/main_window.py:1391-1399`，预取批次互不作废） |
| 填表循环 | `ui/main_window.py:3328-3355` `_fill_song_table()` | 参考结构，统计页另写（数据源不同） |
| 空态视图 | `ui/main_window.py:400-431` `_EmptyStateView`（与 `_SongTable` 同模块） | 统计页自建轻量空态（`_EmptyStateView` 在 main_window 内，跨模块导入会成环） |
| 时长格式化 | `ui/player_bar.py:44-46` `format_seconds()` 只到 `mm:ss` | 新建 `format_play_duration()`（需要 `d天 hh:mm:ss`） |
| 播放进度与状态信号 | `player/engine.py:107-113` `progress(float,float)` / `track_ended()` / `playing_changed(bool)` / `load_failed(str)` | 复用，全部 UI 线程发出（`player/engine.py:13-16`） |
| 进度轮询 | `player/engine.py:120` `poll_interval_ms=250`；`player/engine.py:300-333` `_on_tick()` | 复用 250ms 作为计时 tick |
| 播放位置/总长 | `player/engine.py:284-296` `position()` / `duration()` | 复用（用于「播完」与「结尾 seek」判定） |
| 起播入口 | `ui/main_window.py:3971-4006` `_start_play()`，`_active` 在 `ui/main_window.py:3984` 赋值 | 复用：在此处开新统计周期 |
| 播完入口 | `ui/main_window.py:4241-4256` `_on_track_ended()`（含单曲循环直接 `engine.play_url` 重播，不经过 `_start_play`） | 复用：此处结算并重置周期 |
| 暂停/继续 | `ui/main_window.py:4453-4458` `_toggle_pause()` → `engine.toggle_pause()` → `playing_changed` | 复用 `playing_changed` |
| seek | `ui/main_window.py:4466-4468` `_on_seek_requested()`；`player/engine.py:205-209` | 复用：在此打「手动拖到结尾」标记 |
| 停止 | `player/engine.py:223-233` `stop()`；退出前 `ui/main_window.py:4599` | 复用：停止与退出前 flush |
| 队列条目身份键 | `player/queue.py:58-81` `QueueSong.key`（`(source, "id:bvid")`，酷狗 id 恒 0） | 直接复用为统计主键 |
| 列表窗口形态参考 | `ui/queue_window.py:17-121`（QListWidget + 头部计数，无表头无列） | 仅作交互参考；统计需要多列，QListWidget 不够用 |
| 中英文案表 | `i18n.py:26` `_STRINGS`；`i18n.py:430` `tr()` | 复用：新增 `settings.stats_*` / `stats.*` / `table.play_*` 键 |
| store 测试范式 | `tests/test_store.py:15-17` `make_store()`（`NERIPLAYER_WIN_DATA_DIR` + tmp_path）；`tests/test_local_playlists_store.py:56-380` 分「默认值/往返/校验/原子写」四组 | 复用：新建 `tests/test_play_stats_store.py` |
| 队列逻辑纯函数测试范式 | `tests/test_queue.py`（无 Qt app 依赖，注入随机种子） | 复用：tracker 用可注入 clock |

## 3. 数据模型建议

### 单独文件 play_stats.json，不进 settings.json

理由按现有代码逐条对：

1. 写入频率与体积完全不同。`save_settings()` 每次都是「默认值 + 白名单合并 + 整份覆盖」（`data/store.py:802-838`），调用点散布在关闭行为、播放模式、主题、语言、动态取色等用户动作上（`ui/main_window.py:4275`、`ui/main_window.py:4300-4330`）。统计每 15 秒要落一次盘，若与设置同文件，等于每 15 秒把 sidebar 折叠态、五个排序数组、最近列表全部重写一遍，且每次设置改动也会顺带重写统计大数组。
2. 数据性质不同。`settings.json` 是「界面偏好，损坏就回落默认」（`data/store.py:763-800` 全字段回落）；统计是「用户产出的不可再生数据，损坏只能清零」。两者放一起，一次坏写会同时毁掉偏好与历史。项目里已经为这类数据立了独立文件先例：`local_playlists.json`，`data/store.py:41-42` 的注释写明「用户产出的变长数据，生命周期独立于界面偏好，单独成文件」。
3. 校验策略不同。设置走「非法值静默回落默认」，统计需要「非法条目丢弃、合法条目保留」，与 `_validated_local_playlists()`（`data/store.py:553-612`）同构。

结论：新建 `<数据目录>/play_stats.json`，与 `local_playlists.json` 并列，读/写/校验三件套照 `data/store.py:842-888` 的形状实现。

### JSON schema（version 1）

```json
{
  "version": 1,
  "stats": [
    {
      "key": "netease:185811",
      "source": "netease",
      "id": 185811,
      "bvid": "",
      "title": "富士山下",
      "artist": "陈奕迅",
      "cover_url": "https://p1.music.126.net/xxx.jpg",
      "duration_ms": 258000,
      "listen_ms": 18734000,
      "play_count": 37,
      "first_played_at": 1759210000000,
      "last_played_at": 1798760000000
    },
    {
      "key": "kugou:0:2b1e4c9d8f7a6b5c3d2e1f0a9b8c7d6e|1234567890|556677",
      "source": "kugou",
      "id": 0,
      "bvid": "2b1e4c9d8f7a6b5c3d2e1f0a9b8c7d6e|1234567890|556677",
      "title": "晴天",
      "artist": "周杰伦",
      "cover_url": "https://imge.kugou.com/xxx.jpg",
      "duration_ms": 269000,
      "listen_ms": 5312000,
      "play_count": 9,
      "first_played_at": 1798010000000,
      "last_played_at": 1798750000000
    }
  ]
}
```

`key` 是冗余字段（可由 `source` + `id` + `bvid` 复算），落盘保存是为了加载时做去重与 O(1) 查表，避免每次加载都重新拼串。校验时以 `key` 为准，复算不一致的条目丢弃。

### key 构造规则

直接复用 `QueueSong.key`（`player/queue.py:70-81`）：

- 网易云：`("netease", str(id))`，`bvid` 恒空串（`ui/main_window.py:281-286`）
- B站：`("bili", f"{avid}:{bvid}")`（`ui/main_window.py:289-295`）
- 酷狗：`("kugou", f"0:{hash}|{encode_album_audio_id}|{album_id}")`，`id` 恒 0（`ui/main_window.py:298-311`）

落盘为 `f"{source}:{...}"` 单串，即上面的 `netease:185811`、`kugou:0:...`。

同一首歌跨音源是否合并：v1 分开。三条理由：

1. 合并需要元数据相似度判定（标题归一化 + 歌手归一化 + 时长容差），Android 端的 `identity_key` 就是这么算出来的（见 `M9-android-playback-stats.md` 的「是否区分音源」一节），移植这套规则要连带做文本归一化与冲突合并策略，工作量与风险都远大于统计功能本身。
2. 分开后「同一首歌」在列表里可能出现两行，但每行的数字都是真实听过的，不会出现「合并后无法解释为什么多了 3 次」的问题。
3. `QueueSong.key` 是现成的、已在表格高亮、本地歌单去重、队列匹配三处验证过的键（`ui/main_window.py:3390-3399`、`data/store.py:336-348`），复用零风险。若将来要合并，可在 v2 加 `merged_group` 字段并保留原行，属向后兼容的增量迁移。

### 是否按天存 bucket

v1 不做。理由：

- 需求原文只要「次数 + 累计时长」，没有按天查询入口；Android 端的 bucket 表服务于「历史页按时间段筛选」，Windows 端没有对应界面，做了也无人读。
- 加 bucket 会把文件体积至少翻 2 倍（活跃用户每天每首歌一行），而写盘仍是整份覆盖，flush 成本随之翻倍。
- 保留 `first_played_at` / `last_played_at` 两个时间戳已足够支撑将来做「最近一周」「今日新增」这类查询：日桶可以在需要时由 `last_played_at` 反推，代价是精确度只到「最后一次」，不足以重建历史分布。真要重建历史，v2 再加 `"daily": {"2026-10-30": {"listen_ms": 60000, "play_count": 1}}` 子节点，按需开启。

### 体积估算（1 万首歌）

单条压缩 JSON（`separators=(",", ":")`，无缩进）实测结构约 280-320 字节，其中 `cover_url` 占 60-100 字节。按 300 字节计：

| 曲目数 | 压缩后文件 | indent=2 文件 | 全量重写耗时（NVMe / 机械盘粗估） |
| --- | --- | --- | --- |
| 100 | ~30 KB | ~60 KB | < 2 ms / ~5 ms |
| 1 000 | ~300 KB | ~600 KB | ~3 ms / ~15 ms |
| 10 000 | ~3.0 MB | ~6.0 MB | ~10 ms / ~80 ms |

桌面播放器一年真实曲库很少超过 2000 首有播放记录的，1 万是上界。结论：用压缩 JSON（`json.dumps(..., separators=(",", ":"))`，`ensure_ascii=False`），1 万首 3MB、重写 10ms 量级，留在 UI 线程同步写可以接受；如果实测超过 50ms（机械盘 + 杀软扫描），改走 `ui/workers.py` 的 `run_async` 后台写，同时保留「退出前同步 flush」这条兜底。

注意 `local_playlists.json` 用的是 `indent=2`（`data/store.py:879`），那是给人看得懂、写入频率低的文件；统计文件反过来优先体积与写速，两者风格不同是有意的，需在模块 docstring 里写一句。

### 版本号与迁移

顶层 `"version": 1`，与 `LOCAL_PLAYLISTS_VERSION`（`data/store.py:112`）同形。加载规则照 `_validated_local_playlists()`（`data/store.py:560-566`）：

- 缺 `version` 或类型不是 int → 整份按空统计处理（不抛异常）
- `version <= 0` 视为 1（与 `tests/test_local_playlists_store.py:83-98` 的既有口径一致）
- `version` 大于当前已知 → 仍按 v1 结构尽力读取（`tests/test_local_playlists_store.py:247-265` 就是这个语义：未知高版本照样读，避免降级安装直接清零）
- `version` 小于当前 → 走迁移函数链 `_migrate_v1_to_v2()` 等，迁移后再校验

v1 不需要写迁移函数，但要在 `play_stats.py` 里留一个空的 `_MIGRATIONS: dict[int, Callable]`，让 v2 有挂点。

## 4. 计数与计时口径

### Windows 端可用的信号清单

| 信号 / 方法 | 声明位置 | 语义与用途 |
| --- | --- | --- |
| `progress(float, float)` | `player/engine.py:107` | 每 250ms 一次（`player/engine.py:120`），携带 `(position_s, duration_s)`。暂停期间定时器不停（`player/engine.py:300-333` 只在 `stop()` 里 `_timer.stop()`），所以暂停时也有 tick，位置不变 |
| `track_ended()` | `player/engine.py:108`，触发点 `player/engine.py:335-338` | eof 或「播过 + idle」，判定为整曲播完。此前已发 `playing_changed(False)` |
| `playing_changed(bool)` | `player/engine.py:109` | 三处发：`play_url` 后立刻 True（`player/engine.py:186`，此时音频尚未真正出声）、`set_paused`/`toggle_pause`（`player/engine.py:195`、`player/engine.py:203`）、`stop()` 与播完（`player/engine.py:233`、`player/engine.py:337`） |
| `load_failed(str)` | `player/engine.py:113` | 加载失败宽限 5s 后发出（`player/engine.py:115`、`player/engine.py:324-331`）；此路径不应计入时长 |
| `error(str)` | `player/engine.py:110` | seek/音量/停止失败，与统计无关 |
| `position()` / `duration()` | `player/engine.py:284-296` | 直接读 mpv `time_pos` / `duration`，未知返回 0.0 |
| `is_playing()` / `is_paused()` | `player/engine.py:265-282` | `is_playing` 在 idle 时也为 False，`is_paused` 只在显式暂停为 True |

Android 端的 `progressConfirmed` 在 Windows 没有对应物（`player/engine.py` 只注册 QTimer 快照，不注册 python-mpv 回调，见 `player/engine.py:13-16`）。等价的过滤办法见下。

### 计时：墙钟增量 + 位置推进门槛

时长对齐 Android 的「墙钟增量、暂停停表、seek 不计」（见 `M9-android-playback-stats.md` 的「时长累加规则」），落成四条规则：

1. tick 源是 `engine.progress`，250ms 一次。每次 tick 用 `time.monotonic()` 记录到达时刻，增量 = `now - last_tick`，与 `position` 的绝对值无关，所以 seek 快进不会多算。
2. 只有「位置确实在推进」才累加：要求 `position_s > 0` 且 `position_s != last_position`。这条同时解决两件事：`play_url` 后立刻发出的 `playing_changed(True)`（`player/engine.py:186`）不会被当成已开始播放；缓冲卡顿、mpv 停住不动时 position 不变，也不计时。等价于 Android 的 `progressConfirmed` 过滤，且不依赖额外状态机。
3. 暂停/停止/加载失败时把 `_counting` 置 False，下一次 tick 只更新 `last_tick` 不累加，暂停期间零计入。
4. **暂停必须由 `playing_changed(False)` 立即停表，不能只靠位置判据**。见下面的实测。

实测（本机 libmpv，6 秒测试音频，250ms 轮询，脚本在临时目录，未入库）：

```
PLAYING         1.0          <- play_url 后立即发出 True
tick          0.000
tick          0.001   +0.001
tick          0.242   +0.241
tick          0.471   +0.230
PLAYING         0.0          <- set_paused(True)
tick          0.695          <- 停表后首个 tick
tick          0.695   +0.000
tick          0.695   +0.000   （持续冻结，暂停期间 position 完全不变）
tick          0.695   +0.000
PLAYING         1.0          <- set_paused(False)
tick          0.852          <- 恢复后 position 正常推进
tick          1.090   +0.238
```

两个关键观察：

1. **暂停期间 position 确实完全冻结**，位置判据能正确识别「不动就不计时」，这条成立。
2. **但暂停生效存在一次残留推进**。第二次暂停的序列是 `tick 1.694` → `PLAYING 0.0` → `tick 1.732 (+0.038)` → `tick 1.732 (+0.000)`。`set_paused(True)` 发出后、mpv 真正停下前还漏出一个带 +38ms 增量的 tick。若只靠「位置在推进就计时」，这 38ms 会被算进去；一天暂停几十次就会多算一两秒。

修正后的停表顺序：收到 `playing_changed(False)` 立刻把 `_counting` 置 False 并在此刻结算；后续即使 position 仍有微小推进，也因 `_counting == False` 而不累加。位置判据退化为「只用于过滤未开播与缓冲」，不再承担暂停判定。

单次 ≤40ms 的误差量级与下面的精度结论一致，但暂停是高频操作，必须靠信号闭合而非位置判据，否则误差会累积。

精度：每次 tick 的增量是单调时钟差值，一次会话累加和与真实经过时间之差只来自「最后一个不完整 tick」（≤250ms）与「状态切换落在两次 tick 之间」（≤250ms），单次会话误差 ≤0.5s，且不会随播放时长累积。展示精确到秒，误差落在最后一秒内，可接受。

不用 `position` 差值累加：mpv 在 seek 后 position 跳变、在 `--audio-display=no` 下 position 更新粒度也可能粗于 250ms，差值法在快进时会一次计入大段跳跃。Android 明确避开这条路径。

### 一次播放 +1 的判定

建议对齐 Android：**听满 30 秒 +1，或整曲播完 +1，每个播放周期最多 +1**。

- 常量 `PLAYBACK_STATS_MIN_LISTEN_MS_FOR_PLAY_COUNT = 30_000`，命名与 Android 一致（`M9-android-playback-stats.md` 引用 `PlaybackStatsTracker.kt:8`）。
- 周期起点：`_start_play()`（`ui/main_window.py:3971`）里调用 `tracker.on_song_changed(song)`。此时若上一周期已听满 30 秒且未计过，结算 +1。
- 周期终点 1：`track_ended()`（`ui/main_window.py:4241`）无条件 +1 并重置周期。注意单曲循环的重播路径（`ui/main_window.py:4244-4251`）直接调 `engine.play_url`，不经过 `_start_play`，所以「重置周期」必须由 `track_ended` 负责，否则循环播放只会计 1 次。
- 周期终点 2：手动切歌、停止、退出时结算（只按 30 秒阈值，不看是否播完）。
- 周期内去重：`_counted_this_period` 布尔量，置位后本次周期不再 +1，与 Android 的 `hasCountedCurrentPlay` 同义。
- 短曲（`duration_ms < 30_000`）靠「播完 +1」覆盖，不需要特判。

**不改 50% 口径的理由**：50% 对长曲（如 10 分钟播客）要求 5 分钟才计一次，对 1 分钟的歌要求 30 秒，标准随曲长漂移，同一首歌换版本（完整版/剪辑版）会得出不同次数；30 秒是固定门槛，语义稳定，且与 Android 端数据可直接对照。若产品后续要「深度收听」口径，应作为第二个字段（如 `deep_play_count`）并列，而不是替换 `play_count`。

### 「播完」的检测

`track_ended()` 是主信号，但 mpv 的 eof→idle 检测有 250ms 级延迟（`player/engine.py:311-331`），且 `--audio-display=no` 下最后一帧可能提前。补一条前置判据：在 `progress` 里若 `duration_s > 0` 且 `position_s >= duration_s - 1.0`，置 `_reached_end = True`；flush 时若 `_reached_end` 为真则按「播完」计 +1，不必等 `track_ended`。`track_ended` 到达时再置一次（幂等）。

反向的坑：用户把进度条拖到结尾（`ui/main_window.py:4466-4468` → `player/engine.py:205-209`），mpv 会 eof 并触发 `track_ended`，Android 端专门抑制这种情况（`M9-android-playback-stats.md` 的「position 与 seek 的处理」）。Windows 端对应做法：`_on_seek_requested` 里若 `position_s >= engine.duration() - 1.0`，置 `_suppress_end = True`，`track_ended` 到达时跳过「播完 +1」，只走 30 秒阈值判定。这个标记在下次 `on_song_changed` 时清除。

### flush 频率与异常退出丢失上界

- 周期性：累计墙钟 15 秒 flush 一次（`PLAYBACK_STATS_PERIODIC_FLUSH_MS = 15_000`，与 Android 一致）。flush 的判据是「距上次 flush 的真实经过时间」，不是 tick 计数，避免 tick 抖动漂移。
- 事件性 flush：暂停、切歌、`track_ended`、`stop()`、窗口关闭（`ui/main_window.py:4620` `closeEvent`，注意托盘路径 `event.ignore()` 只是隐藏窗口，进程仍在，此时也要 flush）、`_exit_app`（`ui/main_window.py:4610`）、`engine.shutdown()`（`player/engine.py:235`）。
- 丢失上界：进程被强杀/断电时，最多丢「最近一次 flush 之后」的 ≤15 秒时长，以及「已听满 30 秒但尚未 flush 的那 1 次」计数。15 秒 × 每条记录几字节的增量，量级可忽略。
- 不做「每 tick 落盘」：250ms 一次整份重写 3MB 文件，机械盘上会持续占 IO。

### 写入是增量而非覆盖

flush 只把 `listen_ms += delta`、`play_count += inc`、`last_played_at = now` 三处改动落到内存表，再整份写文件。这样即使写失败（`OSError`），内存里的累计值仍然正确，下一次 flush 会带上这部分，不会像「写快照覆盖」那样把丢失的增量永久吞掉。

## 5. UI 方案与对比

### 三种承载形态

| 方案 | 优点 | 缺点 | 判断 |
| --- | --- | --- | --- |
| A. 独立页面（`central_stack` 第 4 页） | 与现有 `_SlideStack` 转场一致（`ui/main_window.py:628`）；宽度吃满，五列表格有足够空间；列宽/行高/封面委托可从现有歌曲表抽取后共用（见本节末「模块归属」）；设置页与统计页互不遮挡 | 需要新增 `_PAGE_STATS` 常量与返回入口 | 采用 |
| B. 复用队列窗口（`ui/queue_window.py` 形态，独立 Tool 窗口） | 改动小，`QueueWindow` 已有「非模态 + 自订阅数据」骨架（`ui/queue_window.py:17-44`） | 该窗口是 `QListWidget` 单列文本（`ui/queue_window.py:32`、`ui/queue_window.py:100-104`），要换成多列表格等于重写；440×540 的窗口放五列会挤；不占任务栏（`Qt.WindowType.Tool`，`ui/queue_window.py:25`），用户切走后找不回来 | 不采用 |
| C. 模态/非模态弹窗（`QDialog`） | 不改页面栈 | 模态挡住播放器；非模态又要处理置顶与生命周期；尺寸与页面栈转场不一致 | 不采用 |

采用 A。统计页与设置页并列，从设置页入口点进，页面顶部放一行返回设置页的按钮（与侧栏「设置」节点 `ui/main_window.py:2890-2894` 语义一致，回到 `_PAGE_SETTINGS`）。

### 表格列定义

| 列 | 宽度 | 对齐 | 内容 |
| --- | --- | --- | --- |
| 0 序号 | 50 固定（`_TABLE_NUMBER_WIDTH`） | 右对齐（`ui/main_window.py:3336-3341` 同款） | 行号，随排序重算 |
| 1 封面 | 50 固定（`_TABLE_COVER_WIDTH`） | 居中（`_CoverColumnDelegate`，`ui/main_window.py:548`） | 20px 缩略（`_TABLE_COVER_SIZE`） |
| 2 标题 | Stretch | 左对齐 | `title`；空标题回落 `stats.unknown_title` |
| 3 播放时长 | 130 固定 | 右对齐 | `format_play_duration(listen_ms)` |
| 4 播放次数 | 90 固定 | 右对齐 | 整数，千分位不加逗号（保持与现有表一致，现有表不做数字格式化） |

行高沿用 `_TABLE_ROW_HEIGHT = 32`（`ui/main_window.py:202`），斑马纹与选中色沿用全局 QSS（`ui/theme.py:219-234`），无需额外样式。

### 播放时长的格式化

新函数 `format_play_duration(listen_ms: int) -> str`，放在 `ui/stats_page.py`：

| 累计时长 | 输出 |
| --- | --- |
| 0 | `00:00:00` |
| 47 秒 | `00:00:47` |
| 12 分 3 秒 | `00:12:03` |
| 3 时 7 分 9 秒 | `03:07:09` |
| 2 天 5 时 1 分 0 秒 | `2天 05:01:00` |

规则：毫秒向下取整到秒；不足 24 小时不出现「天」段；天数用整数不加前导零，后接「天」加一个半角空格；时/分/秒各自两位补零。英文下「天」换成 `d `（`2d 05:01:00`），单位串由 `i18n.tr("stats.duration_days", days=n)` 返回。

不复用 `ui/player_bar.py:44-46` 的 `format_seconds`：它只出 `mm:ss`，超过 60 分钟会显示成 `75:30`，不满足验收条目 R2；也不该为了统计去改播放条的既有显示（播放条显示单曲时长，`mm:ss` 是对的）。

### 默认排序

`play_count` 降序，同次数按 `last_played_at` 降序，再按 `key` 升序保证稳定。理由：用户打开统计页的第一诉求是「我最常听什么」，Android 端索引里 `(play_count DESC, identity_key ASC)` 就是这个意图（见 `M9-android-playback-stats.md` 的索引说明）。

### 列头点击排序

不用 `QTableWidget.setSortingEnabled(True)`。原因：项目里没有任何表格开启排序（全库 grep 无 `setSortingEnabled` / `sortItems` / `sortByColumn`），现有表的数据源都是外部列表、表格只是投影（`ui/main_window.py:585-592` 的类注释写明这个原则）；且 `QTableWidgetItem` 的默认比较是字符串比较，「9」会排在「10」后面，要正确排序得给数字列写自定义 `__lt__` 子类。

做法：接 `horizontalHeader().sectionClicked`，把「当前排序键 + 方向」存成页面状态，排序数据列表后整表重填（复用 `_fill_stats_table`）。点同一列翻转方向，点不同列重置为降序（次数/时长）或升序（标题）。表头在排序列加 `▲/▼` 后缀，用 `setHorizontalHeaderLabels` 重设（现有表也是每次填表重设表头，`ui/main_window.py:3335`）。

### 大列表性能与分页阈值

`QTableWidget` 每行 5 个 `QTableWidgetItem`，10 000 行是 50 000 个对象。粗估构建成本 0.5-1.5 s（Qt 对象创建 + 布局），首次显示再加封面委托绘制。封面加载是异步的（`ui/covers.py:156-192`），不阻塞首帧，但会一次性把上万个 URL 入队，`_PRELOAD_WORKERS = 5`（`ui/covers.py:39`）要跑很久，且每个未命中都会发网络请求。

阈值判断（按有播放记录的曲目数 N）：

| N | 处理方式 |
| --- | --- |
| N ≤ 1 000 | 直接全量填表，不做任何优化。1000 行构建约 50-150 ms，肉眼无感 |
| 1 000 < N ≤ 3 000 | 全量填表但延迟填：先 `setRowCount` + 空行，用 `QTimer.singleShot(0, ...)` 分 500 行一批填，期间表头显示「载入中」。封面只预取前 200 行 |
| N > 3 000 | 分页：每页 500 行，底部「上一页 / 下一页」与页码。不做虚拟滚动（`QTableView` + `QAbstractTableModel` 才是虚拟滚动，等于放弃 `_SongTable` 的现成形态） |

v1 先按「N ≤ 3000 全量、>3000 分页」实现，分页控件与 `_search_more_btn` 同款样式（`ui/theme.py:336-345`，透明底 + primary 文字）。实际曲库多数落在 1000 以下，分页路径大概率长期不触发，但阈值必须先写进代码，避免用户积累两年后卡住。

封面策略：只对当前可见区域 + 前 200 行调 `preload`，用户滚动到底再追加。`CoverLoader.preload` 每次调用会作废上一批（`ui/covers.py:158-170`），所以不能滚动中反复调用同一个 loader 实例，改为「首次填表时一次把前 200 行交给 loader，滚动到第 150 行时再以当前窗口扩展一次」。统计页用独立 `CoverLoader` 实例，避免与歌曲表互相作废。

### 空态文案

复用 `_EmptyStateView` 的视觉（居中淡色图标 + 一行提示，`ui/main_window.py:400-431`），但在统计页模块内另写一个同名轻量类，不复用 main_window 里那个（会成环：`settings_page`/`stats_page` 被 `main_window` 导入）。图标取 `library_music`（`ui/icons.py:40` 已登记，资产 `assets/icons/library_music.svg` 存在）。文案：

- 中文：`还没有播放记录。播放一首歌满 30 秒后，这里会出现记录。`
- 英文：`No play records yet. Listen to a song for 30 seconds and it will show up here.`

这句话同时承担「为什么刚播的歌没出现」的解释，省掉额外的说明文案。

### 模块归属与循环导入

`_CoverColumnDelegate`（`ui/main_window.py:548`）与 `_TABLE_COL_NUMBER` / `_TABLE_COVER_WIDTH` / `_TABLE_COVER_SIZE` / `_TABLE_COVER_RADIUS` / `_TABLE_ROW_HEIGHT`（`ui/main_window.py:194-202`）都住在 `ui/main_window.py`，而 `ui/main_window.py:143` 已经 `from .settings_page import SettingsPage`，统计页同样要被 main_window 导入。stats_page 反向 import main_window 会成环。

处理方式二选一，建议前者：

1. 把这些表格共享件下沉到新模块 `ui/song_table.py`：`_CoverColumnDelegate`、`_TABLE_COL_NUMBER`、`_TABLE_COL_COVER`、`_TABLE_NUMBER_WIDTH`、`_TABLE_COVER_WIDTH`、`_TABLE_COVER_SIZE`、`_TABLE_COVER_RADIUS`、`_TABLE_ROW_HEIGHT`，以及一个 `install_song_table_chrome(table)` 函数（做 `ui/main_window.py:1413-1432` 那套列宽/委托/行高/斑马纹设置）。main_window 与 stats_page 都从这里导入。改动是纯搬移，行为不变，`tests/test_main_window_layout.py` 可继续守住。
2. stats_page 自己复制一份常量与一个 40 行的封面委托。省一次重构，但两处列宽将来会漂移，不建议。

`_SongTable`（`ui/main_window.py:585`）不参与下沉：它带拖拽调序与 `reorder_requested` 信号，统计页只读，用裸 `QTableWidget` 即可。

## 6. 设置页入口

### 现有分组（按装配顺序）

| # | 分组对象 | 标题键 | 控件形态 | 代码位置 |
| --- | --- | --- | --- | --- |
| 1 | `close_group` | `settings.close_group` 关闭行为 | 两个 `QRadioButton`（退出 / 托盘）+ stretch | `ui/settings_page.py:199-205` |
| 2 | `mode_group` | `settings.play_group` 播放 | 第一行 `QLabel` + `QComboBox`（播放模式）；第二行 `QLabel` + `QSpinBox`（最近列表数量） | `ui/settings_page.py:207-227` |
| 3 | `quality_group` | `settings.quality_group` 音质 | `QLabel` + `QComboBox`（三档） | `ui/settings_page.py:229-237` |
| 4 | `appearance_group` | `settings.appearance_group` 外观 | `QLabel` + `QComboBox`（暗/亮）+ `QCheckBox`（动态取色） | `ui/settings_page.py:239-252` |
| 5 | `language_group` | `settings.language_group` 语言 | `QLabel` + 自绘 `LanguageSwitch` | `ui/settings_page.py:254-260` |
| 6 | `about_group` | `settings.about_group` 关于 | 三个 `QLabel`（版本 / 许可证 / 上游链接） | `ui/settings_page.py:262-272` |

装配顺序由 `ui/settings_page.py:278-288` 的 `root.addWidget` 决定；页面外层包 `QScrollArea`（`ui/settings_page.py:290-297`），新增分组不会顶高窗口。

### 插入位置

新增 `stats_group`，插在 `language_group` 与 `about_group` 之间，即 `ui/settings_page.py:286` 与 `ui/settings_page.py:287` 之间加一行 `root.addWidget(self.stats_group)`。

位置理由：语言、外观、音质、播放都属于「我听的偏好」，统计属于「我听了什么」，是内容型而非配置型，放在配置组之后、关于之前，符合从「怎么用」到「我用了什么」到「这是什么」的阅读顺序。

### 控件形态

```
┌─ 播放统计 ──────────────────────────────────────────┐
│  记录每首歌的累计收听时长与播放次数                    │
│                                        [ 查看统计 ]  │
│  清空所有播放统计记录                    [ 清空 ]    │
└─────────────────────────────────────────────────────┘
```

- `QGroupBox`，标题键 `settings.stats_group`（中「播放统计」，英「Playback Stats」）
- 第一行：`QLabel` 说明文字（`settings.stats_hint`）+ `QPushButton`「查看统计」（`settings.stats_open`），按钮 `setObjectName("statsOpenBtn")`，样式照 `QPushButton#searchMoreBtn`（`ui/theme.py:336-345`，透明底 + primary 字）再加 `border: 1px solid @outlineVariant` 让它看起来像可点的入口
- 第二行：`QLabel` 说明（`settings.stats_clear_hint`）+ `QPushButton`「清空」（`settings.stats_clear`），普通 `QPushButton`（全局透明底样式，`ui/theme.py:259-266`），`setObjectName("statsClearBtn")` 配次要色
- 两行都用 `QHBoxLayout`，中间 `addStretch(1)`，与 `mode_group` 的行内布局同款（`ui/settings_page.py:223-227`）

不做整组可点击：`QGroupBox` 的点击事件会与内部控件冲突，且需要自绘 hover 态，收益不值。

### 信号与接线

`SettingsPage` 新增两个信号：

- `stats_open_requested = Signal()`：`ui/settings_page.py:188-194` 的信号块里加一行
- `stats_clear_requested = Signal()`：同上

`MainWindow._wire_settings_page()`（`ui/main_window.py:4381-4404`）加两行连接：

- `stats_open_requested` → `self.central_stack.setCurrentIndex(_PAGE_STATS)`，同时调 `self.stats_page.reload()`（每次进入重新读数据，统计在后台一直在变）
- `stats_clear_requested` → 弹 `QMessageBox.question` 二次确认（与删除本地歌单同款，`ui/main_window.py:2333-2341`），确认后清空内存表 + 写空文件 + 重填统计页

`retranslate()`（`ui/settings_page.py:317-348`）末尾补三行：分组标题、两个说明标签、两个按钮文字。`MainWindow._on_language_changed()`（`ui/main_window.py:4345-4367`）里已调 `settings_page.retranslate()`，再加 `stats_page.retranslate()`。

`retheme_link()`（`ui/settings_page.py:351-358`）不需要改：入口按钮用的是普通按钮样式，不吃内联颜色。

## 7. 实现步骤拆解

按依赖顺序，每步一句话，步与步之间可独立提交与测试。

1. 新建 `src/neriplayer_win/data/play_stats.py`：定义 `PlayStat` 数据类（`key / source / id / bvid / title / artist / cover_url / duration_ms / listen_ms / play_count / first_played_at / last_played_at`）与 `to_dict()`，字段顺序固定以保证落盘 diff 稳定。
2. 在同文件写 `_validated_play_stats(payload)`：照 `data/store.py:553-612` 的形状校验 version 与 stats 数组，逐条丢弃非法项（`listen_ms < 0`、`play_count < 0`、`key` 复算不一致、`source` 不在 `LOCAL_SONG_SOURCES` 的都丢），`key` 重复只留首个。
3. 在 `data/store.py` 的 `LocalStore` 加 `_PLAY_STATS_FILE = "play_stats.json"` 常量、`play_stats_path` 属性、`load_play_stats() -> list[PlayStat]` 与 `save_play_stats()`（原子写，照 `data/store.py:863-888`，但用 `separators=(",", ":")` 压缩），并在类 docstring 与文件头注释补一句「统计文件优先体积与写速，与 local_playlists.json 的 indent=2 风格不同是有意的」。
4. 新建 `tests/test_play_stats_store.py`：照 `tests/test_local_playlists_store.py` 的四组结构写「无文件/损坏/版本非法回落空、往返保真、非法条目丢弃、原子写失败保留旧文件」。
5. 新建 `src/neriplayer_win/player/stats_tracker.py`：纯逻辑类 `PlayStatsTracker`，构造注入 `clock: Callable[[], float] = time.monotonic`，实现 `on_song_changed(song)` / `on_tick(position_s, duration_s)` / `on_ended()` / `on_paused(bool)` / `on_seek(target_s, duration_s)` / `flush(now) -> PlayStatsDelta | None`，常量 `MIN_LISTEN_MS_FOR_PLAY_COUNT = 30_000`、`PERIODIC_FLUSH_MS = 15_000`。
6. 新建 `tests/test_stats_tracker.py`：注入假 clock 覆盖「不足 30 秒不计次」「满 30 秒计 1 次」「播完必计 1 次」「同周期不重复计」「暂停不计时」「seek 快进不加时」「拖到结尾不计次」「单曲循环每圈 +1」「flush 只输出增量」九条。
7. 新建 `src/neriplayer_win/ui/song_table.py`：把 `_CoverColumnDelegate` 与 `_TABLE_*` 常量从 `ui/main_window.py:194-202`、`ui/main_window.py:548` 搬过来，加 `install_song_table_chrome(table)` 封装列宽/委托/行高设置；main_window 改为从这里导入（纯搬移，`tests/test_main_window_layout.py` 应仍全绿）。
8. 新建 `src/neriplayer_win/ui/stats_page.py`：`StatsPage(QWidget)`，含五列 `QTableWidget`、轻量空态视图、`format_play_duration(ms)`、`reload(records)` 填表（含 3000 行分页阈值）、`retranslate()`、`sort_by(key)`、返回设置页按钮。
9. 在 `ui/main_window.py` 加 `_PAGE_STATS = 3`（`ui/main_window.py:152-153` 旁），构造 `self.stats_page = StatsPage()` 并 `central_stack.addWidget(self.stats_page)`（`ui/main_window.py:1535-1538` 旁），再给统计页挂一个独立 `CoverLoader` 与 `_on_stats_cover_ready`。
10. 在 `ui/main_window.py` 加 `self._stats_tracker = PlayStatsTracker()`、`self._play_stats: dict[str, PlayStat]`（启动时 `self._store.load_play_stats()` 装入），并在 `ui/main_window.py:1318-1319` 旁初始化。
11. 接引擎信号：在 `_wire_engine()`（`ui/main_window.py:4423-4433`）里加 `progress` 第二条连接（现有那条连 `player_bar.set_progress`，统计走独立槽，不复用 UI 槽），槽内做「位置推进门槛 → 累加 → 15 秒 flush 判定」。
12. 接生命周期：`_start_play()`（`ui/main_window.py:3971`）末尾调 `tracker.on_song_changed(song)` 并先 `flush`；`_on_track_ended()`（`ui/main_window.py:4241`）首行调 `tracker.on_ended()`；`_on_playing_changed()`（`ui/main_window.py:4434`）里按 `playing` 调 `tracker.on_paused(not playing)`；`_on_seek_requested()`（`ui/main_window.py:4466`）里调 `tracker.on_seek()`。
13. 接 flush 落盘：写一个 `_flush_play_stats()`，把 tracker 的增量合并进 `self._play_stats` 再 `self._store.save_play_stats(...)`；在 `closeEvent`（`ui/main_window.py:4620`）与 `_exit_app`（`ui/main_window.py:4610`）真退出分支调它，托盘隐藏分支（`event.ignore()` 那条）也调一次。
14. 设置页加入口：`ui/settings_page.py` 加 `stats_group`（`ui/settings_page.py:286-287` 之间插入 `root.addWidget`）、两个信号 `stats_open_requested` / `stats_clear_requested`、`retranslate()` 补文案。
15. `ui/main_window.py` 的 `_wire_settings_page()`（`ui/main_window.py:4381-4404`）接两个信号：打开 → 切 `_PAGE_STATS` + `stats_page.reload()`；清空 → `QMessageBox.question` 确认（照 `ui/main_window.py:2333-2341`）→ 清内存表 + 存空文件 + `stats_page.reload([])`。
16. `i18n.py` 的 `_STRINGS`（`i18n.py:26`）补键：`settings.stats_group` / `settings.stats_hint` / `settings.stats_open` / `settings.stats_clear_hint` / `settings.stats_clear` / `settings.stats_clear_confirm` / `stats.title` / `stats.back` / `stats.empty` / `stats.unknown_title` / `table.play_duration` / `table.play_count` / `stats.loading` / `stats.page_prev` / `stats.page_next`。
17. `MainWindow._on_language_changed()`（`ui/main_window.py:4345-4367`）加 `self.stats_page.retranslate()`。
18. 新建 `tests/test_stats_page.py`：照 `tests/test_local_playlists_ui.py:30-38` 的 `_make_window` / `_close` 写法，验证「设置页入口存在且可点」「点击后 `central_stack.currentIndex() == _PAGE_STATS`」「五列列头文字」「排序点击改变首行」「清空走确认框」。
19. 手工验收：真机播一首歌满 30 秒 → 打开统计页看次数与时长；退出重进看数字是否保留；播完一首短曲看是否 +1。

## 8. 风险与验收

### 多音源同 id

风险：网易云与 B站 id 空间独立，理论上可以撞号；酷狗 `id` 恒 0，所有酷狗歌都靠 `bvid` 区分（`ui/main_window.py:298-311`）。

处理：主键用完整 `QueueSong.key`（`player/queue.py:70-81`），带 `source` 前缀，跨音源天然隔离；酷狗靠 `bvid` 三段编码隔离。验收：构造 `netease:12345` 与 `bili:12345:bvidxxx` 两条记录，列表显示两行互不覆盖。

### 改名后归属

风险：用户改了本地歌单里的标题，或音源侧改了歌名，统计行的 `title` 与当前列表不一致。

处理：统计是「按身份键聚合」，改名不改身份键。每次 `on_song_changed` 时用最新 `QueueSong` 的 `title / artist / cover_url / duration_ms` 覆盖记录里的快照字段（Android 端也是冗余存展示元数据、写入时刷新）。验收：同一首歌在本地歌单里改名后再播一次，统计页标题变成新名，次数与时长继续累加而非新起一行。

已知限制：若用户把一首歌从歌单删掉且再没播过，统计行保留旧标题，这是正确的（统计的是历史）。

### 删歌单不影响统计

风险：统计若挂在歌单条目上，删歌单会连带删统计。

处理：统计表与 `local_playlists.json` 完全解耦，只存 `QueueSong` 快照与身份键，不存歌单 id 或条目下标。验收：删掉正在播放来源的本地歌单后，统计页该行仍在，数字不变。

### 数据重置入口

风险：用户想清空但找不到入口；或误点清空丢历史。

处理：设置页「清空」按钮 + `QMessageBox.question` 二次确认（默认按钮为 No，照 `ui/main_window.py:2337-2338`）。清空时先 `tracker.flush()` 丢弃当前周期增量（否则清空后立刻又写回一条），再写空文件。验收：点清空 → 确认 → 列表空态 → 重启后仍空；点取消 → 数据不变。

### 性能

| 项 | 验收线 | 依据 |
| --- | --- | --- |
| 统计页首次打开（N=1000） | ≤ 150 ms | 1000 行 × 5 item 的构建量级 |
| 统计页首次打开（N=3000） | ≤ 500 ms | 分批填表，超过则降级为分页 |
| flush 单次写盘（N=10000） | ≤ 50 ms | 3MB 压缩 JSON + `os.replace`，机械盘留余量；超线改 `run_async` 后台写 |
| 播放中每 15 秒一次的 flush 对播放无感 | 无掉帧、无音频卡顿 | 写盘在 UI 线程，`os.replace` 是原子改名不是重写；若机械盘实测超线则改后台 |
| 冷启动增量 | ≤ 30 ms | 启动时读一次 3MB JSON 并校验；实测超过则改惰性加载（首次进统计页才读） |
| 常驻内存增量 | ≤ 10 MB | 1 万条 `PlayStat` 数据类约 1-2 MB，加表格 item 约 5 MB；与 `docs/PERF.md` 的 200MB 线并算 |

封面：统计页只预取前 200 行（`_PRELOAD_WORKERS = 5`，`ui/covers.py:39`），滚动追加。验收：N=3000 时打开统计页不应产生 3000 个网络请求（用日志或抓包确认请求数 ≤ 200）。

### 其他风险

| 风险 | 处理 |
| --- | --- |
| 用户把进度条拖到结尾被误计一次 | `on_seek` 置抑制标记（§4「播完的检测」），单测覆盖 |
| 单曲循环只计 1 次 | 重置周期由 `_on_track_ended` 负责而非 `_start_play`（`ui/main_window.py:4244-4251` 的重播路径绕过 `_start_play`），单测覆盖 |
| 播放失败（`load_failed`）计入时长 | 位置不推进即不累加（§4 规则 2），失败时 position 恒 0，天然不触发 |
| 统计文件损坏 | 加载回落空列表并在日志留 warning（照 `data/store.py:856` 的写法），不覆盖原文件，便于用户手工抢救 |
| 降级安装读高版本文件 | 按 v1 结构尽力读，未知字段忽略（`tests/test_local_playlists_store.py:247-265` 的既有语义） |
| 统计页与歌曲表封面预取互相作废 | 独立 `CoverLoader` 实例，理由与侧栏实例同（`ui/main_window.py:1391-1399`） |
| 语言切换后统计页表头仍是旧语言 | `_on_language_changed` 调 `stats_page.retranslate()`，同时重填表（表头随填表重设，`ui/main_window.py:3335` 的既有做法） |

### 不做的事（v1 范围外）

- 按天/按周/按月的统计视图
- 跨音源合并同一首歌
- 播放统计导入导出
- 统计与账号同步
- 歌单播放次数（Android 端的 `LocalPlaylistPlaybackStatsRepository` 是独立功能，需求原文只要求歌曲维度）

## 调研进度

- [x] 读 store.py（`data/store.py` 994 行，default_settings / 校验函数 / 原子写 / 数据目录）
- [x] 读 settings_page.py（六个分组的控件形态与装配顺序）
- [x] 读 main_window.py 表格/设置页/进度信号（`_SongTable`、`_fill_song_table`、`_PAGE_*`、`_wire_engine`、`_start_play`、`_on_track_ended`、`closeEvent`）
- [x] 读 engine.py / queue.py / queue_window.py（信号清单、250ms 轮询、`QueueSong.key`、QListWidget 形态）
- [x] 读 i18n.py / player_bar.py format_seconds（`_STRINGS` 键表、`tr()`、`mm:ss` 不足以承载 `d天 hh:mm:ss`）
- [x] 看 tests 现有 store 测试（`test_store.py` 的 `make_store`、`test_local_playlists_store.py` 的四组结构、`test_local_playlists_ui.py` 的窗口构造 helper）
- [x] 引用 Android 口径（`M9-android-playback-stats.md`：30 秒阈值 / 墙钟增量 / 15 秒 flush / 周期去重 / seek 抑制）
- [x] 读 covers.py（`CoverLoader.preload` 的批次作废语义与 5 线程池）

补充说明：`ui/queue_window.py` 的 `QListWidget` 形态不适合多列表格，已在 §5 说明为何不采用；`ui/main_window.py` 的 `_EmptyStateView` 在 main_window 模块内，统计页另写轻量版本以避免循环导入。
