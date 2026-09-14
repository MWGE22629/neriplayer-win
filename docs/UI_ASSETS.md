# NeriPlayer Win — UI/UX 资产盘点与补缺(M4 施工图)

> 2026-09-14 探索报告,实施于 M4。参考仓库根 `reference/NeriPlayer-Android/`(下称 `R/`)。

**结论速览**:播放控制图标(播放/暂停/上下曲/音量/红心/歌词)在 `R` 的 `drawable/` 里全是 24×24 单 path 矢量 XML,机械转 SVG 即可;应用图标有 1024 多色 SVG 与 1024 RGBA PNG;配色**没有静态色板**(运行时 materialkolor 从种子色 `#0061A4` 生成),Qt 侧需一次性"冻结"M3 蓝色方案为 QSS 变量。本仓库目前零图片资产。

**许可**:`R/LICENSE` GPL-3.0,`icon/`、`res/` 无独立许可;唯 `ic_lyricon / ic_neriplayer / ic_neriplayer_round / ic_statusbar.xml` 头部带 Apache 2.0 声明(与 GPL-3.0 兼容,保留注释即可)。

## A. 资产清单

### A1. 应用图标(`R/icon/`)

| 文件 | 形式 | 说明 | 迁移难度 |
|---|---|---|---|
| `icon/neriplayer.svg` | SVG 1024 | 透明底,淡紫吉祥物 `#c9a8fb` + 深灰蓝字标 `#494565`,平涂无渐变 | 易 |
| `icon/ic_neriplayer.svg` | SVG 1024 | 深藏青底 `#1e293a` 方形版 | 易 |
| `icon/ic_neriplayer_round.svg` | SVG 1024 | 圆形底版 | 易 |
| `app/src/main/res/drawable-nodpi/ic_launcher_foreground_material.png` | PNG 1024 RGBA | 已是光栅,可直接缩放 | 最易 |
| `drawable-nodpi/ic_launcher_monochrome_material.png` | PNG 1024 | 单色版(托盘/浅色备用) | 最易 |
| `drawable-nodpi/ic_notification_small.png` | PNG 64 | 近托盘用途 | 易 |

### A2. 功能图标(`R/app/src/main/res/drawable/`,24×24 viewport,XML→SVG 机械改写:`viewportWidth/Height`→`viewBox`、`pathData`→`d`、`fillColor`→`fill`、丢弃 `android:tint`)

- 播放/暂停:`round_play_arrow_24.xml` / `round_pause_24.xml`
- 上一首/下一首:`round_skip_previous_24.xml` / `round_skip_next_24.xml`
- 音量:`round_volume_up_24.xml`
- 红心实/空:`ic_baseline_favorite_24.xml` / `ic_outline_favorite_24.xml`
- 歌词开/关:`ic_lyrics_24.xml` / `ic_lyrics_off_24.xml`
- 返回:`ic_arrow_back_24.xml`;下载:`ic_download.xml`;刷新:`outline_refresh_24.xml`
- 快捷方式四件套:`ic_shortcut_library/play/search/shuffle.xml`(侧栏图标)
- 音源品牌标:`ic_netease_cloud_music / ic_bilibili / ic_qq_music / ic_youtube / ic_github.xml`
- 跳过:启动器/状态栏/Widget 专用

代码内 Material Icons(Outlined 风格,名称与 Material Symbols 一一对应):Download / LibraryMusic / FavoriteBorder / MoreVert / Close / AccountCircle / Search / Info / MusicNote / SkipNext / PlayArrow / Settings / Explore / Headset / Home 等。

### A3. 子模块(均为空目录,需镜像 `git submodule update --init` 后才能读)

| 子模块 | 搬什么 |
|---|---|
| `miuix` | 只抄设计值:分组大圆角卡片、hairline 分隔、HyperOS 式开关;不引库 |
| `accompanist-lyrics-ui` | 歌词功能(M4+)时读行高亮/渐变参数 |
| `accompanist-lyrics-core` | Python 已可替代,低优先 |
| `NeriPlayer-LTW` | 与视觉无关,跳过 |

## B. 色板与设计规范

### B1. 色板(冻结值,上线前用 material-theme-builder 复核)

```text
# —— Light ——
primary #0061A4   onPrimary #FFFFFF   primaryContainer #D1E4FF   onPrimaryContainer #001D36
secondary #535F70  secondaryContainer #D7E3F7  onSecondaryContainer #101C2B
tertiary #6B5778   tertiaryContainer #F2DAFF   onTertiaryContainer #251431
background/surface #F8F9FF   onBackground/onSurface #191C20
surfaceVariant #DFE2EB   onSurfaceVariant #43474E
outline #73777F   outlineVariant #C3C7CF   error #BA1A1A   errorContainer #FFDAD6

# —— Dark ——
primary #9ECAFF   onPrimary #003258   primaryContainer #00497D   onPrimaryContainer #D1E4FF
secondary #BBC7DB  secondaryContainer #3B4858  onSecondaryContainer #D7E3F7
tertiary #D7BEE4   tertiaryContainer #523F5F   onTertiaryContainer #F2DAFF
background/surface #111418   onBackground/onSurface #E1E2E8
surfaceVariant #43474E   onSurfaceVariant #C3C7CF
outline #8D9199   outlineVariant #43474E   error #FFB4AB   errorContainer #93000A

# surface 层级(Dark):lowest #0C0E12 / low #191C20 / container #1E2125 / high #282A2F / highest #33353A
# (Light 相应 #FFFFFF / #F3F4F9 / #EEF0F4 / #E8E8EE / #E2E2E9)
```

### B2. 设计规范摘要(源自 NeriApp.kt / NowPlayingScreen.kt / NeriMiniPlayer.kt)

- 骨架:左侧 sidebar + 主区(现有 Qt 结构已对齐 Android 四 Tab 语义)
- 圆角:卡片/面板 20dp;缩略图 14dp;输入框/小按钮 8dp;胶囊全圆
- 间距节奏:4 / 8 / 12 / 16 / 24;点击热区 ≥48dp
- 字号四级:18(页题)/ 15(歌名)/ 13(次行)/ 11(标签)
- Mini Player:高 64dp、顶角 20dp、内件 8dp → 直接套 player_bar
- 动效:主题切换 250ms 简化即可;玻璃模糊为平台特性,不做
- 图标基因:线性 Outlined 为主(播放/暂停圆底),24dp 单色随文字色

## C. 补缺方案

| # | 缺什么 | 来源/许可 | 落地 |
|---|---|---|---|
| 1 | Windows .ico | `icon/neriplayer.svg`(GPL-3.0 同源) | `uv run --with cairosvg,pillow python tools/make_ico.py`:svg2png 逐尺寸(16/32/48/64/128/256)→ Pillow 合成 .ico;16px 下字标会糊,小尺寸用 `ic_neriplayer.svg` 或 mascot 裁剪版,或直接用现成 1024 PNG 缩放 |
| 2 | 控制图标集 | 仓库自带 + Material Symbols Outlined 补缺(Apache 2.0,vendored 保留声明) | drawable XML→SVG 机械转换覆盖 15+;缺的(volume_off/home/settings/search/close/more_vert/queue_music 等)按名取 Material Symbols SVG;QIcon 原生吃 SVG,零依赖 |
| 3 | 托盘图标 | `ic_notification_small.png` 或 monochrome 版 | 32×32 深浅两版,不动画 |
| 4 | 窗口图标 | 同 #1 | `app.setWindowIcon(QIcon(...))` |
| 5 | 空状态 | 不引插画 | 居中淡色图标 + 一行 13px 文案 |
| 6 | 落地方式 | — | **直接文件路径,不用 .qrc**:资产放 `src/neriplayer_win/assets/`,路径经 `Path(__file__).parent` 解析;QSS 模板用 Python dict 色板 str.format 渲染,亮暗切换换 dict 重渲 |

## M4 执行序

#6 主题基建 → #2 控件图标(替换 emoji)→ #1+#4 应用图标 → #3 托盘 → #5 空状态;miuix 设计值/歌词 UI 推迟到设置页改版与歌词功能时再镜像 checkout 子模块。
