# M6 参考实现盘点：酷狗代码与多音源架构

调研对象：`reference/NeriPlayer-Android`（只读参考仓库）。
对照对象：`src/neriplayer_win/`（Windows 移植版，网易云 + B站两源）。
用途：为 M6「酷狗作为第三音源」提供可复用的代码线索与架构借鉴。
路径均以仓库根 `D:/Desktop/OpenSourse/NeriPlayer_Win` 为基准。

---

## 1. 结论

1. 参考实现里的酷狗代码只覆盖歌词，不覆盖播放。全部酷狗命名的源码都在
   `platform/lyrics/` 与 `model/.../lyrics/kugou/` 下，没有一个酷狗播放仓库、
   没有一个酷狗登录实现、没有酷狗歌单读取、没有酷狗播放地址解析。
   搜索 `kugou` 得到的非歌词引用只有：设置里的歌词源偏好、歌词偏移默认值、
   歌词编辑器来源标签、`AppContainer` 的一处单例装配、以及一个图标 SVG。
2. 因此 M6 的酷狗播放契约（登录方式、歌单接口、播放地址签名、音质档位）
   无法从参考实现抄，必须按 `TODO.md` 已定的路线实测定契约。
3. 但酷狗的歌词客户端提供了四类可复用信息：域名与参数风格、错误与空值处理约定、
   搜索结果字段（含 `hash`，这是酷狗播放侧的关键标识）、KRC 编码边界与测试写法。
4. 多音源架构值得学的有六条：统一的解析结果类型、按源拆分的解析仓库与数据源接口、
   按源区分的音质键、由 `channelId` 优先判定来源身份、按源隔离的登录态仓库、
   以及以「新增在线播放平台」为入口的接入路径文档。

---

## 2. 酷狗相关文件清单

### 2.1 主源码

| 路径 | 行数 | 职责 |
| --- | --- | --- |
| `reference/NeriPlayer-Android/modules/platform/src/main/java/moe/ouom/neriplayer/platform/lyrics/api/client/KugouLyricsClient.kt` | 99 | HTTP 客户端。三个接口：搜歌（`mobilecdn.kugou.com/api/v3/search/song`）、歌词候选（`lyrics.kugou.com/search`）、KRC/LRC 下载（`lyrics.kugou.com/download`） |
| `.../platform/lyrics/api/codec/KugouLyricCodec.kt` | 245 | 纯函数编解码：搜索结果解析、候选解析、LRC base64 解码、KRC 解密（4 字节头 + 16 字节 XOR 键 + zlib）、KRC 转 YRC 逐词时间轴、翻译提取 |
| `.../platform/lyrics/api/codec/KugouKrcTimedLine.kt` | 6 | 行级时间轴中间结构 `startMs` / `durationMs` |
| `.../platform/lyrics/repository/KugouLyricsRepository.kt` | 47 | 编排层：候选排序（`score` 降序，再按与目标时长的差值升序）、先试全部 KRC、再试全部 LRC |
| `.../modules/model/src/main/java/moe/ouom/neriplayer/data/model/lyrics/kugou/KugouLyricsModels.kt` | 22 | 三个 DTO：`KugouSongSearchResult`（id/hash/title/artist/album/durationMs）、`KugouLyricCandidate`（id/accessKey/durationMs/score）、`KugouLyricsPayload`（lyrics/translatedLyrics） |

主源码合计 419 行。

### 2.2 测试

| 路径 | 行数 | 覆盖点 |
| --- | --- | --- |
| `.../modules/platform/src/test/java/moe/ouom/neriplayer/platform/lyrics/api/codec/KugouLyricsClientTest.kt` | 202 | 搜索字段解析、候选字段解析、LRC base64、KRC 解密后转 YRC、翻译提取 |
| `.../platform/lyrics/api/client/KugouLyricsClientHttpTest.kt` | 85 | OkHttp 拦截器伪造响应：断言 host/path/query/UA、分页上限、空关键词短路、503 与空体吞成空列表、LRC 下载 URL 逐字比对 |
| `.../platform/lyrics/api/codec/KugouLyricCodecBoundaryTest.kt` | 157 | 边界：`status` 与 `error_code` 双成功标记、缺 content、坏 base64、BOM 与纯空白、4 字节头、坏 zlib（`ZipException`）、时间戳溢出、无逐词时间轴时拒绝、语言标签不可用时丢弃翻译、MIME base64 带换行 |
| `.../platform/lyrics/repository/KugouLyricsRepositoryTest.kt` | 68 | Mockito 顺序断言：全部 KRC 候选先试完再试 LRC；KRC 成功时不再发 LRC 请求；取消异常穿透 |

测试合计 512 行。

### 2.3 与酷狗相关的周边引用（非酷狗命名文件）

| 路径 | 行数 | 引用点 |
| --- | --- | --- |
| `.../platform/lyrics/repository/EditableLyricsMatcher.kt` | 580 | `searchKugou()` 分支、`rankKugouForDetailLookup()`、`sourceScore = 4`、`domesticLyricSearchLabels` 含 `kugou` |
| `.../model/.../lyrics/matching/EditableLyricMatchModels.kt` | 76 | `EditableLyricMatchSource.KUGOU`，且 `DEFAULT_EDITABLE_LYRIC_MATCH_SOURCES` 默认含 KUGOU |
| `.../model/.../settings/lyrics/LyricSourcePreference.kt` | 27 | 枚举项 `Kugou("kugou")`，映射到 `EditableLyricMatchSource.KUGOU` |
| `.../model/.../settings/lyrics/LyricOffsetDefaults.kt` | 7 | `DEFAULT_KUGOU_LYRIC_OFFSET_MS = 0L` |
| `.../app/src/main/java/moe/ouom/neriplayer/core/di/AppContainer.kt` | 735 | 474 行装配 `kugouLyricsClient`，479 行注入 `EditableLyricsMatcher` |
| `.../app/.../ui/screen/tab/settings/component/lyrics/SettingsLyricSourceSection.kt` | 153 | 歌词源选项文案（`LyricSourcePreference.Kugou` 两个分支） |
| `.../app/.../ui/screen/tab/settings/component/lyrics/SettingsLyricsOffsetSection.kt` | 388 | 酷狗歌词默认偏移项 |
| `.../app/.../ui/screen/nowplaying/lyrics/NowPlayingLyricsEditor.kt` | 1261 | 歌词编辑器来源标签与可选源列表 |
| `.../site/assets/img/desktop/icons/ic_kugou.svg` | 未计 | 站点图标，与应用逻辑无关 |

### 2.4 覆盖范围的明确结论

- 没有任何类名或方法名包含 `Kugou` 且与播放相关：没有 `KugouPlaybackRepository`、
  `KugouSongResolver`、`KugouCookieRepository`、`KugouQuality`。
- 酷狗不出现在 `PlaybackAudioSource`（`LOCAL / NETEASE / BILIBILI / YOUTUBE_MUSIC`）、
  不出现在 `MusicPlatform`（`CLOUD_MUSIC / QQ_MUSIC`）、不出现在
  `ListenTogetherChannels`、不出现在 `CommentPlatform`。
- `KugouLyricsClient.searchSongs` 服务于歌词匹配，返回值是歌词侧 DTO，
  不是通用搜索 API，不能直接当作 M6 的搜索实现。
- 酷狗歌词接口无需登录；酷狗播放必然需要登录态，参考实现里没有这部分可抄。

---

## 3. 酷狗代码里仍可借鉴的细节

### 3.1 域名与参数

| 用途 | 方法 | 域名与路径 | 参数 |
| --- | --- | --- | --- |
| 搜歌 | GET | `http://mobilecdn.kugou.com/api/v3/search/song`（注意是 http） | `format=json`、`keyword`、`page=1`、`pagesize`（上限 8）、`showtype=1` |
| 歌词候选 | GET | `https://lyrics.kugou.com/search` | `ver=1`、`man=yes`、`client=pc`、`keyword="歌手 - 歌名"`、`duration`、`hash` |
| KRC 下载 | GET | `https://lyrics.kugou.com/download` | `ver=1`、`client=mobi`、`id`、`accesskey`、`fmt=krc`、`charset=utf8` |
| LRC 下载 | GET | `https://lyrics.kugou.com/download` | 同上但 `client=pc`、`fmt=lrc` |

搜索响应的字段名：`data.info[]` 里的 `hash`、`songname`、`singername`、
`album_name`、`album_audio_id`（缺失回退 `audio_id`）、`duration`（秒）。

### 3.2 关键发现：`hash` 是酷狗侧的一等标识

`KugouSongSearchResult` 同时保留 `id`（取自 `album_audio_id`）与 `hash`，
而歌词候选接口的必填参数正是 `hash`（`KugouLyricsClient.kt` 48 行）。
这说明在酷狗的体系里，`hash` 是能把一个搜索结果带到下游接口的标识，
`album_audio_id` 只是条目编号。

推论：M6 的歌曲身份与播放请求都应把 `hash` 作为一等字段保留，不能只留 `id`。
酷狗播放接口具体用 `hash` 还是 `hash + album_id` 定位音频，需按 `TODO.md`
实测定契约，此处不作断言。但「搜索结果里至少有一个字段必须原样传到播放接口」
这条经验是确定的，值得在 M6 的数据模型里先留出位置。

### 3.3 User-Agent 与请求头

`KugouLyricsClient` 的 UA 是 `NeriPlayer/1.0 (https://github.com/cwuom/NeriPlayer)`，
不是浏览器 UA。歌词接口接受它，但播放接口与音频流大概率需要浏览器 UA
或移动端 UA，不能照搬。参考实现里对音频流的头部注入另有一套机制
（见 4.2 表格末行 `ConditionalHttpDataSourceFactory`），B站用桌面 Chrome UA 加 `Referer`。

### 3.4 错误处理约定

- `executeString` 对非 2xx 与空体一律返回 `null`，不抛异常，只写 debug 日志。
- `searchSongs` 捕获异常后返回空列表，但先单独重抛 `CancellationException`。
- 仓库层再兜一层：任何异常转 `null`，同样先重抛取消。
- 下载响应判定同时接受 `status == 200` 或 `error_code == 0`，两者都满足不了才拒绝。
- 该约定适合照搬到 M6 的只读接口（搜索、歌单、歌词）；但播放地址解析不宜
  一律吞成 `null`，需要区分「未登录」与「无版权 / 无地址」，见 4.2。

### 3.5 编码边界

KRC 解码链：base64 → 去掉前 4 字节 → 16 字节 XOR 键逐字节异或 → zlib inflate → UTF-8。
键为 `40 47 61 77 5e 32 74 47 51 36 31 2d ce d2 6e 69`。
LRC 解码链：base64 → UTF-8 → 去 BOM → CRLF 与 CR 归一 → trim → 空则拒绝。
翻译藏在 `[language:<base64 JSON>]` 标签里，取 `content[]` 中 `type == 1` 的第一块，
`lyricContent` 按行索引对齐，元素可能是数组也可能是字符串。

### 3.6 测试写法

四个测试文件的分层很清楚，可直接移植为 pytest 结构：
纯函数用固定字符串夹具覆盖边界（`KugouLyricCodecBoundaryTest`）；
网络层用拦截器队列伪造状态码与响应体，并逐字断言请求 URL（`KugouLyricsClientHttpTest`）；
编排层用 mock 断言调用顺序（`KugouLyricsRepositoryTest`）。
M6 若先做酷狗只读接口，这套分层可以在 `tests/` 下 1:1 复刻；
Windows 侧用 `httpx` 的 mock transport 替换 OkHttp 拦截器即可。

---

## 4. 多音源架构

### 4.1 文字版结构图

```
                        ┌─────────────────────────────────────────┐
                        │ app（Compose 页面 / ViewModel / 组装）    │
                        │ app/.../core/di/AppContainer.kt (735)     │
                        │ app/.../core/di/player/AppPlayerBindings │
                        └───────────────┬─────────────────────────┘
                                        │ 注入
        ┌───────────────────────────────┼────────────────────────────────┐
        │                               │                                │
┌───────▼──────────────┐   ┌────────────▼─────────────┐   ┌──────────────▼─────────────┐
│ 元数据搜索            │   │ 播放解析                  │   │ 账号 / 缓存 / 评论          │
│ SearchApi (接口)      │   │ PlayerManager             │   │ 各平台 CookieRepository     │
│ SearchManager (路由)  │   │  resolveSongUrl()         │   │ CommentRepository (接口)    │
└───────┬──────────────┘   └────────────┬─────────────┘   └──────────────┬─────────────┘
        │                               │                                │
  CloudMusicSearchApi            when(source)                      平台实现
  QQMusicSearchApi               ├─ YouTube -> YouTubeMusicPlaybackRepository
                                 ├─ Bili    -> getBiliAudioUrl -> resolveBiliSong
                                 │                              -> BiliPlaybackRepository
                                 │                              -> BiliAudioDataSource
                                 └─ else    -> getNeteaseSongUrl -> NeteaseClient

模型层（modules/model，无实现依赖）
  SongItem / SongSourceTags / SongIdentity
  SongUrlResult（Success / RequiresLogin / Failure / WaitingForAuthoritativeStream）
  PlaybackAudioInfo + PlaybackAudioSource + PreferredQualityKeys
```

### 4.2 播放解析链路

| 环节 | 文件 | 关键签名 |
| --- | --- | --- |
| 结果类型 | `.../modules/model/src/main/java/moe/ouom/neriplayer/data/model/playback/SongUrlResult.kt` | `sealed class SongUrlResult`：`Success(url, candidateUrls, durationMs, mimeType, noticeMessage, expectedContentLength, audioInfo, representationIdentity, cacheKeyOverride, isNeteaseLocalFallback, isPreviewClip, fallbackCandidates)`；`WaitingForAuthoritativeStream`；`RequiresLogin`；`Failure` |
| 播放信息 | `.../model/.../playback/PlaybackAudioInfo.kt` | `PlaybackAudioInfo(source, qualityKey, qualityLabel, qualityOptions, codecLabel, mimeType, bitrateKbps, sampleRateHz, bitDepth, channelCount)`；`enum PlaybackAudioSource { LOCAL, NETEASE, BILIBILI, YOUTUBE_MUSIC }` |
| 调度入口 | `.../modules/playback/runtime/src/main/java/moe/ouom/neriplayer/core/player/url/PlayerManagerUrlExtensions.kt`（1999 行） | `internal suspend fun PlayerManager.resolveSongUrl(song: SongItem, forceRefresh, youtubeRecoveryStrategy, sideEffects, allowGenericPrefetchCache, playbackRequestTokenOverride, shouldApplyCacheMutation): SongUrlResult`；内部 `when { isYouTubeTrack -> ...; isBiliTrack -> getBiliAudioUrl(...); else -> getNeteaseSongUrl(...) }`（379 至 391 行） |
| 来源判定 | `.../playback/runtime/.../core/player/PlayerManager.kt`（1940 行）1265 至 1271 行 | `isYouTubeMusicTrack(song)`、`isBiliTrack(song)`，均转调 `PlaybackMediaItemFactory.isBiliSource/isYouTubeSource` |
| 来源判定实现 | `.../playback/runtime/.../core/player/media/PlaybackMediaItemFactory.kt`（137 行） | `isBiliSource(song) = song.channelId == "bilibili" || song.album.startsWith("Bilibili")` |
| B站解析 | `.../modules/platform/src/main/java/moe/ouom/neriplayer/platform/bilibili/playback/resolver/BiliSongResolver.kt` | `suspend fun resolveBiliSong(song: SongItem, client: BiliClient): ResolvedBiliSong?`，处理 bvid/cid/legacy avid 回退 |
| B站播放仓库 | `.../platform/bilibili/playback/BiliPlaybackRepository.kt`（66 行） | `interface BiliAudioDataSource { val client: BiliClient; suspend fun fetchAudioStreams(bvid: String, cid: Long): List<BiliAudioStreamInfo> }`；`class BiliPlaybackRepository(source, preferredQuality: suspend () -> String)`，方法 `getBestPlayableAudio(bvid, cid, preferredKeyOverride): BiliAudioStreamInfo?`、`getAudioWithDecision(...): Pair<List<BiliAudioStreamInfo>, BiliAudioStreamInfo?>` |
| B站数据源适配 | `.../platform/bilibili/playback/BiliClientAudioDataSource.kt`（23 行） | 用 `BiliClient.getAllAudioStreams` 实现上面的接口 |
| B站选轨 | `.../platform/bilibili/playback/quality/BiliAudioSelector.kt`（103 行） | `fun selectStreamByPreference(available: List<BiliAudioStreamInfo>, preferredKey: String): BiliAudioStreamInfo?`，按偏好命中，否则沿降级链走，最后兜底任意一条 |
| 网易云 | `.../platform/netease/api/client/NeteaseClient.kt`（1547 行）834 行 | `fun getSongDownloadUrl(songId: Long, level: String = "lossless"): String`，走 eapi `/song/enhance/player/url/v1`，`code == 301` 时预热 weapi 后重试一次 |
| 网易云响应解析 | `.../platform/netease/api/playback/parser/NeteasePlaybackResponseParser.kt` | `sealed class PlaybackResult`：`Success` / `Failure(reason)`，`enum FailureReason` |
| YouTube | `.../platform/youtube/repository/YouTubeMusicPlaybackRepository.kt`（1744 行）278 行起 | `suspend fun getBestPlayableAudio(videoId, preferredQualityOverride, forceRefresh, requireDirect, preferM4a, shareInFlight, avoidDirect, allowUnverifiedDirectFallback, isPrefetch, ...)` |
| 音频流头部注入 | `.../playback/runtime/.../engine/datasource/ConditionalHttpDataSourceFactory.kt`（190 行） | 按 host 判定：B站注入 `Referer: https://www.bilibili.com` + 桌面 Chrome UA + Cookie；YouTube 注入自身头部 |

### 4.3 搜索链路

| 环节 | 文件 | 签名 |
| --- | --- | --- |
| 接口 | `.../platform/search/api/SearchApi.kt`（47 行） | `suspend fun search(keyword: String, page: Int): List<SongSearchInfo>`；`suspend fun getSongInfo(id: String): SongDetails` |
| 扩展接口 | `.../platform/search/api/NativeLyricSearchApi.kt`（8 行） | 继承 `SearchApi`，加 `suspend fun getNativeSongInfo(id: String): SongDetails` |
| 网易云实现 | `.../platform/search/api/client/CloudMusicSearchApi.kt`（168 行） | `class CloudMusicSearchApi(neteaseClient, client: OkHttpClient, debugLogging) : SearchApi` |
| QQ 实现 | `.../platform/search/api/client/QQMusicSearchApi.kt`（233 行） | `class QQMusicSearchApi(...) : NativeLyricSearchApi` |
| 路由 | `.../platform/lyrics/search/SearchManager.kt`（235 行） | `class SearchManager(private val searchApi: (MusicPlatform) -> SearchApi)`；`search(keyword, platform)`、`findBestSearchCandidate(songName, songArtist, songDurationMs)`、`selectBestSearchCandidate(...)`（时长兼容 + 名称/歌手归一化打分，阈值 60） |
| 平台轴 | `.../model/.../music/MusicPlatform.kt`（5 行） | `enum MusicPlatform { CLOUD_MUSIC, QQ_MUSIC }`，只服务元数据搜索，与播放源轴分离 |
| 装配 | `.../app/.../core/di/AppContainer.kt` 454 至 471 行 | `searchManager = SearchManager { platform -> when (platform) { ... } }` |
| 探索页搜索 | `.../app/.../ui/viewmodel/tab/ExploreViewModel.kt`（1526 行）102 行、474 至 479 行 | 自带 `enum SearchSource { YOUTUBE_MUSIC, NETEASE, BILIBILI, LINK_RECOGNITION }`，`when (source)` 分发到四个私有方法，未接入 `SearchApi` |

注意这里存在两套搜索轴：`SearchApi` 系列（歌词与元数据匹配用，两平台）与
`ExploreViewModel.SearchSource`（页面搜索用，三平台 + 链接识别）。
酷狗在参考实现里只出现在前者的歌词匹配里，不在 `SearchSource` 中。

### 4.4 统一歌曲模型与跨源身份

| 文件 | 要点 |
| --- | --- |
| `.../model/.../SongItem.kt`（57 行） | `id: Long`、`name`、`artist`、`album`、`coverUrl`、`mediaUri`、`channelId`、`audioId`、`subAudioId`、`sourceStableKey`、`streamUrl`、`playlistContextId` 等。来源由 `channelId` 与 `album` 前缀共同表达 |
| `.../model/.../SongSourceTags.kt`（7 行） | `object SongSourceTags { const val BILIBILI = "Bilibili"; const val NETEASE = "Netease" }`。注释写明历史歌曲把来源编码在 album 里 |
| `.../model/.../ltw/track/ListenTogetherChannels.kt`（8 行） | `netease` / `bilibili` / `youtubeMusic` / `local`，`channelId` 的取值来源 |
| `.../model/.../SongIdentity.kt` | `data class SongIdentity(id, album, mediaUri)`，`stableKey()` 拼 `id|album|mediaUri` |
| `.../local/.../data/identity/SongIdentityExtensions.kt`（370 行）45 至 55 行 | `fun SongItem.identity(): SongIdentity`，依次尝试 `normalizedSourceStableIdentity()`、`normalizedRemoteIdentity()`，最后回落 `id/album/mediaUri`；`fun SongItem.stableKey()` |
| 同上 258 行 | `fun SongItem.sameIdentityAs(other: SongItem?): Boolean` |
| `.../playback/logic/.../queue/identity/QueueSongIdentity.kt`（9 行） | `interface QueueSongIdentity { fun stableKey(song: SongItem): String; fun sameIdentity(song: SongItem, other: SongItem?): Boolean }` |
| `.../playback/runtime/.../session/AppQueueSongIdentity.kt`（14 行） | 用 `SongIdentityExtensions` 实现上面的接口 |
| `.../platform/comments/CommentSourceResolver.kt`（59 行） | `resolveCommentSource(song)`：先看 `channelId`，缺失才看 `album` 前缀；注释明确「绝不由最终播放地址推断平台」 |

B站把 `cid` 与 `bvid` 塞进 `album`（`Bilibili|cid|bvid`），并提供解析函数
`biliCidOrNull()` / `biliBvidOrNull()`（`BiliSongResolver.kt` 142 至 169 行）。
这是「一个 Long id 装不下的复合身份编码进 album」的既有做法，酷狗若需
`hash` 加 `album_id` 双标识，可以参考这个位置，但更干净的做法是加显式字段。

### 4.5 队列

| 文件 | 要点 |
| --- | --- |
| `.../playback/logic/.../queue/state/PlayerQueueStateStore.kt` | 队列状态单一持有者 |
| `.../playback/runtime/.../PlayerManager.kt` 488 行 | `internal val currentPlaylist: List<SongItem>` 读快照；`currentIndex` 读写走 `queueStore` |
| 同上 1272 行 | `internal fun queueIndexOf(song: SongItem, playlist: List<SongItem> = currentPlaylist): Int`，用 `sameIdentityAs` 比较 |
| `.../playback/runtime/.../url/PlayerManagerUrlExtensions.kt` 165 行 | 队列项直接进 `resolveSongUrl`，队列本身不含源特有字段，源信息全在 `SongItem` 上 |

队列不区分源，只存 `SongItem`，解析时按 `SongItem` 上的来源标记分派。
跨源混排因此天然成立，代价是 `SongItem` 必须能承载任意源的标识。

### 4.6 认证隔离

| 平台 | 文件 | 存储 | 判定键 |
| --- | --- | --- | --- |
| 网易云 | `.../platform/netease/auth/NeteaseCookieRepository.kt`（379 行） | `EncryptedSharedPreferences` 名 `netease_auth_secure_prefs`，旧存储为 DataStore 迁移 | cookie 映射，含 `MUSIC_U` |
| B站 | `.../platform/bilibili/auth/BiliCookieRepository.kt`（303 行），实现 `BiliCookieSource` | `bili_auth_secure_prefs`，同结构迁移 | `SESSDATA` / `DedeUserID` / `bili_jct` |
| YouTube | `.../platform/youtube/auth/YouTubeAuthRepository.kt` | `youtube_auth_secure_prefs` 加 `_recovery` 备用存储 | 自有 bundle |

共同形状：一个平台一个仓库、一个独立存储名、一个 `MutableStateFlow` 暴露登录态、
`getCookiesOnce()` 取快照、`saveCookies` / `clear` / `refreshHealth` 三件套、
`AccountPreferencesOpener` 统一处理加密存储不可用时的降级。
B站另外抽了 `fun interface BiliCookieSource { fun getCookiesOnce(): Map<String, String> }`
（`.../bilibili/api/auth/BiliCookieSource.kt`），让 `api` 层只依赖凭据接口。
`api` 层不得反向读取账号仓库（`CONTRIBUTING.md` 190 行有明文约束）。

### 4.7 音质档位

| 层 | 文件 | 要点 |
| --- | --- | --- |
| 模型 | `.../model/.../playback/PlaybackAudioInfo.kt` 99 至 112 行 | `data class PreferredQualityKeys(netease = "exhigh", youtube = "high", bili = "high")`；`fun PreferredQualityKeys.forSource(source: PlaybackAudioSource): String?` |
| 设置键 | `.../local/.../settings/SettingsRepository.kt`（1640 行）214 至 262 行 | 四条独立 Flow：`audioQualityFlow`、`youtubeAudioQualityFlow`、`biliAudioQualityFlow`，以及移动数据下的三条降级档位 |
| 快照 | `.../local/.../settings/playback/PlaybackPreferenceSnapshot.kt`（703 行）92 至 103 行 | 键名 `audio_quality`、`youtube_audio_quality`、`bili_audio_quality`、`mobile_data_netease_audio_quality` 等 |
| 运行时 | `.../playback/logic/.../runtime/quality/PlaybackQualityOwner.kt`（59 行） | `interface PlaybackQualityPort`；`setPreferredQuality(source, key)`；`changeCurrentPlaybackQuality(optionKey)`；`scheduleRefresh(source, reason)` |
| 生效值 | `.../playback/runtime/.../quality/PlayerManagerTrafficQualityExtensions.kt`（56 行） | `effectiveNeteaseQuality()` / `effectiveYouTubeQuality()` / `effectiveBiliQuality()`，按网络类型在默认档与移动数据档之间选择 |
| 选项构造 | `.../playback/runtime/.../url/PlayerUrlResolver.kt`（362 行）60 至 106 行 | `buildNeteaseQualityOptions(...)`、`buildYouTubeQualityOptions(...)`、`buildBiliQualityOptions(availableStreams, ...)`，以及 `qualityLabelForNetease/Bili/YouTube` |
| 档位降级 | 同上 19 至 28 行 | `NETEASE_QUALITY_FALLBACK_ORDER = jymaster, sky, jyeffect, hires, lossless, exhigh, higher, standard` |

结论：每个源有自己的档位键空间，不做统一枚举。UI 展示时用
`PlaybackAudioInfo.qualityOptions` 回传该源本次实际可选的档位。
这套设计的代价是每加一个源要新增一条设置键、一条 Flow、一个 `qualityLabelFor*`
和一个 `build*QualityOptions`；收益是不会把不同平台的档位强行等价。

### 4.8 装配

`AppContainer` 是手写懒加载单例容器（735 行），没有 Hilt。
`PlayerRepositoryDependencies` 接口（`.../playback/runtime/.../host/PlayerRepositoryDependencies.kt`，52 行）
列出播放器需要的全部仓库与客户端，`PlayerDependencies` 作为注册表在 app 启动时 `install`。
`.../playback/runtime/.../host/PlayerDependencies.kt` 提供 `repositories` 等访问器。

---

## 5. 可移植模式清单

Windows 现状列中的文件均位于 `src/neriplayer_win/`。

| # | 模式 | Android 做法 | Windows 现状 | 移植价值 | 代价 |
| --- | --- | --- | --- | --- | --- |
| 1 | 解析结果用封闭类型表达 | `SongUrlResult` 四态：`Success`（含候选 URL、audioInfo、缓存键覆盖、试听标记）、`RequiresLogin`、`Failure`、`WaitingForAuthoritativeStream` | 用异常表达登录失效（`NeteaseAuthRequiredError` / `BiliAuthRequiredError`），用 `_SongContext(rotator, headers, status)` 表达成功；失败走 `_handle_play_failure` | 高。加第三源后「未登录 / 无版权 / 无地址」三类必须分开，否则会误触发自动跳歌 | 中。需改 `_resolve_song_context`、`_resolve_and_start`、`_on_load_failed` 三处，`main_window.py` 里与播放相关的分支集中，改动可控 |
| 2 | 解析仓库与数据源接口分离 | `BiliAudioDataSource` 接口 + `BiliClientAudioDataSource` 适配 + `BiliPlaybackRepository` 持有 `preferredQuality: suspend () -> String` | 客户端方法直接完成取流与选轨：`BiliClient.resolve_audio_stream(bvid, cid, preferred_quality)` | 中。音质作为可注入函数后，预取与起播可传不同档位，也便于离线单测 | 低。只需在 `api/bili/` 外再包一层薄仓库，或仅把音质改成参数化回调 |
| 3 | 按源区分的音质键 | `PreferredQualityKeys(netease, youtube, bili)` + `forSource()`；设置里三条独立键 | 单一全局键 `play_quality`（`standard/exhigh/lossless`），B站靠 `bili_quality_key_from_netease_level()` 换算（`api/bili/selector.py` 112 行） | 高。酷狗档位（标准 / 高品 / 无损 / Hi-Res 等）与网易云三档不同名，继续换算会失真 | 中。`data/store.py` 增加键与校验，`ui/settings_page.py` 改为按源下拉，旧键迁移默认值 |
| 4 | 来源身份显式优先 | `channelId` 优先，`album` 前缀兜底，`audioId` / `subAudioId` 存复合 id，注释明确不由播放地址推断 | `QueueSong.source: str`（`"netease"` / `"bili"`）加 `id`，B站另有 `bvid`；`player/queue.py` 59 至 69 行 | 高。酷狗需要 `hash` 与条目 id 两个标识，单靠 `(source, id)` 会碰撞或丢失分页信息 | 中。`QueueSong` 加字段，`_netease_song_to_queue` / `_bili_item_to_queue` 与 `main_window.py` 中 1938 行、2479 行两处身份比较同步改 |
| 5 | 分派用映射而非 if/else 链 | `when` 里有 `else -> getNeteaseSongUrl`，默认源是网易云 | `_resolve_song_context` 用 `if song.source == "bili" ... else 网易云`；`_run_search` 同构 | 高。当前写法下新增 `"kugou"` 会被静默当成网易云处理，属于必须修正的结构问题 | 低。改成 `{source: resolver}` 字典并在未知源时显式报错 |
| 6 | 偏好档位 + 自动降级链 | 网易云 `NETEASE_QUALITY_FALLBACK_ORDER` 顺序回退；B站 `selectStreamByPreference` 按偏好命中后沿 `degradeChain` 走，最后兜底任意一条 | 两者都已具备：`QUALITY_FALLBACK_ORDER`（`api/netease/client.py` 86 行）、`select_stream_by_preference`（`api/bili/selector.py` 169 行） | 高。酷狗需要一个自己的降级链，直接沿用这两个函数的形状 | 低。新增一个酷狗档位列表与选轨函数 |
| 7 | 流请求头按 host 注入 | `ConditionalHttpDataSourceFactory` 按 host 判定注入 Referer/UA/Cookie，并订阅登录仓库实时刷新 | `build_bili_stream_headers()` 返回固定头，`PlayerEngine.play_url(url, headers)` 逐文件下发；`BackupUrlRotator` 轮换候选 URL | 中。酷狗音频流若校验 UA 或 Referer，需要同样的「按源准备头部」出口 | 低。已有 headers 通道，只需补一个酷狗头部构造函数与候选 URL 列表 |
| 8 | 一源一登录仓库 | 每平台独立加密存储名、独立登录判定键、`Flow` 暴露状态、`getCookiesOnce()` 供 `api` 层 | 独立文件 `netease.json` / `bili.json`（`data/store.py` 22 至 25 行），独立 `load/save/clear` 三件套，判定键 `MUSIC_U` / `SESSDATA` | 高。酷狗登录态必须与两源完全隔离，否则串号会污染两源 | 低。加 `kugou.json` 与一组方法；`validate_and_sanitize_netease_cookies` 可复用于清洗 |
| 9 | 缓存键在解析前确定 | `PlaybackMediaItemFactory.cacheKey` 按源拼 `netease-id-quality` / `bili-aid-cid-quality` / `ytmusic-videoid-quality` | 无持久媒体缓存，只有单槽预取 `_prefetched`，按 `_resolve_generation` 作废 | 低。mpv 不做 ExoPlayer 式分片缓存 | 不适用 |
| 10 | 预取过期后重解析一次 | 解析结果带 `from_prefetch` 语义，失败先重解析再进失败路径 | 已实现：`_on_load_failed` 中 `from_prefetch` 清标记后重解析（`main_window.py` 2511 至 2521 行） | 中。酷狗地址通常带时效，这条兜底必须保留 | 无 |
| 11 | 异步结果按代际作废 | `RequestGeneration`（`.../common/.../concurrent/RequestGeneration.kt`），文档见 `docs/kotlin-helpers.md` | 已实现：`_resolve_generation`、`_search_generation`、`_fail_skip_token` | 中。第三源不新增机制，只需沿用 | 无 |
| 12 | 搜索按源分轴 | 页面搜索有 `SearchSource` 枚举，元数据搜索有 `MusicPlatform` 枚举，两条轴分开 | 单一 `_search_source`（`"netease"` / `"bili"`），两个互斥按钮，切换即重搜（`main_window.py` 2034 至 2046 行） | 中。加酷狗最省事的是加第三个按钮；跨源合并排序是另一件事，参考实现也没有做成 | 低（加按钮）/ 高（跨源排序） |
| 13 | 失败统一出口 + 熔断 | `fun resolvePlaybackFailureAdvanceAction(currentIndex, playlistSize, repeatMode): PlaybackFailureAdvanceAction`（`.../playback/logic/.../queue/policy/failure/PlayerManagerPlaybackFailurePolicy.kt`），返回 `NEXT` / `WRAP` / `STOP` | 已实现：`_handle_play_failure` 状态栏 + 托盘气泡 + 延时跳歌 + 连续失败熔断，登录失效单独走重登 | 中。第三源接入时只需保证酷狗的错误被归类到「登录失效」或「可跳过」 | 无 |
| 14 | 纯函数编解码 + 网络层 mock | 酷狗四个测试文件的分层 | 已有 `tests/test_search.py`、`tests/test_bili_client.py` 同风格 | 高。M6 的酷狗只读接口可直接按此结构落测试 | 低 |
| 15 | 移动网络降档 | `effectiveNeteaseQuality` 等按网络类型切换 | 桌面无移动网络概念 | 不适用 | 不适用 |

---

## 6. 不可移植或不适用

| 项 | 位置 | 说明 |
| --- | --- | --- |
| Compose UI | `app/.../ui/screen/**` | Windows 用 PySide6，UI 层全部重写，只能借鉴信息层级 |
| Hilt / DI 容器 | 实际是手写 `AppContainer`（`app/.../core/di/AppContainer.kt`），另有 `PlayerDependencies` 注册表 | 没有 Hilt 可抄；可借鉴的是「按源懒加载 + 接口注入」这一习惯，Windows 用模块级单例或构造函数注入即可 |
| Kotlin 协程 / Flow | 全仓库 | `suspend`、`StateFlow`、`withContext(Dispatchers.IO)`、`CancellationException` 穿透约定都不可直接移植；Windows 对应物是 `run_async`（`ui/workers.py`）加代际 token |
| Media3 ExoPlayer 与 `HttpDataSource` | `.../playback/runtime/.../engine/**` | Windows 用 libmpv，分片缓存、`DataSpec` 改写、`setCustomCacheKey` 全部无对应物 |
| `EncryptedSharedPreferences` / Android Keystore | 各 `CookieRepository` | Windows 目前把 cookie 明文写入 `%APPDATA%/neriplayer-win/*.json`（`data/store.py`），没有加密层可对照 |
| Room / DataStore / WorkManager | `modules/database`、`modules/local` | Windows 用 JSON 文件加内存态，迁移链与事务语义都不适用 |
| `Parcelable` / `@Parcelize` | `SongItem`、`SongIdentity` | Android 进程间传递用，Windows 不需要 |
| Android 前台服务 / MediaSession / 音频焦点 | `.../playback/runtime/.../service/**` | 桌面由 Qt 托盘、全局媒体键、`ui/audio_route.py` 承担 |
| 模块边界门禁 | `build-logic`、`tools_pub/quality/module_boundaries.py`、`verifyModularization` | Gradle 专属；Windows 侧对应约束只能靠约定与测试 |
| YouTube 相关全部实现 | `.../platform/youtube/**`（`YouTubeMusicPlaybackRepository` 1744 行） | 与 M6 无关，且依赖 NewPipe、EJS 挑战、PoToken 等 Android 侧机制 |
| 歌词解析库 | `modules/lyrics`、`np-submodule/accompanist-lyrics-core` | Windows 版目前完全没有歌词功能（`grep -i lyric` 在 `src/neriplayer_win` 下无命中），酷狗歌词代码对 M6 的播放目标不构成依赖 |

---

## 7. docs 中的相关说明

参考仓库没有独立的「多音源架构」文档，相关内容分散在以下几处，均已核对。

| 文件 | 摘出的要点 |
| --- | --- |
| `reference/NeriPlayer-Android/CONTRIBUTING.md` 537 至 600 行 | 「扩展路径」四节：新增 Explore 搜索源（客户端放 `platform/<平台>/api`，业务放同库职责包，`ExploreViewModel` 加请求与分页，需要播放再接 `PlayerManager` 解析链路，需要下载再补 `AudioDownloadManager`）；新增播放页元数据补全源（实现 `SearchApi`，`AppContainer` 注册单例，在 `searchManager` 的 provider 登记路由）；新增在线播放平台（参考 `platform/bilibili/` 或 `platform/youtube/`，特殊 Header 扩 `ConditionalHttpDataSourceFactory`，在 `core/player/url/` 与 `resolver/` 接入解析链路，缓存与永久下载分开，同步到网易云喜欢需要稳定网易云 id）；新增设置项（`AutoSettingsSchema` 登记，有副作用保留手写 setter，影响启动行为要同步 snapshot） |
| `reference/NeriPlayer-Android/README.md` 603 至 641 行 | 14 个自有库的分工；`:platform` 集中远程来源与 `api` 协议职责；`:model` 不依赖项目实现；`:playback:logic` 与 `:playback:runtime` 的规则与运行分离；库源码少于 2000 行、每目录最多 16 个源文件 |
| `reference/NeriPlayer-Android/modules/platform/README.md` | 平台内部 `api` 负责协议、通过注入接口拿凭据；账号与缓存由仓库管理；协议层禁止反向读取账号仓库、Room 与播放器 |
| `reference/NeriPlayer-Android/modules/playback/runtime/README.md` | `PlayerManager` 与 `AudioPlayerService` 是主入口；播放规则来自 `:playback:logic`；app 在 `core/di/player` 安装 `PlayerDependencies` |
| `reference/NeriPlayer-Android/modules/model/README.md` | 字段名、默认值需兼容旧缓存与备份；`SongItem` 这类跨模块模型只增不改语义 |
| `reference/NeriPlayer-Android/docs/kotlin-helpers.md` | `RequestGeneration` 的用法：`advance()` 开启新一轮使旧票据失效，`capture()` 取当前轮次；先推进代次再取消旧任务 |
| `reference/NeriPlayer-Android/modules/sync/LYRIC_SYNC.md` | 歌词同步策略，与 M6 播放目标无关 |
| 本仓库 `docs/PERF.md` | 冷启动 ≤ 1.5s、常驻 ≤ 200MB 的验收线，以及 WebEngine 与 `platform.platform()` 两处启动开销的修复记录。M6 新增酷狗客户端时必须遵守，不要在模块顶层 import 重型依赖 |
| 本仓库 `TODO.md` 38 至 46 行 | M6 范围与「API 契约须以实测锚定，不凭猜测写接口」的既定路线 |

参考仓库 `docs/` 目录下只有 `kotlin-helpers.md` 与 `kotlin-helpers_EN.md`，没有架构说明文档。

---

## 8. 对 M6 的落点

1. 酷狗代码可直接复用的只有编解码与请求风格。播放部分按 `TODO.md` 既定路线实测定契约。
2. 动手前先把 Windows 侧的三处分派改成按源映射：`_resolve_song_context`
   （`main_window.py` 2218 行）、`_resolve_and_start` 里 `_make_*_play_done`
   的选择（2263 行）、`_run_search`（2065 行）。否则新增 `"kugou"` 会被
   当成网易云处理。
3. `QueueSong` 补 `hash`（酷狗播放标识）与可选的专辑 id，同步改 `main_window.py`
   里两处 `(source, id)` 身份比较。
4. 音质设置从单键改为按源键，旧 `play_quality` 作为网易云键迁移。
5. 酷狗登录态独立为 `kugou.json` 加一组 `load/save/clear`，登录判定键以实测为准。
6. 解析结果建议引入 `Success` / `RequiresLogin` / `Failure` 三态，登录失效不再
   依赖异常类型判断。
