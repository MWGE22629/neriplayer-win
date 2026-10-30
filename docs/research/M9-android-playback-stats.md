# Android 端播放统计调研（M9）

目标：把 NeriPlayer Android 的「播放统计」功能读透，给 Windows 端移植提供可执行的说明。

用户需求原文：「neri win提供播放统计，播放歌曲的次数，时长日d时:分:秒，入口放在设置页面，点击后显示歌单列表的形式，但把作者和歌曲时长栏换成播放时长、播放次数栏」

调研范围：`reference/NeriPlayer-Android/` 只读。

## 数据模型

Android 端不是"事件明细表"，而是"按歌聚合表 + 按天聚合表 + 分片计数器表"三件套。所有表定义在
`modules/database/src/main/java/moe/ouom/neriplayer/data/local/database/entity/stats/PlaybackStatEntities.kt`。

### playback_stat（全量聚合，一歌一行）

主键 `identity_key`（String，见下），其余为列。file:PlaybackStatEntities.kt:10-59

| 列 | 类型 | 说明 |
| --- | --- | --- |
| identity_key | TEXT, PK | 歌曲身份键，跨音源去重的基础 |
| id | INTEGER | 原曲目 id（可能撞号，所以不能做主键） |
| name / artist / album | TEXT | 展示元数据 |
| album_id | INTEGER | |
| cover_url | TEXT NULL | |
| duration_ms | INTEGER | 歌曲本身的时长（用于展示，不用于累加） |
| total_listen_ms | INTEGER | 累计实际收听毫秒 |
| play_count | INTEGER | 累计播放次数 |
| last_played_at | INTEGER | 最近一次播放时间戳 |
| first_played_at | INTEGER | 首次播放时间戳 |
| media_uri | TEXT NULL | 是否本地/流媒体来源之一 |
| local_file_path / local_file_name | TEXT NULL | 本地文件信息 |
| custom_name / custom_artist / custom_cover_url | TEXT NULL | 用户自定义覆盖字段 |

索引：`last_played_at DESC`、`media_uri`、`(play_count DESC, identity_key ASC)`、
`(total_listen_ms DESC, identity_key ASC)`、`(last_played_at DESC, identity_key ASC)`、
`(first_played_at, identity_key)`。file:PlaybackStatEntities.kt:12-23
索引方向就是默认排序意图：热度榜按播放次数降序、时长榜按时长降序、最近榜按时间降序。

### playback_stat_bucket（按天聚合，一歌一天一行）

复合主键 `(day_start_at, identity_key)`，外键 `identity_key` 指向 playback_stat，`ON DELETE CASCADE`。
字段与 playback_stat 完全同构（含 id/name/artist/cover 等冗余快照列），多一个 `day_start_at`。
file:PlaybackStatEntities.kt:61-118

`day_start_at` 是当天 00:00:00.000 的本地时区毫秒值（算法见"时间维度"）。
冗余存元数据是为了按时间区间查询时不必 JOIN 主表，也让"当年那天的封面"这类快照语义成立。

### playback_stat_counter_shard（全量计数器分片）

复合主键 `(identity_key, device_id, epoch_started_at)`，外键级联删除。
字段：`total_listen_ms`、`play_count`、`first_played_at`、`last_played_at`。file:PlaybackStatEntities.kt:120-150
这是多设备同步用的增量计数器：每台设备每个"epoch"（同步会话代次）写一片，合并时相加，
避免整表覆盖导致并发丢增量。

### playback_stat_daily_counter_shard（按天计数器分片）

复合主键 `(day_start_at, identity_key, device_id, epoch_started_at)`，外键指向 bucket 并级联。
file:PlaybackStatEntities.kt:152-187

### 是否区分音源

表里没有 `source` / `platform` 列。区分靠 `identity_key` 与 `media_uri` 两个字段：
`identity_key` 由曲目元数据派生（见计数口径一节的 identityKey 计算），`media_uri` 记录
可回放的媒体地址。因此"同一首歌在不同音源"是否合并，取决于 identity_key 的构造规则，
而不是靠独立列。

### 领域模型

`TrackStat`（file:modules/model/.../stats/TrackStat.kt:3-22）与表结构一一对应，
`PlaybackStatBucket`（file:PlaybackStatsModels.kt:16-36）对应 bucket 行。
`toEntity()` 扩展函数做映射（file:PlaybackStatEntities.kt:189-234）。

## 计数与计时口径

### 采样器：PlaybackStatsTracker

核心状态在 `modules/playback/logic/.../runtime/stats/PlaybackStatsTracker.kt`。
它不读播放器 position 做时长，而是用**墙钟增量**：`SystemClock.elapsedRealtime()` 记录的
"活跃区间起点"。

关键常量：
- `PLAYBACK_STATS_PERIODIC_FLUSH_MS = 15_000`，每 15 秒落一次盘。file:PlaybackStatsTracker.kt:7
- `PLAYBACK_STATS_MIN_LISTEN_MS_FOR_PLAY_COUNT = 30_000`，一次播放至少听满 30 秒才计 1 次。
  file:PlaybackStatsTracker.kt:8

状态字段：`trackingSong`、`segmentStartElapsedMs`、`accumulatedMs`（本批待落盘毫秒）、
`currentPlayListenedMs`（本次播放周期累计，用来判 30 秒阈值）、`isPlaying`、
`hasCountedCurrentPlay`（本次周期是否已计过 1 次）、`lastPlaybackPositionMs`（只用于判断是否播完）。
file:PlaybackStatsTracker.kt:28-38

### 时长累加规则

`collectActiveSegmentLocked()`（file:PlaybackStatsTracker.kt:168-182）：
只有在 `isPlaying == true` 且存在活跃区间时才累加，累加量是 `now - segmentStartElapsedMs`
的真实经过时间，然后立刻把起点推到 `now`。所以：

- 暂停时 `onPlayingChanged(false)` 先结算区间再停表（file:PlaybackStatsTracker.kt:80-83），
  暂停期间不计时。
- 播放中周期性调用 `collectActiveSegmentLocked()` 会不断结算，因此 seek 不会产生跳跃计入：
  计时跟墙钟走，跟 position 无关，快进不会多算时长。
- 引擎缓冲/启动卡顿通过 `onEnginePlayingChanged(isPlaying, progressConfirmed)` 过滤：
  恢复播放但还没确认进度推进时不计时，避免把启动卡顿算进去。
  file:PlaybackStatsOwner.kt:48-59
- 写入的是**实际收听毫秒**，不是歌曲 duration。`duration_ms` 只用于展示和"整曲次数"兜底推算。

一次 flush 的产物只有 `listenedMs > 0` 或 `playCountIncrement > 0` 时才生成快照，
否则返回 null（file:PlaybackStatsTracker.kt:199）。flush 后 `accumulatedMs` 归零，
但 `currentPlayListenedMs` 不清零，因为本次播放周期还没结束。

### 播放次数 +1 的条件

三个位置都可能给出 1：

1. `flushLocked(countPlay=...)` 内：`countPlay && !hasCountedCurrentPlay && currentPlayListenedMs > 0`
   （file:PlaybackStatsTracker.kt:198），其中 `countPlay` 来自 `shouldCountCurrentPlay()`
   = `!hasCountedCurrentPlay && currentPlayListenedMs >= 30_000`（file:PlaybackStatsTracker.kt:232-235）。
2. `onTrackEnded()`：无条件 `countPlay = true`（file:PlaybackStatsTracker.kt:109），
   所以整曲播完必定 +1，即使只听了 5 秒就跳到最后。随后重置
   `hasCountedCurrentPlay`、`currentPlayListenedMs`、`lastPlaybackPositionMs`。
3. 切歌 `onSongChanged()`：用 `shouldCountCurrentPlay()` 判定（file:PlaybackStatsTracker.kt:53）。

即：**听满 30 秒 +1，或整曲播完 +1，每个"播放周期"最多 +1**。`hasCountedCurrentPlay`
保证同一周期不会重复 +1；切歌、播完、清除代次变化都会重置周期。

"短时间重复"：没有独立的去抖窗口。反复切歌再切回来，每一轮都是新周期，只要每轮听满
30 秒就各 +1；循环播放单曲时，`onTrackEnded` 会触发 `hasCountedCurrentPlay = false`，
所以每循环一圈 +1。

### position 与 seek 的处理

`onPlaybackProgress(positionMs)`（file:PlaybackStatsTracker.kt:91-105）只做一件事：检测
position 是否"回绕"（上一位置明显大于当前位置且接近曲尾），判为播完就调 `onTrackEnded()`。
它**不参与时长计算**。

`onManualSeek(positionMs)`（file:PlaybackStatsTracker.kt:119-122）：记下新位置并置
`suppressNextPositionWrap = true`，这样下一次进度回调不会被误判成播完。
注意用户手动拖到末尾不会加次数；只有自然播放到末尾被 position 回绕检测到才会。

`onUntrackedPlaybackProgress()`：统计被禁用期间只更新 position 和判断回绕，
回绕时清空计数状态，避免恢复统计后把旧周期算进来。file:PlaybackStatsTracker.kt:149-159

### 写入链路与幂等

```
PlayerManager → PlaybackStatsOwner → PlaybackStatsPendingWrites 队列
  → AppPlaybackStatsWritePort.record(PlaybackStatsSnapshot)
  → PlaybackStatsRepository.recordListenDeltaNow(...)
  → Room 表 playback_stats_pending_delta（持久日志，幂等键 eventId）
  → drainPendingLocked() → PlaybackStatsDeltaPolicy.apply(...)
  → playback_stat / bucket / counter shard 四张表
```

- 每个快照带 `eventId`（UUID），入队时以 eventId 做幂等，重复入队不重复计数。
  file:PlaybackStatsRepository.kt:165-194
- `recordListenDeltaNow` 先落持久日志再应用，应用失败由后续 drain 重试；
  调用方不会因为失败而重复计数（file:PlaybackStatsRepository.kt:186-194）。
- 负数或零增量在入口被丢弃（file:PlaybackStatsRepository.kt:175）。
- 存量数据迁移时若日志里没有显式 `playCountIncrement`，用 `countSession()` 兜底：
  首次出现时 `listenedMs >= 30_000` 记 1；否则比较"累计时长 / 曲长"的整数倍数是否增加，
  增加就记 1。file:PlaybackStatsDeltaPolicy.kt:74-80

### 本地歌单播放统计

`LocalPlaylistPlaybackStatsRepository.recordPlayNow(playlistId, playedAt, eventId)` 是另一条线，
统计"歌单被播放"的次数，与单曲统计独立。file:LocalPlaylistPlaybackStatsRepository.kt:193

### 统计开关与降级

`writesEnabled` 为 false 时采样器仍在更新 position，但不计时长、不计次数
（file:PlaybackStatsOwner.kt:102-117）。存储不可用时 `pendingWrites` 队列会挂起采样，
并在恢复时从当前时刻重新计时，中间不可用的时段不计入
（file:PlaybackStatsOwner.kt:119-156）。

## 时间维度

### 五个档位：日/周/月/年/总

`PlaybackStatsPeriod` 只有 `DAY / WEEK / MONTH / YEAR / ALL` 五个值
（file:modules/model/.../stats/PlaybackStatsModels.kt:3-9）。UI 选择器按这个顺序渲染
（file:PlaybackStatsComponents.kt:68-74），标签分别是「日/周/月/年/总」
（file:strings_stats.xml:17-21）。

### 窗口算法：滚动天数窗口，不是自然周/自然月

`resolvePlaybackStatsTimeRange(nowMillis)`（file:PlaybackStatsTimeRangeExtensions.kt:5-34）：

1. 用 `Calendar.getInstance()`（默认时区，即设备本地时区）把 `now` 归到**当天 00:00:00.000**，
   再 `add(DAY_OF_YEAR, 1)` 得到 `endExclusive`。也就是窗口上界永远是「明天零点」，
   包含今天一整天。
2. 从 `end` 往前减固定天数得到 `startInclusive`：DAY 减 1、WEEK 减 7、MONTH 减 30、YEAR 减 365。
3. `ALL` 返回 `startInclusive = null`、`endExclusive = Long.MAX_VALUE`。

由此推出几个事实（测试 file:PlaybackStatsPeriodTest.kt:23-60 已固化）：

- **周不是周一起点，也不是周日起点**，纯滚动 7 天，窗口「昨天往前推 6 天 + 今天」。
  2026-07-02（周四）的 WEEK 窗口是 06-26 到 07-03（file:PlaybackStatsPeriodTest.kt:35-36）。
- **月固定 30 天、年固定 365 天**，不按自然月长度和闰年调整。跨月也不会对齐到 1 号
  （file:PlaybackStatsPeriodTest.kt:46-60）。
- 「日」窗口 = 今天 00:00 到明天 00:00，即自然日。
- 全部端点都用 `Calendar` 的本地时区计算，因此夏令时切换日窗口长度是 23 或 25 小时，
  不是固定 86'400'000 毫秒。

有一处不一致值得注意：`resolvePlaybackStatsTimeRange` 内部用 `Calendar.getInstance()`
取**进程默认时区**（file:PlaybackStatsTimeRangeExtensions.kt:15），而 UI 层的
`statsQueryDay(nowMillis, timeZone)` 接收注入的时区参数（file:PlaybackStatsQueryState.kt:83-94）。
生产环境两者都等于 `TimeZone.getDefault()`，所以行为一致；只有测试里注入非默认时区时，
窗口边界仍按默认时区算。移植时不必复制这个不彻底的注入，直接统一走本地时区即可。

### 写入侧与查询侧的时区一致性

`day_start_at` 由 `playbackStatsDayStartAt(millis)` 生成（file:PlaybackStatsTimeRangeExtensions.kt:36-41），
同样用默认时区归零到当天 00:00。写入在 `applyDelta` 里按 `delta.playedAt` 计算
（file:PlaybackStatsRoomStore.kt:526），所以 bucket 的主键永远是**本地时区日界**。

查询侧 `bucketSource()` 用 `query.period.resolvePlaybackStatsTimeRange(query.nowMillis)`
把窗口起止绑定到 SQL，直接和 `day_start_at` 比较（file:PlaybackStatsSqlQuery.kt:83-99）。
两侧用的是同一个「把毫秒归到本地零点」的定义，所以比较是自洽的。

**时区变更的影响**：`day_start_at` 已经按写时的时区固化成毫秒值。用户换时区后，
旧的 `day_start_at` 不会重算，按新时区生成的窗口边界会和旧 bucket 的日界错开最多
一个时区偏移量。Android 端接受这个偏差，没有做迁移。UI 层另外监听
`ACTION_TIMEZONE_CHANGED / ACTION_DATE_CHANGED / ACTION_TIME_CHANGED` 来重建查询
（file:PlaybackStatsQueryState.kt:139-149），保证「选中的日/周/月」随时间变化即时刷新。

### 窗口到 bucket 的映射

- DAY/WEEK/MONTH/YEAR：SQL 在 `playback_stat_bucket` 上做 `day_start_at >= ? AND day_start_at < ?`
  过滤后按 `identity_key` 聚合（file:PlaybackStatsSqlQuery.kt:87-91）。一个窗口覆盖 N 个
  bucket 行，聚合时求和 `total_listen_ms`（拆成高低两个 32 位 limb 累加）、求和 `play_count`、
  取 `MIN(first_played_at)`、`MAX(last_played_at)`。
- ALL：直接读 `playback_stat` 主表，不碰 bucket（file:PlaybackStatsSqlQuery.kt:67）。

### 跨窗口的「总计」

没有独立的「总计表」。总计是按当前窗口现算的，`summary()` 对 `$source` 做一次
`COUNT(*) / SUM(play_count) / SUM(total_listen_ms)`（file:PlaybackStatsSqlQuery.kt:20-26），
`$source` 已经带上了窗口过滤条件。所以切到「周」时顶部三张卡片的数字会跟着变成
「这一周」的口径，而不是「历史总计」。

### 旧数据兼容模式

如果当前周期不是 ALL、且库里存在没有对应 bucket 行的老 `playback_stat` 记录
（`hasLegacyStats()`，file:PlaybackStatsDao.kt:153-155），SQL 会走
`bucketSource() UNION ALL legacySource(onlyWithoutBuckets = true)`（file:PlaybackStatsSqlQuery.kt:66-78），
并在 UI 上显示「只展示能被精确还原的部分统计」的提示
（file:strings_stats.xml:12，展示点 file:PlaybackStatsAdaptiveContent.kt:226-237）。
这种情况下旧记录的过滤条件是
`(CASE WHEN first_played_at > 0 THEN first_played_at ELSE last_played_at END) >= start AND last_played_at < end`，
含义是「首次播放落在窗口内且末次播放也在窗口内」，跨窗口的记录直接丢弃，
因为无法精确拆分到各天。

## 查询与分页

### 查询入口链

```
PlaybackStatsScreen (file:PlaybackStatsScreen.kt:58-62)
  → rememberPlaybackStatsQuery(period, sort, queryDay.nowMillis)   // 构造 PlaybackStatsQuery
  → rememberStatsPage(query, pageRequestState)                      // produceState 里读数据
  → repository.readSummary(query) / repository.readPage(query, cursor, 100, before)
  → PlaybackStatsRoomStore.readSummary / readPage
  → PlaybackStatsSqlQuery(query, hasBuckets).summary() / .page(cursor, limit, before)
  → PlaybackStatsDao.querySummary / queryStats (@RawQuery)
```

- `PlaybackStatsQuery` 五个字段：`period`、`sort`、`minimumListenMs`（默认 0）、
  `requirePlayCount`（默认 false）、`nowMillis`
  （file:PlaybackStatsQuery.kt:8-14）。
  统计页只传 period/sort/nowMillis 三个字段（file:PlaybackStatsQueryState.kt:41），
  其余保持默认，即 `minimumListenMs = 0`、`requirePlayCount = false`，
  过滤条件只剩 `total_listen_ms >= 0`，累计时长为 0 的行也会入榜。
- `hotPlaybackStatsQuery` 是另一条线，只给首页/资料库的「最常播放」卡片用：
  `PLAY_COUNT` 排序、`requirePlayCount = true`、周门槛 10 分钟 / 月门槛 30 分钟
  （file:PlaybackStatsQuery.kt:32-36、file:PlaybackStatsPeriod.kt:12-14、103-106）。

### 排序键

`PlaybackStatsSort` 四值：`PLAY_COUNT / LISTEN_TIME / RECENT / FIRST_PLAYED`
（file:PlaybackStatsQuery.kt:6），对应列名 `play_count / total_listen_ms /
last_played_at / first_played_at`（file:PlaybackStatsSqlQuery.kt:103-108）。

**四段排序只在 `hotOrder` 下生效，统计页的四种模式全是两段**（file:PlaybackStatsSqlQuery.kt:50-55）：

| 排序模式 | ORDER BY | 方向 |
| --- | --- | --- |
| PLAY_COUNT（`requirePlayCount=true` 时，即热门查询） | `play_count DESC, total_listen_ms DESC, last_played_at DESC, identity_key ASC` | 全降序 |
| PLAY_COUNT（统计页默认，`requirePlayCount=false`） | `play_count DESC, identity_key ASC` | 次数降序 |
| LISTEN_TIME | `total_listen_ms DESC, identity_key ASC` | 时长降序 |
| RECENT | `last_played_at DESC, identity_key ASC` | 时间降序 |
| FIRST_PLAYED | `first_played_at ASC, identity_key ASC` | 时间升序（四种里只有它是升序） |

`ascending` 只在 `FIRST_PLAYED` 时为 true（file:PlaybackStatsSqlQuery.kt:16）。
`hotOrder` 需要同时满足 `requirePlayCount && sort == PLAY_COUNT`（file:PlaybackStatsSqlQuery.kt:17），
所以统计页即使在「按播放次数」下也只有 `play_count` + `identity_key` 两级。
次级排序键 `identity_key` 是字符串主键，作用是给同分数的行一个稳定次序，
不然翻页会重复或漏行。

### 分页：键集游标（keyset），不是 LIMIT/OFFSET

`page(after, limit, before)` 生成的 SQL 形如
`... WHERE <filter> AND <cursor 条件> ORDER BY <order> LIMIT ?`
（file:PlaybackStatsSqlQuery.kt:28-33）。游标是 `PlaybackStatsCursor(sortValue, identityKey,
secondaryValue, tertiaryValue)`（file:PlaybackStatsQuery.kt:24），内容取自上一页最后一行。

- 普通排序的游标条件是
  `(sortColumn > ? OR (sortColumn = ? AND identity_key > ?))`，
  「上一页」时方向全部取反（file:PlaybackStatsSqlQuery.kt:45-48）。
- 热门排序（四段键）的游标条件是完整四元组
  `play_count < ? OR (play_count = ? AND total_listen_ms < ?) OR ... OR
  (play_count = ? AND total_listen_ms = ? AND last_played_at = ? AND identity_key > ?)`，
  保证四段排序下翻页不跳行（file:PlaybackStatsSqlQuery.kt:35-43）。
- `readPage` 里实际请求 `limit + 1` 行，多出来的一行只用来判断「还有没有下一页」，
  不返回给 UI（file:PlaybackStatsRoomStore.kt:569-577）。`pageAfter` 用它生成
  `nextCursor`，`pageBefore` 用它生成 `previousCursor`（file:PlaybackStatsRoomStore.kt:579-587）。
- `limit` 在 store 层被限制在 `1..256`（file:PlaybackStatsRoomStore.kt:570）。

### 首页条数

固定 **100**：调用点是 `repository.readPage(query, current.cursor, 100, current.before)`
（file:PlaybackStatsPageState.kt:60），repository 的默认参数也是 `pageSize: Int = 100`
（file:PlaybackStatsRepository.kt:410）。UI 的「上一页」把 offset 减 100、
「下一页」把 offset 加本页实际行数（file:PlaybackStatsPageState.kt:92-97）。

### 刷新与失效

- `repository.revisionFlow` 每次写入/清空都会推一个新版本号，页面用 `collectLatest`
  订阅它，任何写入都会触发重新读 summary + 当前页
  （file:PlaybackStatsPageState.kt:53-72、file:PlaybackStatsRepository.kt:82-84）。
- 刷新时保留上一份结果，只把 `loading` 置 true，避免整页闪烁
  （file:PlaybackStatsPageState.kt:52）。
- 游标失效的兜底：用旧游标读回空页时，把请求重置为首页再读一次
  （file:PlaybackStatsPageState.kt:61-65）。
- 跨日/跨时区时查询对象重建，页码归零：`rememberPlaybackStatsQuery` 的 identity
  包含当日 `dayKey`（file:PlaybackStatsQueryState.kt:40、58-68），测试
  file:PlaybackStatsQueryStateTest.kt:41-62 覆盖了「进入另一天 → offset 归零」。
  周期为 ALL 时 `dayKey` 取 null，跨日不重置（file:PlaybackStatsQueryState.kt:68、
  测试 file:PlaybackStatsQueryStateTest.kt:245-265）。

## UI 结构

页面文件 5 个（`app/src/main/java/moe/ouom/neriplayer/ui/screen/history/stats/`）：
`PlaybackStatsScreen.kt`（顶层与顶栏）、`PlaybackStatsComponents.kt`（指标卡、图表、行）、
`PlaybackStatsAdaptiveContent.kt`（单列/双列布局与空态）、`PlaybackStatsPageState.kt`
（分页状态与翻页按钮）、`PlaybackStatsQueryState.kt`（时间窗口与刷新）。

### 顶栏

`TopAppBar`，标题「播放统计」（file:PlaybackStatsScreen.kt:126-127）。左侧返回箭头，
右侧两个图标按钮：排序（点击弹出 4 项下拉菜单）与清空（弹二次确认）
（file:PlaybackStatsScreen.kt:139-165）。排序菜单项文案：按播放次数 / 按累计时长 /
按最近播放 / 按首次播放（file:PlaybackStatsScreen.kt:40-45、file:strings_stats.xml:22-25）。
清空确认标题「清空统计数据」、正文「确定要清空所有播放统计数据吗？此操作不可撤销。」
（file:strings_stats.xml:26-27），确认后调 `AppContainer.playbackStatsRepo.clearAll()`
（file:PlaybackStatsScreen.kt:71-74）。

### 周期选择器

五个 chip：日 / 周 / 月 / 年 / 总，默认选中「总」
（file:PlaybackStatsComponents.kt:68-74，默认值 file:PlaybackStatsScreen.kt:54）。
clean 主题下用分段控件 `CleanSegmentedTabs`，其他主题用可横滚的 `FilterChip` 行
（file:PlaybackStatsComponents.kt:81-118）。

### 顶部指标卡：三项，不是「活跃天数」

`StatsOverviewCard` 只渲染三个指标，顺序固定（file:PlaybackStatsComponents.kt:192-207）：

| 顺序 | 图标 | 数值 | 标签文案 | 展示单位 |
| --- | --- | --- | --- | --- |
| 1 | Headphones | `totalPlayCount.toString()` | 播放次数（`stats_total_plays`） | 纯数字，卡片内不写「次」 |
| 2 | AccessTime | `formatListenDuration(totalListenMs)` | 累计时长（`stats_total_time`） | 自适应（见下） |
| 3 | LibraryMusic | `trackCount.toString()` | 曲目数（`stats_track_count`） | 纯数字，卡片内不写「首」 |

没有「活跃天数」「平均时长」这类指标。`trackCount` 是窗口内有记录的**去重曲目数**
（SQL 里 `COUNT(*)` 作用在按 `identity_key` 聚合后的源上，file:PlaybackStatsSqlQuery.kt:22）。

时长格式 `formatListenDuration(ms)`（file:PlaybackStatsComponents.kt:473-482）：

- `hours > 0` → `"${hours}h ${minutes}m"`
- 否则 `minutes > 0` → `"${minutes}m"`
- 否则 `"${totalSeconds}s"`

**满 1 分钟后秒被丢弃，也没有「天」这一档**，所以 25 小时会显示成 `"25h 30m"`。
测试固化了 `3_600_000 → "1h 0m"`、`59_999 → "59s"`（file:PlaybackStatsContentTest.kt:266-267）、
`7_200_000 → "2h 0m"`（file:PlaybackStatsContentTest.kt:241）。这跟 Windows 需求里的
`d天 hh:mm:ss` 不同。

### 图表

「最常播放」条形图，取前 5 条（file:PlaybackStatsAdaptiveContent.kt:182、248）。
条形长度按当前排序模式取 `playCount` 或 `totalListenMs`
（file:PlaybackStatsComponents.kt:334-335），数值标签同样按时长格式或次数显示
（file:PlaybackStatsComponents.kt:343-344）。图表只在首页（`offset == 0`）且行数 ≥ 2
时显示（file:PlaybackStatsAdaptiveContent.kt:179-185）。

### 列表：ListItem 行，没有列头

列表不是表格，没有列头行。每行是 Material3 `ListItem`
（file:PlaybackStatsComponents.kt:379-458），内容分区：

| 位置 | 内容 |
| --- | --- |
| leadingContent | 排名数字（前 3 名加粗并用主色）+ 48dp 封面（无封面时显示音符图标） |
| headlineContent | 标题（`customName ?: name`） |
| supportingContent | 作者（`customArtist ?: artist`） |
| trailingContent | 两行：播放次数（`"%d 次"` 复数串，主色）、累计时长（`formatListenDuration`） |

即 Android 端**保留了作者列**，把「歌曲时长」换成了「累计收听时长」，播放次数与时长
都塞在行尾两行里。Windows 需求是「作者和歌曲时长都换成播放时长、播放次数」，
属于列结构差异，见「移植要点」。

双列布局下，榜单顶部有一行小标题，内容就是当前排序模式的名字
（file:PlaybackStatsAdaptiveContent.kt:132-139），所以「当前排序是什么」在列表区可见，
不必回顶栏看。单列布局没有这行标题，只靠顶栏图标按钮的 contentDescription 表示。

双列布局下空态与翻页的行为也和单列不同：榜单为空时只渲染空态、不渲染翻页按钮
（file:PlaybackStatsAdaptiveContent.kt:140-145）；单列布局无论有没有数据都会渲染翻页行
（file:PlaybackStatsAdaptiveContent.kt:188），只是按钮处于禁用态。

### 加载态与失败态

- 加载：顶栏下方 4dp 高的 `LinearProgressIndicator`（`testTag = statsLoading`），
  只占 4dp 不遮挡内容（file:PlaybackStatsAdaptiveContent.kt:196-200）。
- 失败：出现一个文本按钮「无法读取播放统计，请重试」（`stats_load_failed`，
  file:strings_stats.xml:7），点击把 `retry` 计数 +1 重新读
  （file:PlaybackStatsAdaptiveContent.kt:201-211）。

### 空态：三套文案，按数据状况区分

`StatsEmptyResult` 按优先级选文案（file:PlaybackStatsAdaptiveContent.kt:214-224）：

| 条件 | 文案 |
| --- | --- |
| `!summary.hasAnyStats` | 「还没有播放记录\n开始听歌后这里会显示统计数据」（`stats_empty`） |
| `summary.usesLegacyBreakdown` | 「这批旧同步数据只有总统计，没有按天分桶\n请在旧设备更新后再同步一次」（`stats_period_missing_breakdown`） |
| 其他 | 「当前时间范围内还没有播放统计」（`stats_period_empty`） |

空态图形是一个 64dp 的柱状图图标 + 文案（file:PlaybackStatsComponents.kt:122-137），
容器高度 180..320dp（file:PlaybackStatsAdaptiveContent.kt:215）。
加载中或失败时不显示空态文案（file:PlaybackStatsAdaptiveContent.kt:216）。

### 翻页

列表底部一行两个文本按钮「上一页」「下一页」
（file:PlaybackStatsPageState.kt:77-87、file:strings_stats.xml:5-6）。
「上一页」在没有更早页时禁用，「下一页」在 `nextCursor == null` 时禁用；
加载中或失败时两个都禁用（file:PlaybackStatsPageState.kt:78-86）。
注意这不是滚动加载，是显式翻页，页码信息靠 `StatsPageRequest.offset` 维护，
只用于显示序号（`rank = request.offset + index + 1`，file:PlaybackStatsAdaptiveContent.kt:260）。

### 响应式布局

`usePlaybackStatsTabletLayout(windowWidth)` 在窗口宽度 ≥ 720dp 时返回 true
（file:PlaybackStatsAdaptiveContent.kt:52-53）。但「平板」只是第一道门：
`resolvePlaybackStatsLayout` 还要求内容宽 ≥ 720dp 才真的分栏（`useColumns`，
file:PlaybackStatsAdaptiveContent.kt:62-72）。内容宽上限 1200dp，左右 padding
平板 24dp / 手机 8dp，左栏宽度取内容宽 32% 且不超过 320dp。判定按**窗口宽度**
而不是设备类型，横屏手机也会走双列（测试 file:PlaybackStatsWindowLayoutTest.kt:39-53）。

## 设置入口

### Android 端没有设置页入口

在 `app/src/main/java/moe/ouom/neriplayer/ui/screen/tab/settings/` 全目录 grep
`PlaybackStats` 只命中两行，且都不是入口：

- `component/traffic/SettingsTrafficManagementSection.kt:35,53` 引用的是
  `PlaybackStatsPeriod` 枚举，用于流量统计页的时间段切换（该文件第 41 行还 import 了
  `StatsPeriodSelector` 组件），与播放统计页的数据无关。
- `page/SettingsPage.kt` 的页面枚举里没有任何统计相关项（file:SettingsPage.kt:32-146），
  `SettingsHomePageGroups`（file:SettingsPage.kt:159-193）八个分组也没有统计。
- 搜索索引 `SettingsSearchIndex.kt` 里同样没有 stats 相关条目。

`SettingsTrafficManagementSection` 与播放统计的相似点只在「时间档位选择器」这一件 UI 组件，
所以 Windows 端可以借用这个思路，但不能声称「Android 把入口放在流量页」。

### 实际存在的入口

| 入口 | 位置 | 形态 | 文案 |
| --- | --- | --- | --- |
| 播放统计页 | 资料库页顶栏（`LargeTopAppBar.actions`） | `BarChart` 图标按钮，actions 行第一项 | contentDescription =「播放统计」（file:LibraryScreen.kt:371-376、file:strings_stats.xml:4） |
| 单曲统计弹窗 | 正在播放页「更多」菜单 | 列表项，图标 `BarChart` | 标题「播放统计」（file:MoreOptionsMainContent.kt:537-542） |

同一个 actions 行的第二项是 `History` 图标，打开的是「最近播放」页而不是统计页
（file:LibraryScreen.kt:377-382），两者在顶栏并列但目标页不同。

### 导航方式

图标按钮的回调一路透传，最后在顶层 `NeriApp` 里调 `navController.navigate`：

```
LibraryScreen(onOpenStats = ...)                 file:LibraryScreen.kt:252
  → LibraryHostScreen(onOpenStats = onOpenStats)  file:LibraryHostScreen.kt:262,678
  → NeriApp { onOpenStats = { navController.navigate(Destinations.PlaybackStats.route) } }
                                                  file:NeriApp.kt:1511-1513
```

路由是常量字符串 `"playback_stats"`（file:navigation/Destinations.kt:41-45），
在 `AppNavigationGraph` 里注册为 `composable`（file:AppNavigationGraph.kt:624-645），
带一套透明详情页转场（`transparentDetailEnterTransition` 等，file:AppNavigationGraph.kt:626-637），
并被登记进「透明主 Tab 详情路由」集合（file:MainTabNavigationMotion.kt:37）。
返回用 `navController.popBackStack()`（file:AppNavigationGraph.kt:641）。

单曲统计弹窗不导航，直接在当前页弹 `AlertDialog`，展示三项：首次播放（
`yyyy/MM/dd HH:mm` 格式化）、累计收听（同样 `h m` / `m` / `s` 格式）、播放次数（
`"%d 次"`）（file:MoreOptionsMainContent.kt:545-573）。

### 对 Windows 端的含义

需求写的是「入口放在设置页面」，而 Android 把入口放在资料库顶栏。Windows 端要新增的
是设置页里的一个入口项，Android 端没有可直接照搬的「第几个分组第几项」。
可参考的是 Android 设置页的分组装配方式（`SettingsHomePageGroups` 一个分组一组、
组内平铺不嵌套，file:SettingsPage.kt:159-193），以及「入口点击后只做路由跳转、
不在设置里存任何状态」这一点（file:NeriApp.kt:1511-1513 的回调体里只有 navigate）。

## 关键 SQL 摘录

统计页用到的两类查询（summary 与分页）由 `PlaybackStatsSqlQuery` 用 `SimpleSQLiteQuery`
动态拼出，通过 `@RawQuery` 下发（file:PlaybackStatsDao.kt:163-167），
因为窗口边界、排序、游标都是运行时参数。`PlaybackStatsDao` 里另外有一批固定 `@Query`
（同步导出、按主键取单行），不参与统计页读取，见本节第 8 小节。

四个组成部分：`source`（数据源）、`filter`（阈值）、`cursor`（翻页条件）、
`orderClause`（排序）。

### 1. summary：顶部三指标

原文（file:PlaybackStatsSqlQuery.kt:20-26）：

```kotlin
fun summary(): SimpleSQLiteQuery = SimpleSQLiteQuery(
    "SELECT trackCount, totalPlayCount, ${saturatedSum()} AS totalListenMs FROM " +
        "(SELECT COUNT(*) AS trackCount, COALESCE(SUM(play_count), 0) AS totalPlayCount, " +
        "COALESCE(SUM(MAX(total_listen_ms, 0) >> 32), 0) AS hi, " +
        "COALESCE(SUM(MAX(total_listen_ms, 0) & 4294967295), 0) AS lo FROM ($source) WHERE $filter)",
    (arguments + query.minimumListenMs).toTypedArray()
)
```

展开后（以「周」窗口、有 bucket 为例）：

```sql
SELECT trackCount, totalPlayCount,
       CASE WHEN hi + (lo >> 32) > 2147483647 THEN 9223372036854775807
            ELSE ((hi + (lo >> 32)) << 32) + (lo & 4294967295) END AS totalListenMs
FROM (
  SELECT COUNT(*) AS trackCount,
         COALESCE(SUM(play_count), 0) AS totalPlayCount,
         COALESCE(SUM(MAX(total_listen_ms, 0) >> 32), 0) AS hi,
         COALESCE(SUM(MAX(total_listen_ms, 0) & 4294967295), 0) AS lo
  FROM (<source>)
  WHERE total_listen_ms >= ?
)
```

意图：

- 内层对 `$source` 做聚合，`COUNT(*)` 就是「曲目数」，`SUM(play_count)` 是「播放次数」，
  时长拆成高 32 位 `hi` 与低 32 位 `lo` 两个整数累加。
- 外层 `saturatedSum()`（file:PlaybackStatsSqlQuery.kt:81）把两个 limb 拼回 64 位：
  `((hi + (lo >> 32)) << 32) + (lo & 4294967295)`，其中 `hi + (lo >> 32)` 是进位修正。
  左移 32 位要求高半部分不超过 2^31-1，超过就无法用 int64 表示，于是提前判
  `> 2147483647` 并返回 `Long.MAX_VALUE`。注释写明理由：「两个整数 limb 分开聚合，
  在千万行和 Long 上限输入下仍保持精度」。若直接用 `SUM(total_listen_ms)`，
  SQLite 在累加超界时不会报错，结果是静默回绕。
- `WHERE $filter` 只有 `total_listen_ms >= ?`，`?` 绑定的是 `query.minimumListenMs`
  （file:PlaybackStatsSqlQuery.kt:18、25）。`requirePlayCount = true` 时追加
  `AND play_count > 0`。
- 只有一次查询，三张指标卡的数字来自同一行，不会出现「次数和时长口径不一致」。

### 2. page：分页读取

原文（file:PlaybackStatsSqlQuery.kt:28-33）：

```kotlin
fun page(after: PlaybackStatsCursor?, limit: Int, before: Boolean = false): SimpleSQLiteQuery {
    val params = (arguments + query.minimumListenMs).toMutableList()
    val cursor = if (after == null) "" else if (hotOrder) hotCursorClause(after, before, params) else sortedCursorClause(after, before, params)
    params.add(limit)
    return SimpleSQLiteQuery("SELECT * FROM ($source) WHERE $filter$cursor ORDER BY ${orderClause(before)} LIMIT ?", params.toTypedArray())
}
```

展开（首次请求、无游标、按播放次数）：

```sql
SELECT * FROM (<source>)
WHERE total_listen_ms >= ?
ORDER BY play_count DESC, identity_key ASC
LIMIT ?
```

意图：

- `SELECT *` 直接把 `playback_stat` 或聚合后的 bucket 行映射回 `PlaybackStatEntity`，
  列名与实体字段一一对应，不需要别名表。
- `after == null` 时 `$cursor` 是空串，即首页不带任何游标条件。
- `LIMIT ?` 绑定 `limit + 1`（调用方传的），多读一行只用于判断是否还有下一页
  （file:PlaybackStatsRoomStore.kt:573）。

### 3. 游标条件：普通排序

原文（file:PlaybackStatsSqlQuery.kt:45-48）：

```kotlin
private fun sortedCursorClause(after: PlaybackStatsCursor, before: Boolean, params: MutableList<Any>): String {
    params.addAll(listOf(after.sortValue, after.sortValue, after.identityKey))
    return " AND ($sortColumn ${if (ascending != before) ">" else "<"} ? OR ($sortColumn = ? AND identity_key ${if (before) "<" else ">"} ?))"
}
```

展开（下一页、按播放次数）：

```sql
AND (play_count < ? OR (play_count = ? AND identity_key > ?))
```

意图：严格的键集（keyset）分页。第一段比较主排序值，第二段在主排序值相等时用
`identity_key` 打破平局。`before` 为真时所有比较符取反，得到「上一页」。
不用 OFFSET 是因为排序值会随播放变化，OFFSET 在数据变动后会重复或漏行。

### 4. 游标条件：热门四段排序

原文（file:PlaybackStatsSqlQuery.kt:35-43）：

```kotlin
private fun hotCursorClause(after: PlaybackStatsCursor, before: Boolean, params: MutableList<Any>): String {
    params.addAll(listOf(after.sortValue, after.sortValue, after.secondaryValue, after.sortValue,
        after.secondaryValue, after.tertiaryValue, after.sortValue, after.secondaryValue, after.tertiaryValue, after.identityKey))
    val comparison = if (before) ">" else "<"
    val identityComparison = if (before) "<" else ">"
    return " AND (play_count $comparison ? OR (play_count = ? AND total_listen_ms $comparison ?) OR " +
        "(play_count = ? AND total_listen_ms = ? AND last_played_at $comparison ?) OR " +
        "(play_count = ? AND total_listen_ms = ? AND last_played_at = ? AND identity_key $identityComparison ?))"
}
```

展开（下一页）：

```sql
AND (play_count < ? OR (play_count = ? AND total_listen_ms < ?)
     OR (play_count = ? AND total_listen_ms = ? AND last_played_at < ?)
     OR (play_count = ? AND total_listen_ms = ? AND last_played_at = ? AND identity_key > ?))
```

意图：四段排序键必须配四段游标条件，否则当 `play_count` 与 `total_listen_ms` 都相等时
会重复读同一批行。参数一共 10 个，对应四个 OR 分支的累积绑定。

### 5. orderClause

原文（file:PlaybackStatsSqlQuery.kt:50-55）：

```kotlin
private fun orderClause(before: Boolean): String {
    val identityDirection = if (before) "DESC" else "ASC"
    if (!hotOrder) return "$sortColumn ${if (ascending != before) "ASC" else "DESC"}, identity_key $identityDirection"
    val hotDirection = if (before) "ASC" else "DESC"
    return "play_count $hotDirection, total_listen_ms $hotDirection, last_played_at $hotDirection, identity_key $identityDirection"
}
```

意图：`before` 时整个排序方向翻转，这样 SQLite 按索引反向扫，结果集再被
`pageBefore` 里的 `asReversed()` 翻回正序（file:PlaybackStatsRoomStore.kt:585）。
`identity_key` 的方向始终与主排序相反（正序时 ASC，反序时 DESC），保证平局行的次序稳定。

### 6. source：数据源三选一

原文（file:PlaybackStatsSqlQuery.kt:66-78）：

```kotlin
private fun sourceSql(hasBuckets: Boolean): String = when {
    query.period == PlaybackStatsPeriod.ALL -> "SELECT * FROM playback_stat"
    hasBuckets -> bucketSource() + " UNION ALL " + legacySource(onlyWithoutBuckets = true)
    else -> legacySource()
}

private fun legacySource(onlyWithoutBuckets: Boolean = false): String {
    val range = query.period.resolvePlaybackStatsTimeRange(query.nowMillis)
    arguments.add(checkNotNull(range.startInclusive))
    arguments.add(range.endExclusive)
    val withoutBuckets = if (onlyWithoutBuckets) " AND NOT EXISTS (SELECT 1 FROM playback_stat_bucket b WHERE b.identity_key = playback_stat.identity_key)" else ""
    return "SELECT * FROM playback_stat WHERE (CASE WHEN first_played_at > 0 THEN first_played_at ELSE last_played_at END) >= ? AND last_played_at < ?$withoutBuckets"
}
```

意图：`ALL` 直接读主表；有 bucket 时用 bucket 聚合 + 无 bucket 的旧记录兜底；
无 bucket 时全走旧记录过滤。旧记录过滤用「首次播放 >= 窗口起点 且 末次播放 < 窗口终点」，
即只保留完全落在窗口内的记录。

### 7. bucketSource：按天聚合

原文（file:PlaybackStatsSqlQuery.kt:83-100）：

```kotlin
private fun bucketSource(): String {
    val range = query.period.resolvePlaybackStatsTimeRange(query.nowMillis)
    val start = checkNotNull(range.startInclusive)
    repeat(3) { arguments.add(start); arguments.add(range.endExclusive) }
    return "WITH bucket_sums AS (SELECT identity_key, SUM(MAX(total_listen_ms, 0) >> 32) AS hi, " +
        "SUM(MAX(total_listen_ms, 0) & 4294967295) AS lo, MIN(SUM(play_count), 2147483647) AS play_count, " +
        "MIN(CASE WHEN first_played_at > 0 THEN first_played_at END) AS first_played_at, " +
        "MAX(last_played_at) AS last_played_at FROM playback_stat_bucket WHERE day_start_at >= ? AND day_start_at < ? GROUP BY identity_key), " +
        "totals AS (SELECT identity_key, ${saturatedSum()} AS total_listen_ms, play_count, first_played_at, last_played_at FROM bucket_sums) " +
        "SELECT b.identity_key, b.id, b.name, b.artist, b.album, b.album_id, b.cover_url, b.duration_ms, " +
        "t.total_listen_ms, t.play_count, t.last_played_at, COALESCE(t.first_played_at, 0) AS first_played_at, " +
        "b.media_uri, b.local_file_path, b.local_file_name, b.custom_name, b.custom_artist, b.custom_cover_url " +
        "FROM totals t JOIN playback_stat_bucket b ON b.identity_key = t.identity_key " +
        "WHERE b.day_start_at >= ? AND b.day_start_at < ? AND NOT EXISTS " +
        "(SELECT 1 FROM playback_stat_bucket newer WHERE newer.identity_key = b.identity_key " +
        "AND newer.day_start_at >= ? AND newer.day_start_at < ? AND " +
        "(newer.last_played_at > b.last_played_at OR (newer.last_played_at = b.last_played_at AND newer.day_start_at > b.day_start_at)))"
}
```

意图逐段：

- `WITH bucket_sums`：按 `identity_key` 分组，累加窗口内每天的时长与次数，
  `MIN(SUM(play_count), 2147483647)` 把次数饱和到 Int 上限（列类型是 Int）。
- `WITH totals`：把两 limb 拼回 64 位时长，`first_played_at` 用
  `MIN(CASE WHEN first_played_at > 0 THEN first_played_at END)` 跳过 0 值取最早。
- 最后的 SELECT 里 `totals JOIN playback_stat_bucket` 是为了取元数据（标题/作者/封面/时长），
  `NOT EXISTS` 子查询只保留窗口内**最后一次出现**的那一行 bucket 作为元数据来源
  （条件 `newer.last_played_at > b.last_played_at` 或时间相同时 `newer.day_start_at > b.day_start_at`）。
  这就是「最近一次播放时的标题与封面」这个快照语义的实现。
- 窗口起止被绑定三次（`repeat(3)`，file:PlaybackStatsSqlQuery.kt:86），
  对应三处 `day_start_at >= ? AND day_start_at < ?`。绑定次数被测试逐位核对
  （file:PlaybackStatsSqlQueryBindingTest.kt:39-46）。

### 8. 主表侧的固定查询（非统计页）

`PlaybackStatsDao` 里还有一批 `@Query` 固定 SQL，用于同步与备份的全量导出，
不参与统计页读取：`getStats()` 按 `last_played_at DESC, identity_key ASC`、
`getBuckets()` 按 `day_start_at ASC, identity_key ASC`、
`getCounterShards()` / `getDailyCounterShards()` 按主键全序
（file:PlaybackStatsDao.kt:22-45）。导出分页用的是主键 keyset
（`WHERE identity_key > :after` / `WHERE (day_start_at, identity_key) > (:day, :identity)`），
同样不是 OFFSET（file:PlaybackStatsDao.kt:105-130）。

## 移植要点

前提：Windows 端落盘是单个 `play_stats.json`（见 `M9-win-playback-stats.md` §3 的方案），
没有 SQL、没有索引、没有事务，全部查询在内存里用 Python 完成。

| 能力 | Android 做法 | Windows 端等价做法 | 可省与否 |
| --- | --- | --- | --- |
| 单曲累计（时长/次数/首末时间） | `playback_stat` 主表，一歌一行，`identity_key` 主键 | `stats` 数组一条记录，`key` 字段标识；顶部三指标直接遍历求和 | 必须保留，这是需求本体 |
| 按天聚合 | `playback_stat_bucket`，`(day_start_at, identity_key)` 复合主键，一行一天 | 每条记录内嵌 `"daily": {"2026-10-30": {"listen_ms": 60000, "play_count": 1}}` | v1 可省，见下「按天聚合」 |
| 时间窗口查询 | SQL 在 bucket 上按 `day_start_at` 范围过滤再 `GROUP BY identity_key` | 遍历每条的 `daily` 字典，按日期键字符串比较过滤后重新求和 | 取决于是否做 `daily` |
| 日界计算 | `playbackStatsDayStartAt()`：本地时区归到 00:00，存成毫秒 | `datetime.date.today().isoformat()` 直接产出 `"YYYY-MM-DD"` 字符串 | 必须保留（决定日桶归属） |
| 分片计数器 | `playback_stat_counter_shard` + `playback_stat_daily_counter_shard`，按 `(identity_key, device_id, epoch_started_at)` 记增量 | 无对应物，单进程直接 `+=` | 必须省掉，见下「分片计数器」 |
| 游标分页 | keyset 游标（`PlaybackStatsCursor`，四段热排 / 两段常规） | `sorted()` 后切片 `rows[start:start+page_size]` | 可省，改为内存切片 |
| 排序 | SQL `ORDER BY` + 索引 | `sorted(key=..., reverse=...)`，复合键用元组 | 保留逻辑，实现换掉 |
| 顶部汇总 | `summary()` 一次聚合查询，两 limb 累加防溢出 | `sum(r["listen_ms"] for r in rows)`，Python int 无溢出 | 保留 |
| 旧数据兼容模式 | `UNION ALL` 老记录 + `NOT EXISTS` 去重 | 无历史包袱，新文件从空开始 | 省掉 |
| 清空 | `clearAll()` 写 cleared_at 屏障，防旧增量回灌 | 清空内存表后整份写空文件 | 保留，实现简化为清空 + 重写 |
| 响应式双列 | 按窗口宽度 720dp 切单列/双列 | Qt 里按 `QWidget.width()` 切左右两个容器 | 可省，Windows 窗口宽度通常够 |

### 按天聚合在 JSON 里怎么表达

Android 用独立 bucket 表，有三个原因：SQL 按范围过滤快、桶行冗余存元数据让快照语义成立、
同步时能按 `(day_start_at, identity_key)` 做增量合并。Windows 端这三条都不成立，
所以表达方式要换。

建议形态（每条记录一个子字典，日期为键）：

```json
{
  "key": "netease:185811",
  "listen_ms": 18734000,
  "play_count": 37,
  "first_played_at": 1759210000000,
  "last_played_at": 1798760000000,
  "daily": {
    "2026-10-28": {"listen_ms": 601000, "play_count": 2},
    "2026-10-29": {"listen_ms": 258000, "play_count": 1}
  }
}
```

要点：

1. 键用 `"YYYY-MM-DD"` 本地日期字符串，不用 Android 的「本地零点毫秒值」。理由是
   JSON 里日期字符串天然可排序、可读、跨时区改机时不会因为偏移量变成「昨天」，
   而毫秒值在 JSON 里是一串裸数字，排查问题时要人脑换算。
2. `daily` 是可选键。v1 不写入 `daily` 时，老文件仍能正常读，只是没有日粒度查询。
   这给「先上线单曲累计，后续再加按天」留了不改变文件结构的路。
3. 顶层 `listen_ms` / `play_count` 是 `daily` 的累计值，两者必须同时更新。可以在加载时
   校验：若 `sum(daily[*].listen_ms) != listen_ms`，以 `daily` 为准并给出一次告警日志，
   不做静默修正。
4. 窗口查询就是一次字典遍历：`{k: v for k, v in daily.items() if start_date <= k < end_date}`，
   `start_date` 用 `(today - timedelta(days=n)).isoformat()`。这跟 Android 的
   `resolvePlaybackStatsTimeRange` 语义一致，但少了一次毫秒 ↔ 日期换算。
5. 日桶过期清理：`daily` 会无限增长。Android 靠同步策略处理，Windows 端建议在 flush 时
   丢弃超过 400 天的键（覆盖「年」窗口 365 天留余量）。清理逻辑要放进 flush 而不是加载，
   避免加载时改动用户数据。
6. 体积代价：一天一个键约 40 字节。一年 365 天、每天听 10 首，约 100KB 量级，
   相对 1 万首 3MB 的主表可以忽略。

如果确定 v1 不做时间窗口筛选（Windows 需求里没有），`daily` 整段可以省略，只留
`first_played_at` / `last_played_at`。代价是将来加按天查询时必须从某一刻开始重新累积，
历史分布无法重建。这一点在 `M9-win-playback-stats.md` §3「是否按天存 bucket」里已经给出
同样结论：v1 不做，v2 再加。

### 分片计数器要不要保留

结论：**不保留**。

Android 保留分片有三个理由，逐条对 Windows：

1. 多设备合并语义。同步时两端都要把对方的增量合进来，如果只有一个总数，
   「A 有 10 次、B 有 3 次，合并后应该 13」就无法从两个总数推出，只能靠
   「base + 各设备增量」的拆法。`SyncPlaybackCounterMergePolicy` 正是拿
   `counterBaseListenMs` / `counterBasePlayCount` 加上双方 `counterShards` 逐片相加
   （file:SyncPlaybackStatsMergePolicy.kt:80-96、114-115），
   加法走饱和实现（file:SyncPlaybackCounterArithmetic.kt:4-16）。
   Windows 端没有同步通道，`sync` / `merge` / `device_id` 整条线不存在。
2. epoch 与清除屏障。`epochStartedAt` 还承担「清空后旧增量不得回灌」的判据：
   `applyDelta` 里 `delta.epochStartedAt < state.clearedAt` 的增量被直接丢弃
   （file:PlaybackStatsRoomStore.kt:520-524），bucket 侧再用
   `firstPlayedAt >= epochStartedAt` 判断是否要开新桶（file:PlaybackStatsDeltaPolicy.kt:49-50）。
   Windows 端清空就是重写文件，没有待处理队列，不存在回灌路径。
3. 分片带来的行数增长与查询无关。统计页的读取完全走 `playback_stat` 与
   `playback_stat_bucket`，四张分片表只在同步导出/合并时读
   （`getCounterShards()` / `getDailyCounterShards()`，file:PlaybackStatsDao.kt:34-45）。
   所以就算保留分片，对统计页性能也没有影响；保留它纯粹是为了同步正确性。

结论：Windows 端只要保留「顶层累计值 + 可选 daily 子字典」两层，
`device_id` / `epoch` / 分片求和全部不出现。若将来真做多端同步，正确做法是加
`"shards": [{"device": ..., "epoch": ..., "listen_ms": ..., "play_count": ...}]`
子结构并只在这些条目上求和，而不是现在预埋空数组。

### 分页要不要保留

Android 的 keyset 分页是为 `LazyColumn` 滚动准备的，100 条一页，靠游标续读。
Windows 端用 `QTableWidget` 一次性铺满，2000 首行也就 2000 个 item，直接全量填表即可，
`M9-win-playback-stats.md` §2 的落点清单里已经按「参考 `_fill_song_table()` 复制」定好了。
所以 `PlaybackStatsCursor`、`nextCursor`、`previousCursor`、`readPage` 这一整套不移植，
只保留「排序后切片」这一个纯函数，方便将来行数真的上万时再挂分页。

### 时长格式化：必须重写

Android 的 `formatListenDuration` 只到分钟（file:PlaybackStatsComponents.kt:473-482），
Windows 需求明确要 `d天 hh:mm:ss`（`M9-win-playback-stats.md` 的 R2）。
两者不可复用，Windows 端另写：

```python
def format_play_duration(ms: int) -> str:
    total = max(ms, 0) // 1000
    days, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if days:
        return f"{days}天 {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"
```

注意两处口径差异：Android 版本在 `minutes == 0 && hours == 0` 时输出 `"Ns"`（如 `"59s"`），
上面这份统一输出 `hh:mm:ss`（如 `"00:00:59"`）；Android 版本没有「天」这一档，
超过 24 小时会输出 `"25h 30m"` 这类大时数，上面这份换成 `"1天 01:30:00"`。
这是需求差异，不是移植错误，实现时以 Windows 需求为准，不要向 Android 对齐。

### 排序与列结构：按需求改，不照搬

- Android 列表行保留了作者（`supportingContent`）和封面，行尾两行放次数与时长
  （file:PlaybackStatsComponents.kt:379-458）。Windows 需求是「作者和歌曲时长换成
  播放时长、播放次数」，即五列：序号 / 封面 / 标题 / 播放时长 / 播放次数。
  照搬 Android 会多一列作者、少一列独立的次数列。
- Android 的排序入口在顶栏下拉菜单，四个模式；需求 R6 要的是「点列头切换升降序」。
  两套交互不冲突，但实现上以列头排序为准，下拉菜单可以不做。
- 默认排序：Android 统计页默认 `PLAY_COUNT` 降序（file:PlaybackStatsScreen.kt:55），
  与需求 R6「按播放次数降序排列」一致，沿用。
- 次级排序：Android 用 `identity_key ASC` 保证稳定（file:PlaybackStatsSqlQuery.kt:52-54）。
  Windows 端 `sorted()` 是稳定排序，但同次数行的次序取决于原始数组顺序，文件重写后可能变。
  建议显式加 `key` 作为次级键，与 Android 对齐。

### 空态与三套文案

Android 三套空态文案里，「旧数据兼容」那条（`stats_period_missing_breakdown`）依赖
同步历史，Windows 端没有这个状况，只保留两条：全新无数据（对应 `stats_empty`）、
当前窗口无数据（对应 `stats_period_empty`）。后者只在做了时间窗口筛选时才有意义。

### 可以照搬的部分

- 顶栏结构：返回 + 标题「播放统计」（file:PlaybackStatsScreen.kt:126-137）。
- 清空二次确认的文案（file:strings_stats.xml:26-27）。
- 失败态文案「无法读取播放统计，请重试」+ 重试按钮（file:strings_stats.xml:7）。
- 「当前排序模式显示在列表上方」这个做法（file:PlaybackStatsAdaptiveContent.kt:132-139），
  在列头排序的 UI 下可以改成在列头标箭头，不必单起一行标题。

## 调研进度

- [x] PlaybackStatEntities.kt
- [x] PlaybackStatsDao.kt
- [x] store/stats 7 个文件（SqlQuery / RoomStore / RoomSnapshotAccess 重点读，其余按需）
- [x] ui/screen/history/stats 5 个文件
- [x] 写入点 grep（recordPlay / recordListen）
- [x] 时间维度 grep（PlaybackStatsTimeRangeExtensions / PlaybackStatsPeriod / QueryState）
- [x] 设置页入口 grep（结论：Android 端无设置页入口，入口在资料库顶栏）
- [x] 测试（PlaybackStatsContentTest / WindowLayoutTest / QueryStateTest / PageStateTest /
      AdaptiveLayoutTest / PlaybackStatsPeriodTest / SqlQueryBindingTest）
