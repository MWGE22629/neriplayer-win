# M9 播放条音质与码率标签调研

## 调研进度

- [x] 建立文档骨架
- [x] 需求拆解
- [x] 三源数据可得性对照表
- [x] 现有文案与状态栏现状
- [x] PlayerBar 布局改造（含 ASCII 示意）
- [x] 文案与 i18n 设计
- [x] 更新时机
- [x] 样式与风险

## 需求拆解

需求要显示「当前实际生效的音质档位 + 码率」，位置在底部播放条中间控制组（模式/上一首/播放/下一首）的右侧，形如「无损 FLAC · 1411 kbps」。

拆成四件事：

1. 数据源：起播解析时才能拿到「实际生效」档位与码率（解析层已完成降级链），因此标签必须挂在解析结果上，不能从用户设置里读偏好档位。偏好档位是「想要什么」，标签要显示「实际拿到什么」。
2. 展示位置：`PlayerBar.controls_row` 当前是 `[_swipe_host, stretch, mode, prev, play, next, stretch, queue, volume_icon, volume_slider]`（`src/neriplayer_win/ui/player_bar.py:509-521`）。中间组右侧、`queue` 按钮左侧那段空档就是候选位，落在第二个 `addStretch(1)` 上。
3. 生命周期：换歌、音质降级回退、播放失败重试、预取命中等时刻都要刷新；暂停/恢复不改变音质，无需刷新。
4. 文案：档位名与码率单位要分源处理，码率缺失时降级为只显示档位名或不显示。

现状的关键结论：三源都已经在状态栏文案里拼了音质后缀，但**只有 B站带码率**，网易云与酷狗只有档位名（酷狗按实测码率反推档位名，也不显示数字）。所以这次改动的实质是「把已有的后缀数据搬到播放条，并给网易云/酷狗补码率」。

## 三源数据可得性对照表

| 音源 | 档位键来源 | 展示名（现状） | 码率来源 | 缺失表现 |
|---|---|---|---|---|
| 网易云 | `PlayableUrl.level`（`api/netease/models.py:109`），来自响应 `data.level`（`api/netease/client.py:243`） | `_NETEASE_LEVEL_KEYS` → `quality.*`（`ui/main_window.py:242-251`） | **无**。响应解析只保留 `url/type/level/is_preview/size`（`api/netease/client.py:239-246`），`PlayableUrl` 没有码率字段 | `level` 为空则后缀为空（`ui/main_window.py:260-266`），无码率可显示 |
| B站 | `BiliAudioStreamInfo.quality_tag`（`api/bili/models.py:53`），取 `"dolby" / "hires" / None` | `_BILI_QUALITY_TAG_KEYS` → `quality.dolby / quality.hires`（`ui/main_window.py:254-257`）；普通音轨无标签 | `BiliAudioStreamInfo.bitrate_kbps`（`api/bili/models.py:52`），由 `bandwidth // 1000` 换算（`api/bili/client.py:314-316`） | `bitrate_kbps <= 0` 则后缀为空（`ui/main_window.py:269-276`）；`quality_tag=None` 时只显示码率 |
| 酷狗 | `KugouPlayableUrl.quality_key`（`api/kugou/models.py:119`），取 hash 字段名 `FileHash/HQFileHash/SQFileHash/ResFileHash` 或偏好键 | `kugou_quality_label(preferred, quality_key)` → 中文短名「无损 / 320k / 128k」（`api/kugou/selector.py:65-78`），**只有中文，不走 i18n** | `KugouPlayableUrl.bitrate_kbps`（`api/kugou/models.py:118`），来自播放接口响应 `data.bitrate`（`api/kugou/client.py:315`），服务端返回什么就是什么 | 档位名回落偏好档位短名（`selector.py:78`）；`bitrate_kbps` 为 0 时无法显示码率 |

补充结论：

- **网易云拿不到码率。** 播放地址接口 `/song/enhance/player/url/v1` 的响应里本有 `br` 字段，但解析函数 `parse_playback_response` 只挑了 `type/level/size`（`api/netease/client.py:239-246`），`PlayableUrl` 也未定义码率字段（`api/netease/models.py:106-110`）。要显示码率需要两步改动：解析层补 `br`，模型补字段。否则网易云只能显示档位名（如「无损 FLAC」「320K」），而这些档位名本身已隐含码率（320K/128K）。
- **B站 `bandwidth` 单位是 bps**，换算为 `bandwidth // 1000` 得到 kbps（`api/bili/client.py:314-316`）。DASH 音轨走 `bitrate_kbps_from_bandwidth(a.bandwidth)`（`client.py:444/458/473`）；MP4 单段回退流则用 `(size_bytes * 8) // length_ms` 估算（`client.py:483-487`），量纲同为 kbps。
- **酷狗码率表**（`api/kugou/selector.py:35-41` 原样摘录）：

```python
_HASH_BITRATES: dict[str, int] = {
    "FileHash": 128,
    "HQFileHash": 320,
    "SQFileHash": 937,
    "ResFileHash": 1642,
    "SuperFileHash": 2000,
}
```

  文档头注释另记「实测四档的返回 bitrate:128k 用 FileHash -> 128 / 320k 用 HQFileHash -> 320 / flac 用 SQFileHash -> 937 / 高码率用 ResFileHash -> 1642」（`selector.py:7-11`）。注意 SQFileHash 的实测值是 937 而非 CD 级 1411，说明酷狗 flac 档本身给的是约 937 kbps 的流；`ResFileHash` 1642 才是高于 CD 的档。回填逻辑在 `api/kugou/client.py:316-323`（`parse_play_response`）与 `client.py:847-861`（服务端自行降档时按返回码率反查字段名，容差 ±40）。

一个容易踩的点：`kugou_quality_label` 的返回值是硬编码中文「无损 / 320k / 128k」（`selector.py:28-32`），不走 `tr()`。英文界面下状态栏已经是中文短名，若把它直接搬上播放条，英文界面会出现中文音质标签，需要一并改造为 i18n 键。

## 现有文案与状态栏现状

音质文案只有一条链路：解析层算出「实际生效档位」→ 拼成后缀串 → 塞进状态栏消息 → 下一条状态栏消息把它顶掉。播放条、队列窗口、任务栏缩略图都不参与（`grep quality ui/queue_window.py ui/taskbar.py` 无命中）。

### 1. 算出来的地方（三源分派点）

统一入口 `MainWindow._resolve_song_context`（`ui/main_window.py:3808`），先读一次偏好档位 `quality = self._settings.get(SETTING_PLAY_QUALITY, "lossless")`（`main_window.py:3818`），再按 `song.source` 分三支（`main_window.py:3819-3841`）。注意这里读的是偏好值，最终展示的是解析层返回的实际值，两者可以不同（降级链生效时）。

- 网易云：`_resolve_netease_context`（`main_window.py:3842-3860`）调 `resolve_playable_url`（`api/netease/client.py:969`），后者内部按 `QUALITY_FALLBACK_ORDER`（`client.py:86-95`）逐档降级，返回 `PlayableUrl(level=...)`。后缀由 `_netease_quality_suffix(playable.level)` 产出（`main_window.py:260-266`），格式 `"({label})"`，`label` 走 `_NETEASE_LEVEL_KEYS`（`main_window.py:242-251`）映射到 `tr("quality.*")`，未识别档位原样输出英文键名。`level` 为空则后缀为空串。
- B站：`_resolve_song_context` 内联分支（`main_window.py:3819-3835`）调 `resolve_audio_stream`，后缀由 `_bili_quality_suffix(stream)` 产出（`main_window.py:269-276`），格式 `"({杜比|Hi-Res} {n}kbps)"`，`quality_tag` 为 `None` 时只剩 `"({n}kbps)"`；`bitrate_kbps <= 0` 时整体为空串。
- 酷狗：`_resolve_kugou_context`（`main_window.py:3862-3883`）调 `resolve_playable_url`，后缀由 `kugou_quality_label(quality, playable.quality_key)` 产出（`api/kugou/selector.py:65-78`，导出别名见 `api/kugou/__init__.py:55`）。该函数按**实测码率**反推短名，硬编码中文 `"无损" / "320k" / "128k"`（`selector.py:28-32`），不走 `tr()`。

试听片段单独一条：`playable.is_preview` 为真时 `status = tr("play.preview")`（`main_window.py:3850-3851`），不追加音质后缀。

### 2. 拼装与传递

后缀只被拼进 `tr("play.playing", title=..., quality=...)` 这一条消息（`i18n.py:158`）：

```
zh: "正在播放:{title}{quality}"      en: "Now playing: {title}{quality}"
```

结果存进 `_SongContext.status`（`main_window.py:380-386`，字段注释即「状态栏文案（含实际生效音质 / 试听提示）」）。预取路径复用同一份字符串：`_PrefetchedPlay.status`（`main_window.py:389-398`）在 `_start_play(..., status=prefetched.status, from_prefetch=True)` 时原样带出（`main_window.py:3792-3801`）。

### 3. 显示的位置

唯一显示点：`_start_play` 末尾 `self.statusBar().showMessage(status or tr("play.playing", title=song.title, quality=""))`（`main_window.py:3994-3996`）。状态栏是 `QMainWindow` 默认的，首次调用在 `main_window.py:1622` 隐式创建，全文件无 `addWidget` / `addPermanentWidget`，因此这行消息落在左侧易变区，窗口缩放、其他提示都会影响它。

托盘气泡**不带音质**：`_handle_play_failure` 只发 `tr("play.failed_title")` + `f"{song.title}\n{message}"`（`main_window.py:4186-4189`），熔断时同理（`main_window.py:4193-4197`）。所以「音质文案出现在托盘气泡」这一说法不成立，气泡只承载失败信息。

### 4. 什么时候消失

`showMessage` 无超时参数即常驻，被下一次 `showMessage` 覆盖。起播后能顶掉它的调用至少有：模式切换（`main_window.py:4276`）、播完（`4256`）、失败与自动跳过（`4148/4152/4168/4183/4192`）、路由暂停/恢复（`4540/4553`）、停止（`4602`）、解析开始（`3805` 的 `play.resolving`，下一首起播时先出现）。也就是说音质信息在状态栏里是「一闪而过、随事件蒸发」，这正是把标签挪到播放条的动机。

语言切换后这条消息不会被重译（`_on_language_changed` 直接 `showMessage(tr("common.ready"))` 覆盖，`main_window.py:4369`）。

### 5. i18n 里的全部 `quality.*`（原文照抄，`i18n.py:322-331`）

| 键 | 中文 | 英文 |
|---|---|---|
| `quality.lossless` | 无损 FLAC | Lossless FLAC |
| `quality.hires` | Hi-Res | Hi-Res |
| `quality.exhigh` | 320K | 320K |
| `quality.higher` | 192K | 192K |
| `quality.standard` | 128K | 128K |
| `quality.jymaster` | 超清母带 | Master |
| `quality.jyeffect` | 高清环绕声 | Hi-Res Surround |
| `quality.sky` | 沉浸环绕声 | Immersive Surround |
| `quality.dolby` | 杜比 | Dolby |

另有一组**只给设置页用**的同名近似键，与上面互不复用（`i18n.py:294-298`）：`settings.quality_group`（音质 / Audio Quality）、`settings.quality`（音质偏好: / Preferred quality:）、`settings.quality.lossless`（无损 FLAC / Lossless FLAC）、`settings.quality.exhigh`（极高 320K / Very High 320K）、`settings.quality.standard`（标准 128K / Standard 128K）。设置页下拉只有这三档（`ui/settings_page.py:50-52`），而 `quality.*` 覆盖 9 档，说明后者是「解析层可能返回的全部档位」，前者是「用户可选的偏好」。

`quality.higher` 的实际产出路径：网易云降级链能落到 `higher`（`client.py:93`），且 `_NETEASE_LEVEL_KEYS` 有映射（`main_window.py:246`），所以「192K」可能出现在状态栏与播放条。酷狗三条硬编码短名词表（`selector.py:70-76`）按 `>=900 / >=320 / else` 只分三档，没有 192 这一档。

## PlayerBar 布局改造

### 位置判定：两个 stretch 夹心，标签只有两个落点

`controls_row` 的实际插入顺序（`ui/player_bar.py:509-521`）：

```
addWidget(self._swipe_host)       # 封面 40 + 8 + 歌名/作者块(120~420)
addStretch(1)                     # ← 弹性空档 1
addWidget(self.mode_button)       # 36x36
addWidget(self.prev_button)       # 36x36
addWidget(self.play_button)       # 44x44
addWidget(self.next_button)       # 36x36
addStretch(1)                     # ← 弹性空档 2
addWidget(self.queue_button)      # 36x36
addWidget(self.volume_icon_label) # 20x20
addWidget(self.volume_slider)     # setFixedWidth(90)
```

`setSpacing(8)`、`setContentsMargins(0,0,0,0)`（`player_bar.py:511`），外层 `layout.setContentsMargins(12, 4, 12, 6)`（`player_bar.py:524`）。两个 stretch 同权重，窗口变宽时余量平分给两处空档，因此四键组恒落在整行中部。

实测基线（1080 宽窗口，`PlayerBar` 实宽 854，控件为 `x+width`）：

```
_Swipe:12+168 | st:154 | 模式:342+36 上首:386+36 播放:430+44 下首:482+36 | st:154 | 队列:680+36 音标:724+20 音量:752+90
```

「中间按钮右侧」在这个夹心布局里只有两个落点：空档 2 的左边（`next_button` 之后）与空档 2 的右边（`queue_button` 之前）。A 取前者，B 取后者。

### 改法 A：下一个 stretch 前插入标签

```python
controls_row.addWidget(self.next_button)
controls_row.addWidget(self.quality_label)   # ← 新增
controls_row.addStretch(1)
```

1080 宽实测（两态 `QualityLabel`，完整形态 131px，显示「无损 FLAC · 1411 kbps」）：

```
改前
 [封面+歌名 168] ..st154.. [模][上][播][下] ..st154.. [队][音][音量 90]
 12           180         342 386 430 482          680 724 752

改后（A）
 [封面+歌名 168] ..st85.. [模][上][播][下][音质 131] ..st84.. [队][音][音量 90]
 12           180        273 317 361 413 457..588       680 724 752
                                          ↑ 新增，紧贴下首键
```

窄窗（900 宽，两个 stretch 都塌成 0，标签显示短形态「无损 FLAC」）：

```
 [封面+歌名 168][模][上][播][下][无损 FLAC 120]..0..[队][音][音量 90]
 12            188 232 276 328 372..492          500 544 572
```

### 改法 B：标签放进右侧组最前

```python
controls_row.addStretch(1)
controls_row.addWidget(self.quality_label)   # ← 新增，在 queue_button 之前
controls_row.addWidget(self.queue_button)
```

1080 宽实测（同一标签）：

```
改后（B）
 [封面+歌名 168] ..st85.. [模][上][播][下] ..st84.. [音质 131][队][音][音量 90]
 12           180        273 317 361 413        541..672   680 724 752
                                                  ↑ 与下首键之间隔 128px
```

A 的标签落在 457..588，B 落在 541..672。B 在宽窗下与四键组隔着整段弹性空档，「在中间按钮右侧」只剩字面顺序。

900 宽时 A 与 B 的几何完全相同（stretch 塌成 0，标签都在 372 起），差别只在宽窗。

### 对最小宽度的影响（实测）

测量方式：以真实 `MainWindow` 实例（注册 QSS、真实 Windows 平台插件）插入标签后读 `minimumSizeHint()`，再 `resize` 读几何。基线 `PlayerBar.minimumSizeHint() = 546`、`MainWindow.minimumSizeHint() = 692`（侧栏 140 + 分隔条 6 + 播放条 546）。

先看几种「单态」策略作为对照（标签文本固定为某一串，不会切换）：

| 标签宽度策略 | label sizeHint | PlayerBar minHint | MainWindow minHint | 增量 |
|---|---|---|---|---|
| 基线（无标签） | — | 546 | 692 | — |
| 裸 `QLabel`，中文串「无损 FLAC · 1411 kbps」 | 126 | 680 | 826 | +134 |
| 裸 `QLabel`，英文串「Lossless FLAC · 1411 kbps」 | 146 | 700 | 846 | +154 |
| 裸 `QLabel`，「Immersive Surround · 1411 kbps」 | 183 | 737 | 883 | +191 |
| `setFixedWidth(110)` | 110 | 664 | 810 | +118 |
| `setFixedWidth(130)` | 130 | 684 | 830 | +138 |
| `setMinimumWidth(90)` + `setMaximumWidth(140)` | 90~140 | 644 | 790 | +98 |

规律：**PlayerBar 最小宽增量 = 标签最小宽 + 8px 间距**。单态裸标签的最小宽就是当前文本的自然宽，所以下限随曲目漂移。

本文推荐的**两态方案**（下一节详述）取短形态作 `minimumSizeHint`，中文曲目实测 767（+75），英文最长档 824（+132）。它介于 `setMinimumWidth(90)` 与裸标签之间，且完整形态能正常显示。

A 与 B 的最小宽度**完全相同**（同一组控件，stretch 不贡献最小宽），选型只看视觉落点。

### 「360px 最小宽度」这个前提需要更正

`ui/main_window.py:449` 的 `self.setMinimumSize(360, 420)` 属于 `_CoverPickerDialog`（类定义在 `main_window.py:430`），不是 `MainWindow`。全仓 `grep setMinimumSize` 只有这一处，另有 `ui/kugou_login.py:65` 给二维码图片设最小尺寸。`MainWindow` 没有显式下限，实际下限由布局最小提示决定，实测 692x249。

所以不存在 360px 这个场景：播放条自身最小宽 546 已远超 360，主窗口拖不到那里。加标签把下限抬到 732~824（两态方案，随当前曲目短形态浮动）。需要守的是「标签必须有自己的最小宽下限」，不能让裸 `QLabel` 的自然宽直接进布局。

四键也不会因为加标签被压小。它们是 `setFixedSize(36,36)` / `(44,44)`（`player_bar.py:474-479`），min 与 max 相等。窗口变窄时依次压缩的是 `info_container`（420 → 120，`player_bar.py:452-453`）与标签本身，音量条 90、音标 20 恒定（实测 692 到 1280 各宽度下末项都是 `音量+90`）。

### 标签宽度策略：两态切换，不出省略号

用户明确要求：**窄窗口下被压缩时只显示「无损 FLAC」，不显示后半段，也不要出现「…」**。因此不做中间截断，标签只有两种完整形态：

```
宽:  无损 FLAC · 1411 kbps
窄:  无损 FLAC
```

两者都是完整文本，没有残缺态，也没有省略号。

`setFixedSize` / `setFixedWidth` 仍不可用：文本宽度随音源、档位、语言三重变化。13px 微软雅黑实测（`QFontMetrics(player_bar.artist_label.font())`，真实 Windows 平台）：

| 完整形态 | 宽 | 短形态 | 宽 |
| --- | --- | --- | --- |
| 无损 FLAC · 1411 kbps | 125 | 无损 FLAC | 61 |
| 320K · 320 kbps | 93 | 320K | 29 |
| 杜比 · 448 kbps | 90 | 杜比 | 26 |
| Hi-Res · 1642 kbps | 107 | Hi-Res | 38 |
| 448 kbps | 54 | 448 kbps | 54 |
| 沉浸环绕声 · 1411 kbps | 129 | 沉浸环绕声 | 65 |
| Lossless FLAC · 1411 kbps | 145 | Lossless FLAC | 81 |
| Immersive Surround · 2000 kbps | 189 | Immersive Surround | 118 |

（均为字面宽，实际控件再加 6px 内边距，所以「无损 FLAC」两态分别是 131 / 67）

实现：

```python
class QualityLabel(QLabel):
    """音质标签：完整 / 仅档位 两态切换，不出省略号。"""

    PAD = 6

    def __init__(self) -> None:
        super().__init__("")
        self.setObjectName("qualityLabel")
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._full, self._short, self._showing_full = "", "", True
        self.setMinimumWidth(0)
        self.setMaximumWidth(220)

    def set_texts(self, full: str, short: str) -> None:
        self._full, self._short = full, short or full
        self._showing_full = True
        self.setText(full)
        self.setToolTip(full)
        self.updateGeometry()

    def _w(self, text: str) -> int:
        return QFontMetrics(self.font()).horizontalAdvance(text) + self.PAD

    def sizeHint(self) -> QSize:                      # 完整形态宽度
        return QSize(self._w(self._full), QFontMetrics(self.font()).height())

    def minimumSizeHint(self) -> QSize:              # 短形态宽度，即压缩极限
        return QSize(self._w(self._short), QFontMetrics(self.font()).height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        avail = self.width()
        if avail >= self._w(self._full) and not self._showing_full:
            self._showing_full = True
            self.setText(self._full)
        elif avail < self._w(self._full) and self._showing_full:
            self._showing_full = False
            self.setText(self._short)
```

关键点：`minimumSizeHint` 返回**短形态**宽度，所以布局下限只按短形态抬高，完整形态不会被写进下限。代价是**下限确实会抬高**，抬多少取决于短形态有多宽，实测（每项单独起一个 `MainWindow`）：

| 完整形态 / 短形态 | 完整宽 | 短宽 | PlayerBar minHint | MainWindow minHint |
|---|---|---|---|---|
| 无损 FLAC · 1411 kbps / 无损 FLAC | 131 | 67 | 621 | 767 |
| 320K · 320 kbps / 320K | 99 | 35 | 589 | 735 |
| 杜比 · 448 kbps / 杜比 | 96 | 32 | 586 | 732 |
| Hi-Res · 1642 kbps / Hi-Res | 113 | 44 | 598 | 744 |
| 沉浸环绕声 · 1411 kbps / 沉浸环绕声 | 135 | 71 | 625 | 771 |
| Lossless FLAC · 1411 kbps / Lossless FLAC | 151 | 87 | 641 | 787 |
| Immersive Surround · 2000 kbps / Immersive Surround | 195 | 124 | 678 | 824 |

基线是 `PlayerBar 546` / `MainWindow 692`。所以两态方案下主窗口下限从 692 抬到 732~824，随当前曲目的短形态宽度浮动。最宽的一档是英文 `Immersive Surround`（824），最窄是 `杜比`（732）。

若不能接受浮动，可以给标签 `setMinimumWidth(32)`（最短短形态）之外的固定下限，但那样短形态会被截断，与「不出省略号」冲突。或者把 `minimumSizeHint` 固定成 `_w(short)` 的最大值（124），下限恒定 824，代价是中文曲目在窄窗下也会有 90px 空置。v1 建议接受浮动，因为下限只在窗口拖到极窄时才有意义，实际使用窗口多在 1080 以上。

### 实测切换行为（真实 MainWindow，含侧栏与 QSS）

标签为「无损 FLAC · 1411 kbps」/「无损 FLAC」，短形态 67px、完整形态 131px（均含 6px 内边距）：

| 窗口宽 | 播放条宽 | 标签宽 | 显示文本 |
|---|---|---|---|
| 767 | 621 | 67 | 无损 FLAC |
| 850 | 624 | 70 | 无损 FLAC |
| 880 | 654 | 100 | 无损 FLAC |
| 900 | 674 | 120 | 无损 FLAC |
| **912** | **686** | **131** | **无损 FLAC · 1411 kbps** |
| 1000 | 774 | 131 | 无损 FLAC · 1411 kbps |
| 1080 | 854 | 131 | 无损 FLAC · 1411 kbps |
| 1280 | 1054 | 131 | 无损 FLAC · 1411 kbps |

逐 4px 扫描（680 到 1100）得到的唯一切换点：窗口 **912px**（播放条 686px，标签 131px）。低于此值恒为「无损 FLAC」，高于此值恒为完整串，全程无省略号。

一个副作用：900px 时标签已拿到 120px 却只显示 67px 的短形态，空置 53px。这是两态的固有代价，换取不出现残缺文本。若要在 120px 与 131px 之间显示「只到码率数字」的第三态，会引入第三个文本形态，与「只有两种完整形态」冲突，v1 不做。

**原生 `ElidedLabel` 不能用在这里**：它的 `sizeHint` 按已截断文本算，首次布局后 hint 塌缩，宽窗下永远停在最小宽；且它必然产生省略号，与本次要求相反。两态方案不继承 `ElidedLabel`，直接继承 `QLabel` 并覆写两个 hint。

**`QSizePolicy.Policy.Ignored` 不可用**：空档里两个 stretch 会吃掉全部余量。实测 `Ignored` + `setMinimumWidth(0)` 时，790/900/1080/1280 各宽度标签宽度恒为 **0**，文本不显示。

`set_tools_tip` 兜底：短形态下用户看不到码率，悬停应显示完整串，已由 `set_texts` 里的 `setToolTip(full)` 覆盖。

### 两种形态由谁提供

`_SongContext` 增加两个字段，三个 `_resolve_*_context` 各自填：

| 字段 | 内容 | 例 |
| --- | --- | --- |
| `quality_label` | 档位名（短形态） | `无损 FLAC` / `320K` / `杜比` |
| `quality_full` | 档位名 + 码率（完整形态） | `无损 FLAC · 1411 kbps` |

`quality_full` 为空（码率未知）时，`set_texts(full=label, short=label)`，两态相同，标签恒显示档位名。

### 是否挤压音量条

不挤压。`volume_slider` 是 `setFixedWidth(90)`（`player_bar.py:490`），`volume_icon_label` 是 `setFixedSize(20,20)`（`player_bar.py:482`），四键全部固定尺寸。唯一可伸缩的只有 `info_container` 与标签本身。实测 760~1280 各档下音量条几何恒为末项 90px。

## 文案与 i18n 设计

### 格式串选型

三种候选，13px 微软雅黑实测宽度（`QFontMetrics(artist_label.font())`，真实 Windows 平台）：

| 格式 | 示例（中文） | 宽度 | 示例（英文） | 宽度 |
|---|---|---|---|---|
| 档位全名 + 码率 | 无损 FLAC · 1411 kbps | 125 | Lossless FLAC · 1411 kbps | 145 |
| 编解码器 + 码率 | FLAC · 1411 kbps | — | FLAC · 1411 kbps | — |
| 短档位 + 码率 | 无损 · 1411 kbps | 89 | Lossless · 1411 kbps | — |

选**档位全名 + 码率**（复用现有 `quality.*` 值）。理由：

1. 「无损 FLAC」是现有状态栏已在用的说法（`i18n.py:323`），播放条沿用不产生第二套术语。
2. 编解码器写法会丢掉档位语义。酷狗 SQFileHash 的实测码率是 937 kbps（`selector.py:35-41`），纯看码率分不出它属于无损档还是高码率档；而「FLAC · 937 kbps」这种组合还要从 `PlayableUrl` 多取一个 `type` 字段，网易云现在解析出来又丢掉了（`api/netease/client.py:242` 返回 `type`，`client.py:1000-1002` 构造 `PlayableUrl` 时未带）。
3. 档位全名让降级可见。用户选无损但服务端只给 320K 时，标签写「320K · 320 kbps」，比只写「320 kbps」更能说明「档位掉了」。

码率单位写 `kbps`，小写带前置空格。与 Android 端一致（`PlaybackAudioInfo.kt:46` 的 `"$it kbps"`）。现有 B站状态栏后缀是 `f"{stream.bitrate_kbps}kbps"`（`main_window.py:274`），数字紧贴单位；播放条与状态栏最好统一成带空格，避免同一产品里出现两种写法。

分隔符用中点 `·` 前后各一空格。Android 播放页用 `"  ·  "`（两个空格，`NowPlayingPlaybackControls.kt:267`），桌面端字距更紧，单空格足够。

### 新增 i18n 键

```python
# -- 播放条音质标签（i18n.py，接在 quality.* 组之后）--------------------------
"player.quality_format": ("{label} · {kbps} kbps", "{label} · {kbps} kbps"),
"player.quality_kbps_only": ("{kbps} kbps", "{kbps} kbps"),
"player.quality_tooltip": ("当前音质：{text}", "Current quality: {text}"),
```

`player.quality_format` 中英同值，是因为占位符两侧只有 `label` 与数字，语言差异全部落在 `label` 上。留成键而不是硬编码拼接，是为了以后调整分隔符或单位（如改成 `1411kbps`）只改一处。

### 三源取值与降级

| 情形 | 输出 | 说明 |
|---|---|---|
| 档位有、码率有 | `无损 FLAC · 1411 kbps` | 理想路径 |
| 档位有、码率无 | `无损 FLAC` | 走 `quality.*` 值本身，不拼中点 |
| 档位无、码率有 | `1411 kbps` | 走 `player.quality_kbps_only`（B站普通音轨即此情形，`quality_tag=None`） |
| 两者都无 | 空串，标签 `setVisible(False)` | 不留占位，也不留空白 |

网易云拿不到码率时的显示：`无损 FLAC` / `320K` / `128K` / `192K`。这四个值本身就隐含码率（320K = 320 kbps），信息损失小，可以接受。`超清母带` / `沉浸环绕声` / `高清环绕声` / `Hi-Res` 四档则只剩档位名，用户看不到数字。

### 酷狗标签必须改造

`kugou_quality_label` 返回硬编码中文（`api/kugou/selector.py:28-32` 的 `QUALITY_LABELS`），英文界面下状态栏已经是中文短名。直接搬上播放条会把这个缺陷放大成常驻可见。

两种改法：

- **A（改动最小）**：`QUALITY_LABELS` 的 `"lossless" / "exhigh" / "standard"` 三个值改成 `tr()` 现取，即 `selector.py:74/78` 的 `QUALITY_LABELS["lossless"]` 换成 `tr("quality.lossless")`、`"320k"` 换成 `tr("quality.exhigh")`、`"128k"` 换成 `tr("quality.standard")`。返回变成「无损 FLAC / 320K / 128K」，与网易云档位名统一。
- **B（保持短名）**：新增 `quality.short_lossless` 等键，值为「无损 / Lossless」「320K / 320K」「128K / 128K」。

选 A。理由：A 让三源档位名归一，播放条不需要按音源切文案表；且 `quality.exhigh` 值本来就是「320K」，与酷狗原短名「320k」只差大小写。代价是酷狗「无损」变成「无损 FLAC」，而酷狗 flac 档实测只有 937 kbps（`selector.py:7-11`），标 FLAC 属事实（服务端返回的就是 flac 流），但「无损」字样的承诺比网易云弱一些，可接受。

改造后 `QUALITY_LABELS` 变成「档位键 → i18n 键」映射，`selector.py:65-78` 的 `quality_label` 返回值改走 `tr()`。`tests/test_kugou_client.py` / `test_kugou_crypto.py` / `test_kugou_integration.py` 里没有直接断言 `quality_label` 返回值（grep 无命中），但有间接依赖，改完仍需跑一遍。

### 结构化数据，不要解析字符串

不要从 `_SongContext.status` 里反解析音质（那是给状态栏的一句话，格式随语言变）。给 `_SongContext`（`ui/main_window.py:380-386`）加两个字段：

```python
@dataclass
class _SongContext:
    rotator: BackupUrlRotator
    headers: dict[str, str] | None
    status: str
    quality_label: str = ""      # 档位名（已 tr）
    bitrate_kbps: int = 0        # 0 = 未知
```

三个 `_resolve_*_context` 各自填。B站已有 `bitrate_kbps`（`api/bili/models.py:52`），酷狗已有 `bitrate_kbps`（`api/kugou/models.py:118`），网易云需要补（见下）。

### 网易云补码率

Android 端已经这么做了：`NeteasePlaybackResponseParser.kt:166-172` 从 `br` / `bitrate` / `bitrateKbps` 三个候选键取值，`>= 10000` 视为 bps 并除以 1000（实测响应里 `br` 是 1411000 这种量级），得到 1411。同文件 `:62` 还保留 `size` 作为 `contentLength`，供 `estimateBitrateKbps(contentLength, durationMs)` 在 `br` 缺失时估算（`PlayerUrlResolver.kt:139-145`）。

Windows 端对应两处改动：

- `api/netease/client.py:239-246` 的 `parse_playback_response` 返回 dict 增 `"br": _opt_int_or_none(data, "br")`，`"size"` 已经在返回里了。
- `api/netease/models.py:106-110` 的 `PlayableUrl` 增 `bitrate_kbps: int = 0`，`client.py:1000-1002` 构造处传入，量纲归一（`>= 10000` 除以 1000）。

不做这两步也能上线：网易云显示档位名，靠「320K / 128K / 192K」自带的数字传达码率。

### 档位名未识别时

`_netease_quality_suffix` 对未识别 level 原样输出键名（`main_window.py:264` 的 `label = tr(key) if key is not None else level`）。播放条沿用这个策略：网易云新增官方档位时显示英文键名（如 `jymaster2`）而不是「未知」，比吞掉更可诊断。

## 更新时机

### 唯一权威来源：解析结果

「实际生效音质」只在解析完成那一刻确定。`_SongContext` 由 `_resolve_song_context`（`ui/main_window.py:3808`）产出，整条链路里只在三处构造（`main_window.py:3828` B站、`3858` 网易云、`3875` 酷狗），没有第二处能给出实际档位。因此标签的写入时机是**拿到 `_SongContext` 的时刻**，不是 `set_track` 的时刻。

注意 `set_track` 与解析是两个分开的时点，顺序固定为先 `set_track` 后解析：

```
_play_at(index)                          # main_window.py:3773
  ├─ player_bar.set_track(title, artist)       :3788   ← 此刻还没有音质信息
  ├─ player_bar.set_cover(url)                 :3789
  ├─ _take_prefetched(index, song)             :3792
  │    └─ 命中 → _start_play(status=...)       :3792-3801
  └─ 未命中 → _resolve_and_start(song, gen)    :3806
       └─ 后台线程 _resolve_song_context → 回调 → _start_play(status=...)  :3918-3970
```

所以标签不能在 `set_track` 里清空后就撒手，必须在 `_start_play` 落值；同时 `set_track` 里应先清空（或显示占位），否则会短暂残留上一首的音质。

### 逐场景判定

| 场景 | 是否刷新 | 依据 |
|---|---|---|
| 换歌起播 | 是 | `_start_play` 收到新的 `_SongContext`（`main_window.py:3971-3997`），标签随 `status` 一起落地 |
| 预取命中 | 是 | `_prefetched.status` 在起播时带出（`main_window.py:3792-3801`），是同一份解析结果的快照 |
| 音质降级 / 回退 | 是，但不需要额外触发 | 降级链在解析层内部完成（网易云 `client.py:980-1010`、酷狗 `client.py:840-861`、B站 `bili/selector.py`），`_SongContext` 返回时已是最终档位。标签天然显示正确值，无需监听降级事件 |
| 播放失败 → backupUrls 轮换 | 否 | `_on_load_failed`（`main_window.py:4143`）只换 URL（`rotator.advance()`），同一音轨、同一码率。加刷新反而会把标签刷成重复值 |
| 播放失败 → 自动跳下一首 | 是（随换歌） | `_advance_after_failure`（`main_window.py:4217-4227`）末尾 `_play_at(index)`，走正常换歌路径 |
| 预取过期重解析 | 是 | `_on_load_failed` 的 `from_prefetch` 分支重走 `_resolve_and_start`（`main_window.py:4163-4169`），会拿到新的 `_SongContext` |
| 暂停 / 恢复 | 否 | `_on_playing_changed`（`main_window.py:4434`）只管按钮图标，音质与播放态无关 |
| seek | 否 | `_on_seek_requested`（`main_window.py:4466`）直接调 engine，不涉及解析 |
| 设置页改音质偏好 | 否，但要在下一首体现 | `_on_settings_play_quality`（`main_window.py:4292`）只落盘，注释明写「下一首解析地址时按新偏好取档」。当前这首的音质不变，标签不应动 |
| 停止播放 | 是（清空） | `_on_media_stop`（`main_window.py:4595`）只 `set_playing(False)`，没有清 `set_track`。若标签跟着 track 走就会残留，建议一并清空并隐藏 |
| 语言切换 | 是（重译） | `_on_language_changed`（`main_window.py:4345`）会调 `player_bar.retranslate()`（`main_window.py:4360`），标签要在里面按保留的结构化字段重算文案 |

### 驱动信号的选择

有三个候选信号，选哪个取决于把「音质」当音频属性还是当曲目属性：

- `set_track`：曲目属性，但调用得太早（`main_window.py:3788`），此时解析还没开始。只能用来清空。
- `_start_play`：解析完成且已交给 engine，`_SongContext` 在手（`main_window.py:3971`）。**推荐**。起播成功才算「实际生效」，失败时不该显示一个没播出来的音质。
- `engine.playing_changed`：更晚，且是布尔量，只能触发重查，拿不到音质数据。

因此：`set_track` 里清空，`_start_play` 里写入，`_on_media_stop` 里清空。三个点覆盖全部生命周期，不需要额外信号。

### 与状态栏文案的关系

`_start_play` 已有的 `status` 参数（`main_window.py:3971-3997`）是「一句话」，含标题 + 音质后缀。标签所需的两项（档位名、码率）应作为独立参数或独立对象传入，不要从 `status` 字符串里切。若照前一节给 `_SongContext` 加 `quality_label` / `bitrate_kbps` 两字段，`_start_play` 需要多接两个参数，或直接接整个 `_SongContext`（现在它拆成了 `rotator` / `headers` / `status` 三个实参，`main_window.py:3971-3977`）。

预取路径同样要带上这两个字段：`_PrefetchedPlay`（`main_window.py:389-398`）现在存 `rotator` / `headers` / `status` / `created_at`，需要同步加 `quality_label` / `bitrate_kbps`，否则预取命中的那一首标签会空掉。这是最容易漏的一处。

## 样式与风险

### 字号与颜色

对齐 `QLabel#artistLabel`（`ui/theme.py:136-139`）的次级文字规格：

```css
QLabel#qualityLabel {
    font-size: 13px;
    color: @onSurfaceVariant;
}
```

颜色变量名是 `onSurfaceVariant`（`theme.py:48` 亮色 `#43474E`、`theme.py:77` 深色 `#C3C7CF`）。这是全仓次级文字的既定色，`artistLabel`（`theme.py:136-139`）、`emptyHint`（`theme.py:128-131`）、树/表未选中项（`theme.py:176/203/240/315/468/497`）都用它。`@role` 记号由 `ThemeManager.apply`（`theme.py:603`）在切主题时整体重写 QSS（替换逻辑在 `theme.py:544-552` 的 `build_qss`），标签颜色自动跟随亮暗与动态取色（`apply_dynamic`，`theme.py:629`），无需 `retheme()` 额外处理。

不要用 `@primary`：主色在播放条里已被模式按钮图标占用（`player_bar.py:589` 的 `tinted_icon(..., "primary")`），音质标签用主色会和模式按钮抢视觉权重。

不要用 `@outline`（`theme.py:49` 亮色 `#73777F` / `theme.py:78` 深色 `#8D9199`）：那是分隔线/禁用态色，对比度偏低，13px 正文读起来吃力。

字号 13px 与 `artistLabel` 一致。`QLabel` 全局默认也是 13px（`theme.py:105`），所以不写 `font-size` 也能得到正确值，但显式写出来可防以后全局字号调整时标签单独漂移。

### 高度对齐

播放条第二行内各控件高度实测：`cover_label` 40、`play_button` 44、其余四键 36、`volume_icon_label` 20、`artist_label` 文本高 17（13px 微软雅黑，`QFontMetrics.height()`）。`QHBoxLayout` 默认垂直居中，标签无需额外设高，行高由 44px 播放键决定，标签不会撑高行。

若要与 `artist_label` 严格同基线，写 `setAlignment(Qt.AlignmentFlag.AlignVCenter)` 显式声明即可，默认行为已是居中。

### 码率数字的等宽处理

13px 微软雅黑下数字**不是等宽**：实测 `1411` 22px、`1111` 20px、`937` 21px、`1642` 27px、`320` 22px。切歌时标签宽度会因数字字形与位数双重变化。

用 `QFont.setFeature(QFont.Tag("tnum"), 1)` 强开等宽数字**对这个字体无效**：实测开启前后 `1411 / 1111 / 937` 宽度完全相同（22 / 20 / 21）。微软雅黑 UI 的 OpenType 特性表里没有 `tnum`。换 `Monospace` 风格提示也拿不到等宽数字，因为该提示只是选字族，不改变数字宽度。

所以处理手段只剩「接受抖动」或「补齐位数」两种：

- 接受抖动：标签在 90~140 区间内自动伸缩，四键与音量条都是固定宽，抖动只发生在标签自身，不推动其他控件。视觉上换歌时标签右端轻微移动。
- 补齐位数：把码率格式化成固定 4 位（`937` → ` 937`），引入前导空格，中文界面里观感差。

建议 v1 不处理。抖动幅度小于截断阈值，用户感知弱。

### 中英切换的宽度变化

同一档位的中英宽度差（13px 微软雅黑实测）：

| 档位键 | 中文 | 宽 | 英文 | 宽 | 差 |
|---|---|---|---|---|---|
| `lossless` | 无损 FLAC | 61 | Lossless FLAC | 81 | +20 |
| `exhigh` | 320K | 29 | 320K | 29 | 0 |
| `higher` | 192K | 27 | 192K | 27 | 0 |
| `standard` | 128K | 27 | 128K | 27 | 0 |
| `jymaster` | 超清母带 | 52 | Master | 40 | -12 |
| `jyeffect` | 高清环绕声 | 65 | Hi-Res Surround | 96 | +31 |
| `sky` | 沉浸环绕声 | 65 | Immersive Surround | 118 | +53 |
| `dolby` | 杜比 | 26 | Dolby | 34 | +8 |

带上码率后的全串：中文「无损 FLAC · 1411 kbps」125px，英文「Lossless FLAC · 1411 kbps」145px；最长英文「Immersive Surround · 1411 kbps」182px，对应中文「沉浸环绕声 · 1411 kbps」129px。

最坏情况差 53px（`sky` 档），英文全串比中文宽。两态方案下这不是问题：英文全串在窗口足够宽时完整显示，不够宽时整态退成短形态「Immersive Surround」，不出现截断。

需要注意 `MAX_W` 与两态切换的关系：`MAX_W` 若小于英文全串宽度（如 140 < 182），英文下标签永远拿不到足够宽度，会一直停在短形态。因此 `MAX_W` 应设 220（覆盖最长全串 182 加内边距），把「是否完整形态」的判断完全交给实际可用宽度。

`MAX_W` 只影响 `sizeHint`，不影响 `minimumSizeHint`：两态方案的 `minimumSizeHint` 取短形态宽度，所以 `MAX_W` 调到 220 也不会抬高主窗口最小宽（实测前后均为 692）。

### 与播放条现有固定尺寸控件的高度对齐

第二行各控件高度实测：`cover_label` 40、`play_button` 44、其余四键 36、`volume_icon_label` 20、`artist_label` 文本高 17（13px 雅黑）。标签不设固定高度，由 `QHBoxLayout` 按行内最高控件（44px 播放键）对齐，文本在 44px 内垂直居中。

不设 `setFixedHeight` 的理由：QSS 的 `font-size` 变更（比如以后全局调到 14px）会自动带来正确的文本高，硬编码高度会失配。若需显式对齐，用 `setAlignment(Qt.AlignmentFlag.AlignVCenter)`，默认行为已是居中。

### 风险清单

1. **`ElidedLabel.sizeHint` 按截断文本算**（`player_bar.py:49-72`）。不覆写就在首次布局后塌缩到最小宽，宽窗永远展不开。实测未覆写时 790 到 1280 各宽度标签恒为 90px。必须处理。

2. **`QSizePolicy.Policy.Ignored` 让标签宽度恒为 0**。实测该策略下 790 到 1280 标签宽都是 0、文本不显示。该策略在此布局不可用。

3. **预取路径要同步加字段**。`_PrefetchedPlay`（`main_window.py:389-398`）与 `_SongContext`（`main_window.py:380-386`）都要加音质字段。只改 `_SongContext` 会导致预取命中的那一首标签为空，而该分支只在顺序连播时触发，容易漏测。

4. **停止播放不清标签**。`_on_media_stop`（`main_window.py:4595-4602`）只改按钮与标题，不清 `set_track`。标签需显式清空并隐藏。

5. **酷狗档位名硬编码中文**（`api/kugou/selector.py:28-32` 的 `QUALITY_LABELS`）。英文界面下会显示中文。改造触及纯函数，改完要跑酷狗相关用例。

6. **状态栏与播放条信息重复**。状态栏那句是一次性提示，播放条是常驻展示，重复但不冲突。若减负可删掉 `play.playing` 的 `{quality}` 占位，但影响 `_resolve_*_context` 三处，属可选项。

7. **网易云无码率**。不补解析层字段的话，网易云只显示档位名。`无损 FLAC / 320K / 128K / 192K` 四档自带数字，`超清母带 / 沉浸环绕声 / 高清环绕声 / Hi-Res` 四档无数字可看。这是数据可得性上限。

8. **三源码率量纲不统一**。B站 `bandwidth` 是 bps，除以 1000 得 kbps（`api/bili/client.py:314-316`）；网易云 `br` 在 Android 端按 `>= 10000` 判定为 bps 再除 1000（`NeteasePlaybackResponseParser.kt:166-172`）；酷狗 `data.bitrate` 直接是 kbps（`api/kugou/client.py:315`，实测 128/320/937/1642）。统一走 `bitrate_kbps` 字段命名时，必须在各自解析入口完成归一，不能透传原始值。

9. **三源档位集合不一致**。网易云降级链含 `higher`（`api/netease/client.py:93`）且 `_NETEASE_LEVEL_KEYS` 有映射（`main_window.py:246`），所以「192K」可能出现；酷狗硬编码短名只有三档（`selector.py:65-78` 按 `>=900 / >=320 / else` 分）。播放条文案表要按「可能出现的全部档位」设计。

## 调研进度

- [x] 需求拆解
- [x] 三源数据可得性对照表
- [x] 现有文案与状态栏现状
- [x] PlayerBar 布局改造（含 ASCII 示意 + 实测宽度数据）
- [x] 文案与 i18n 设计
- [x] 更新时机
- [x] 样式与风险
- [ ] 网易云 `br` 字段的实际响应核对（属实现阶段验证项）
- [ ] 酷狗 `quality_label` 改造后的回归（属实现阶段验证项）

测量口径说明：本文所有宽度与最小尺寸数据来自真实 `MainWindow` 实例（注册 `ui/theme.py` 的 QSS、Windows 平台插件、13px 微软雅黑），不是离屏渲染的估算值。离屏模式（`QT_QPA_PLATFORM=offscreen`）缺字体，量出的宽度偏大约一倍，不可用。
