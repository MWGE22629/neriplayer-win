# M9 Windows 端流光动态背景调研

> 状态：进行中（边读边落盘）。本文件随时是当前已确认内容的完整快照。
> 目标文件：`docs/research/M9-win-ambient-background.md`

## 1. 需求与约束

### 1.1 需求口径

用户原话「neri win 的流光动态背景（手机版是这样的、电脑版只是动态纯色）」。拆开看：

- 目标：Windows 端要做出**完整流光动态背景**，视觉上对齐 Android 手机版，即多个彩色柔光斑缓慢流动、边界互相溶解。
- 括号是在描述现状差异，不是给目标封顶。桌面版当前只有「动态纯色」（封面取色后整屏换一个纯色底），这是现状基线。
- 因此本文按两级递进组织：一级是现状基线的打磨（动态纯色/渐变，兼作低配降级档），二级是完整流光，二级是重点。

### 1.2 对照实现（Android）

| 维度 | Android 实现 | 位置 |
| --- | --- | --- |
| 着色器 | AGSL `hyper_background_effect.glsl`，5 点高斯权重混合 | `reference/NeriPlayer-Android/app/src/main/assets/shaders/hyper_background_effect.glsl:1-117` |
| 5 点权重混合 | `weight = 1/(1 + dist²/r²*9.3)`，平方后加权平均，再除以 alpha | 同上 `:75-91` |
| 光斑位移 | 每点 `x += sin(t+y)*offset`、`y += cos(t+x)*offset`，offset 基础 0.10 | `reference/NeriPlayer-Android/app/src/main/java/moe/ouom/neriplayer/ui/view/BgEffectPainter.java:239-261` |
| 5 个初始点 | (0.52,0.46,r0.92) (0.14,0.32,0.74) (0.92,0.30,0.76) (0.26,0.88,0.80) (0.84,0.86,0.84) | 同上 `:60-66` |
| 色彩后处理 | HSV 饱和度/明度提拉、按纵向渐变压暗（`mix(0.68,1.10,smoothstep(0.10,0.92,vUv.y))`） | glsl `:92-103` |
| 抖动 | `gradientNoise` 去 banding，幅度 ±5/255 | glsl `:38-41,112-113` |
| 帧率 | 60fps 固定帧格（`1_000_000_000L / 60L`） | `ui/animation/AmbientFrameGrid.kt:8` |
| 音频联动 | `levelEase`/`beatEase` 平滑后驱动 zoom、点半径、位移 | `BgEffectPainter.java:217-226` |
| 换歌过渡 | 520ms，`smoothStep01` 插值颜色数组 + lightOffset/saturateOffset | `HyperBackground.kt:71,421-465` |
| 降级 | API < 33（Android 13）直接不画，留透明 | `HyperBackground.kt:156` |

### 1.3 桌面端约束

- 内核 libmpv 跑在主进程内，UI 线程零阻塞是既有铁律（`main_window.py` 的 `run_async` 全后台）。
- 验收线：冷启动 ≤ 1.5s，常驻 WorkingSet ≤ 200MB（`docs/PERF.md:3-5`）。当前发行版基线 744ms / 148MB 空闲，播放态约 220MB（`docs/PERF.md:11,81-83`）。这意味着背景功能**不能常驻大块内存**，也不能引入新的 DLL 加载链。
- 发行目录已 464MB，其中 WebEngine 195MB 是登录窗刚需（`docs/PERF.md:96-106`）。新增 Qt 模块要算进体积账。
- 依赖声明只有 `PySide6>=6.6`（`pyproject.toml:12-18`），实际测量环境 PySide6 6.11.2（`docs/PERF.md:92`）。版本下限 6.6 决定可用 API 边界。
- 用户开关与持久化走 `settings.json`，设置页按 QGroupBox 分组（`settings_page.py:199-297`）。

## 2. 现有主题系统现状

### 2.1 theme.py：冻结色板 + @role 模板 + 全局 QSS

结构（`src/neriplayer_win/ui/theme.py`）：

- 两套冻结 M3 色板 `_LIGHT` / `_DARK`，各 29 个角色键（`theme.py:32-88`）。除 M3 标准角色外还有五级 surface：`surfaceLowest/Low/Container/High/Highest`（`:53-58` 与 `:82-87`）。
- 单一全局 QSS 模板 `_QSS_TEMPLATE`（`:100-539`），用 `@role` 记号经 `re.sub` 替换（`build_qss`，`:544-552`）。选择器用 `str.format` 会与 QSS 花括号冲突，故走正则。
- 所有控件底色都直接绑死在角色上，没有透明度层：
  - `QWidget { background: @surface; }`（`:102-105`）是全局基底，这是流光背景的最大障碍：每个 QWidget 默认铺一层不透明 surface。
  - `QTableWidget` / `QHeaderView` / `QTableCornerButton` 全部 `@surface`（`:219-249`）。
  - `#playerBar { background: @surfaceContainer; }`（`:252-256`）。
  - `QGroupBox { background: @surfaceContainer; }`（`:454-462`）。
  - `QLineEdit` / `QComboBox` / `QMenu` / `QStatusBar` 也是实色（`:379-431,473-500`）。
  - 少数已经透明：`QTreeWidget` / `QListWidget`（`:156-161,192-197`）、`QLabel`（`:110-112`）、`QPushButton`（`:259-266`）、`QSlider`（`:348-351`）。
- 主题状态是模块级全局：`_current_name` / `_dynamic_palette` / `_applied_name`（`:559-565`），`current_palette()` 优先返回动态色板（`:572-575`）。
- `ThemeManager`（`:578-645`）：`apply(name)` 写全局样式表并 `theme_changed` 广播；`apply_dynamic(seed_argb)` 现算 TonalSpot 并总是重写 QSS（`:629-645`）。注意 `apply()` 在「主题名未变且已写过」时跳过 `setStyleSheet`（`:614-620`），而 `apply_dynamic()` 从不跳过。

关键点：**换一次主题 = 一次全进程 `setStyleSheet`**，会 re-polish 所有存活控件。切歌时 `apply_dynamic` 每首至少一次。流光背景如果每帧改 QSS 是不可行的，必须走独立绘制通道。

### 2.2 material_color.py：种子色到 TonalSpot 色板

- 纯 Python 的 material-color-utilities 移植，含 CAM16/HCT 全套（`material_color.py:37-520`）。
- 对外三个入口：`tonal_spot_palette(seed_argb, is_dark)`（`:635-656`）、`seed_from_hex(hex)`（`:659-667`）、`TonalPalette` 类（`:528-587`）。
- `tonal_spot_palette` 产出 primary hue 的 C36、secondary C16、tertiary hue+60° C24、neutral C6、neutralVariant C8，tone 表见 `_TONAL_SPOT_ROLES`（`:604-630`）。
- 性能特征：每个角色一次 `argb_from_hct`，内部是二分求解（`_solve_to_int`），且有 `TonalPalette._cache`。一次 `tonal_spot_palette` 约 29 次 tone 查询 + 6 个调色板的 keyColor 二分。这是**切歌时同步执行**的（`main_window.py:4075`），量级在毫秒级，不是背景方案要考虑的瓶颈。

流光需要的不是 29 个角色，而是 5 个「光斑色」。Android 用 androidx Palette 的 6 种 swatch 经 `softenPaletteColor` 映射到 5 个角色（Base/Accent/Light/Dark/Bridge，`HyperBackground.kt:467-514,541-610`）。桌面侧目前只有 `cover_seed.py` 的单一主色（5bit 量化直方图 + HSL 打分，`cover_seed.py:47-70`），**没有多色板**。这是流光路线的第一个改造点：要么扩 `cover_seed.py` 出 5 色，要么从 `tonal_spot_palette` 的 primary/tertiary/neutral 里派生 5 色（后者零成本且天然与 QSS 主题同源）。

### 2.3 main_window.py：apply_dynamic 调用链

调用链（`src/neriplayer_win/ui/main_window.py`）：

1. `_play_at` → `_request_dynamic_seed(song)`（`:3790`，定义在 `:4031-4047`）。
2. `_request_dynamic_seed`：无封面直接 `_revert_static_theme()`；缓存命中直接 `_apply_dynamic_seed(hex)`；否则交 `self._seed_loader.request(url)`（`:4037-4047`）。
3. `_on_seed_cover_ready`（`:4049-4066`）：校验 URL 与 generation，`run_async(extract_seed_hex)` 后台提取，回调里 `cover_seed.cache_put` 后 `_apply_dynamic_seed`。
4. `_apply_dynamic_seed`（`:4068-4075`）：`seed_from_hex` 去重，写 `self._dynamic_seed`，开关 `SETTING_DYNAMIC_COLOR` 为真时 `self.themes.apply_dynamic(seed)`。
5. `_revert_static_theme`（`:4077-4080`）：清种子，`themes.apply(name)` 回静态。
6. 明暗切换 `_on_settings_appearance`（`:4310-4321`）：先 `apply(value)`，若有种子再 `apply_dynamic` 重算。
7. 动态取色开关 `_on_settings_dynamic_color`（`:4323-4335`）。

`self.themes = ThemeManager()` 在 `:1593`，`theme_changed` 接 `_on_theme_changed`（`:1594`），初始 `apply` 在 `:1595`。

这个链路的形状对流光很友好：**已经有一个「当前生效种子」的单一真值点**（`self._dynamic_seed`）和明确的切歌时机（`_apply_dynamic_seed`）。流光只要在这里追加一次「设置目标色组」的调用，加自己的 520ms 插值即可，不需要动网络或解析层。

窗口结构（决定背景层挂哪）：

- `MainWindow.__init__`：`resize(1080,680)`（`:1316`），`setCentralWidget(self._splitter)`（`:1619`）。
- 层级：`QSplitter` → `[sidebar(_SidebarTree), body]`；`body` → `QHBoxLayout` → `[root(QVBoxLayout: central_stack, player_bar)]`（`:1597-1619`）。
- `central_stack` 是 `_SlideStack`（`QStackedWidget` 子类，`:628`，实例化 `:1535`），三页：登录 / 表格 / 设置（`:1536-1538`）。
- `player_bar` 是 `PlayerBar`，`setObjectName("playerBar")`（`:1578`），QSS 给它 `surfaceContainer` + 顶角 20 圆角。
- 窗口隐藏/托盘：`closeEvent` 里 `event.ignore(); self.hide()`（`:4620-4635`），关闭行为由 `close_action` 决定（默认 tray）。

挂载点结论：背景层最自然的落点是 `body` 的底层（`QWidget` 自绘或子控件下沉），或者在 `MainWindow` 上装一个覆盖中央区域的兄弟控件。`QStackedWidget` 每页都要透明，否则翻页就露出 QSS 的 `QWidget{background:@surface}`。

### 2.4 player_bar.py / settings_page.py 结构与可改造点

`player_bar.py`（664 行）：

- `PlayerBar(QWidget)`（`:407`），两行布局：进度行 + 信息/控制行（`:408-413`）。
- 自绘子控件若干：`ElidedLabel`（`:49`）、`SeekSlider`（`:97`）、`AnimatedIconButton`（`:147`，`paintEvent` 在 `:191`）、`_CoverLabel`（`:223`，`paintEvent` 在 `:290`，切歌交叉淡化）、`_SwipeHost`（`:323`）。
- `PlayerBar` 本身**没有** `paintEvent`，完全靠 QSS `#playerBar` 画底（`theme.py:252-256`）。
- 改造点：若要做「播放条半透明玻璃」，只需把 `#playerBar` 的 `background` 改成 `rgba(...)`，并在 `PlayerBar` 上加 `setAttribute(Qt.WA_StyledBackground)` 已由 QSS 满足；无需自绘。但播放条是紧贴窗口底边的浮层卡片，半透明后底下必须是背景层而非 QSS 的 surface，需要 `body` 与 `root` 都透明。

`settings_page.py`（426 行）：

- 分组顺序：关闭行为 / 播放 / 音质 / 外观 / 语言 / 关于（`settings_page.py:199-288`）。
- 外观组当前只有两个控件：主题下拉 `appearance_combo`（`:240-247`）与动态取色勾选框 `dynamic_color_check`（`:249-252`）。
- 信号 `dynamic_color_changed = Signal(bool)`（`:194`），由 `MainWindow._on_settings_dynamic_color` 接（`main_window.py:4323`）。
- 回填 `set_dynamic_color`（`:423-426`）用 `blockSignals` 防回环；文案在 `retranslate`（`:337-338`）。
- 改造点：流光开关最自然的落点是外观组内新增一个 QCheckBox（或三档下拉：关闭 / 纯色 / 流光），与 `dynamic_color_check` 同排。新增 `ambient_effect_changed` 信号，`MainWindow` 侧持久化到 `settings.json` 并调用背景控制器。文案键加进 `i18n`。

### 2.5 依赖与打包现状

- `pyproject.toml:12-18`：`PySide6>=6.6`、`python-mpv>=1.0.7`、`httpx`、`pycryptodome`、`segno`。无 OpenGL 相关显式依赖，无 qfluentwidgets 等 UI 库。
- 构建：Nuitka standalone + LTO，`tools/build_exe.py`，`prune_dist` 已裁 Qt 翻译/Chromium locale/qt6pdf（`docs/PERF.md:108-111`）。
- 版本下限 6.6 意味着 `QRhiWidget`（Qt 6.7 引入）**不能作为唯一实现**，要么抬下限，要么做运行时能力探测 + 回退。
- 打包影响：Nuitka 对 PySide6 的模块收集按 import 图走。引入 `QtQuick`/`QtQml` 会把 `Qt6Quick.dll`、`Qt6Qml.dll`、`Qt6QuickEffects.dll` 及 QML 插件目录带进发行包，粗估 15-30MB 级（详见 §3.4）。
- 现成 libmpv 已在进程内，`mpv-2.dll` 115MB（`docs/PERF.md:101`），任何新增图形上下文都要和它共处同一进程（OpenGL 上下文与 libmpv 的 `gpu` 渲染后端理论上可共存，但 mpv 默认在独立渲染上下文里，见 §6 风险）。

## 3. 七条路线逐条评估

本节结论来自本机实测（PySide6 6.11.2 / Python 3.12.13，仓库 `.venv`）与原型窗口渲染，实测方法见 §3.8 末尾。

### 3.1 QWidget + QGraphicsEffect

可行性：低。`QGraphicsBlurEffect` 只能模糊控件已绘制的像素，不能生成颜色；而流光的本质是「多色场随时间变化」，模糊只是其中一环。要做出流光仍需下层先画出彩色场，等于给 3.2 加一层昂贵的事后模糊。`QGraphicsEffect` 在 `QGraphicsView` 外的普通窗口上还会强制走离屏渲染，中大型控件（整窗）每帧一次全窗离屏拷贝。

性能代价：一次全窗模糊在 1080x680 上量级为 5-15ms，且发生在 GUI 线程。

判断：不采用。它解决的是「毛玻璃」，不是「流光」。

### 3.2 QWidget 自绘 + QRadialGradient + QTimer

可行性：高，且是本机实测唯一在无新增依赖前提下达到目标的路线。

实测（1200x700、5 个 `QRadialGradient`、120 帧、`QImage` 离屏）：

| 画法 | 中位耗时 | 95 分位 | 60fps 单核占用 |
| --- | --- | --- | --- |
| 全分辨率直接画 | 1.71 ms | 2.24 ms | 约 10.2% |
| 320x180 画 + 平滑放大 | 1.09 ms | 1.36 ms | 约 6.5% |
| 160x90 画 + 平滑放大 | 1.11 ms | 1.38 ms | 约 6.7% |

三点结论：

1. 五层全屏渐变在 CPU 上足够快，50fps 下不到单核 10%。
2. 降分辨率几乎无收益（160x90 与 320x180 持平），瓶颈是「全屏 `fillRect` 五次」的填充率而非单像素运算。因此**不要做多级分辨率降级**，一套全分辨率画法即可。
3. 上表是离屏 `QImage` 的纯绘制耗时，不含窗口合成与上屏。实测整窗可见时另有约 1-2ms 的刷新开销，仍在预算内。

反面实测（说明为什么不能「精确复刻」shader）：用纯 Python 逐像素实现 5 点权重混合，仅 160x90 网格每帧就要 31.6ms，换算 60fps 需 189% 单核。原因是每像素 5 次权重计算全部落在 Python 层。**在未安装 numpy 的前提下，逐像素精确复刻不可行**，必须把混合交给 QPainter 的 C++ 渐变实现。

画面质量关键参数（原型迭代得出，见 §4.2）。
判断：采用，作为主方案。

### 3.3 QOpenGLWidget / QRhiWidget + GLSL

可行性：技术上可行，工程上不划算。

本机能力实测：

- `PySide6.QtWidgets.QRhiWidget` 存在；`PySide6.QtGui` 提供 44 个 `QRhi*` 类，含 `QRhiGraphicsPipeline`、`QRhiShaderResourceBindings`、`QRhiBuffer`、`QRhiShaderStage`，`QRhiWidget.rhi()` 可取到实例。
- `QOpenGLWidget` 在 `PySide6.QtOpenGLWidgets`，不在 `QtWidgets`。
- 但 `pyproject.toml` 的下限是 `PySide6>=6.6`，而 `QRhiWidget` 是 Qt 6.7 才引入的。作为唯一实现必须抬下限，否则要做运行时探测加回退，两条路都要维护两套绘制代码。

工程阻力：

1. 原生子窗口合成。`QOpenGLWidget` 在 Windows 上走独立交换链，与 QSS 绘制的兄弟控件叠加时会出现 Z 序、透明度、圆角裁切问题。本项目大量控件依赖 QSS 圆角与半透明（`theme.py` 整套 `@surface` 角色），叠加 OpenGL 层会把这些约定打破。
2. 与 libmpv 共处。进程内已有 `mpv-2.dll`（115MB，`docs/PERF.md:101`），其 `gpu` 后端自带渲染上下文。再加一个 OpenGL 上下文，在部分显卡驱动上会互相干扰（上下文丢失、垂直同步争用）。
3. 收益不对等。3.2 已经能在 CPU 上跑到 1.7ms/帧，GLSL 的价值要到「每像素 5 次以上混合 + 高分辨率 + 60fps 常开」才显现，而本需求只有 5 个光斑。

判断：不作为首选。若后续要做「真实音频频谱 + 多光斑 + 全屏后处理」再评估，届时抬 `QRhiWidget` 下限。

### 3.4 Qt Quick + ShaderEffect

可行性：可行，代价在体积。

`QQuickWidget` 在 PySide6 中可用（实测导入成功）。用 `ShaderEffect` 可以把 Android 的 glsl 几乎原样搬过来，画面最接近手机端。

代价：

1. Nuitka 打包会带入 `Qt6Quick.dll`、`Qt6Qml.dll`、`Qt6QuickEffects.dll` 及 QML 插件目录，粗估 15-30MB，发行包已 464MB（`docs/PERF.md:96-106`）。
2. `QQuickWidget` 同样是独立合成层，与 QSS 兄弟控件叠加的 Z 序问题同 3.3。
3. 引入一整套 QML 运行时，只为一个背景，与「轻量是硬指标」冲突。
4. 冷启动会多一次 QML 引擎初始化，直接冲撞 1.5s 验收线。

判断：不采用。除非将来整个界面迁到 QML，否则为一个背景引 QML 不划算。

### 3.5 半透明分层

这不是独立的渲染路线，而是 3.2 的**必要配套**，且是原型里最容易被低估的部分。

现状障碍：`theme.py` 的全局 QSS 里 `QWidget { background: @surface; }`（`theme.py:102-105`）给每个控件铺不透明底色，`QTableWidget`/`QHeaderView`/`QGroupBox`/`#playerBar` 也都是实色（`theme.py:219-256,454-462`）。背景层画得再好，只要上层控件是不透明的，就什么都看不见。

原型实测出的三条硬结论：

1. 表格必须是**接近实色**的半透明（原型用 `rgba(16,18,24,232)`，即 91% 不透明），不能是 40-60% 的浅透。浅透时背景亮带会在表格边缘被「切断」，形成肉眼可见的明暗接缝，且表格空白区透出背景后呈「上深下浅的灰膜」，观感脏。
2. 按钮必须是实色（原型用 `rgba(44,48,58,245)`）。半透明灰底压在渐变过渡区上会变成一块既不属于背景也不属于前景的「灰补丁」，边框发虚。
3. 全局压暗遮罩必须**轻**。原型最初用 alpha 165/60/85/185 的纵向遮罩，结果光斑被抹成均匀雾气，完全失去流光感。改成只在上下两端轻压（105/20/25/120）后光斑才立起来。原因是表格与按钮本身已有近实色底，不再需要全局遮罩兜底对比度。

判断：必须做，且只对「大面积容器」放开透明度，对文字承载控件保实色。

### 3.6 Windows DWM Mica / Acrylic

可行性：`DwmSetWindowAttribute` 经 ctypes 可调，`DWMWA_SYSTEMBACKDROP_TYPE`（值 38）在 Win11 22H2+ 生效，可设 Mica / Acrylic / Tabbed。

限制：

1. 系统版本门槛。Win10 完全不支持 `SYSTEMBACKDROP_TYPE`；Win11 21H2 只支持旧式 `DWMWA_MICA_EFFECT`（值 1029，未文档化）；22H2+ 才有稳定接口。README 声明支持 Windows 10+，等于一半目标机器不可用。
2. 语义不符。Mica 是「取桌面壁纸色调做窗口底」，Acrylic 是「模糊窗口后方内容」。两者都与「随当前歌曲封面变化的多色流光」无关，也完全不能跟着换歌变色。
3. 需要窗口透明链路（`WA_TranslucentBackground` 或无边框窗口），会与现有 QSS 圆角、`QSplitter` 布局、任务栏缩略图按钮等既有代码纠缠。

判断：可作为「窗口底色质感」的额外加分项，但不能承担流光需求。不纳入本次范围。

### 3.7 现成轻量库

可选项是 `qfluentwidgets` 的 `AcrylicWindow`、`BlurWindow` 等。评估结论一致：

1. 这些库解决的是系统级模糊/亚克力，不是彩色流光，功能不匹配。
2. `qfluentwidgets` 自带一整套控件替换与主题系统，与本项目已有的 `theme.py` M3 色板 + 全局 QSS 直接冲突，引入等于重构 UI 层。
3. 新增依赖会进 Nuitka 打包，与体积/启动验收线冲突。

判断：不引入。流光用 30-60 行自绘代码即可实现，没有必须外部库的理由。

### 3.8 对比表

| 路线 | 能否做出流光 | 本机实测耗时 | 新增依赖/体积 | 与现有 QSS 冲突 | 版本门槛 | 判断 |
| --- | --- | --- | --- | --- | --- | --- |
| 3.1 QGraphicsEffect | 否（只能模糊） | 全窗 5-15ms | 无 | 中（离屏合成） | 无 | 不采用 |
| 3.2 自绘 QRadialGradient | 是 | 1.71ms/帧，60fps 单核 10.2% | 无 | 需配套 3.5 | 无 | **主方案** |
| 3.3 QOpenGL/QRhiWidget | 是 | 未测（GPU） | 无新增 DLL | 高（Z 序/透明） | Qt 6.7 | 暂不采用 |
| 3.4 QtQuick ShaderEffect | 是（最接近手机） | 未测 | +15-30MB | 高（独立合成层） | 无 | 不采用 |
| 3.5 半透明分层 | 配套 | 近零 | 无 | 本身就是改造 | 无 | **必须做** |
| 3.6 DWM Mica/Acrylic | 否（非彩色流光） | 系统实现 | 无 | 高（透明链路） | Win11 22H2+ | 不纳入 |
| 3.7 现成库 | 部分（仅模糊） | 未知 | 大 | 高（整套主题） | 视库而定 | 不引入 |

实测方法：`QGuiApplication` 下用 `QImage(1200,700, ARGB32_Premultiplied)` + `QPainter` 逐帧绘制 5 个 `QRadialGradient` 全屏 `fillRect`，跑 120 帧取中位数与 95 分位；逐像素对照实验用 160x90 网格纯 Python 循环实现 shader 权重公式。

## 4. 推荐方案

### 4.0 两条口径（用户确认）

- 流光**一直流动**，不随播放状态启停。与 Android 一致：`BgEffectPainter.java:240` 的常驻漂移项 `uPointOffset = 0.10` 即使在 level/beat 为 0 时也生效，所以 Android 安静时也在缓慢流动。
- 「播放时长」统计口径与此无关，属于另一个功能，见 `M9-win-playback-stats.md`。

### 4.1 第一级：动态纯色/渐变（降级档）

现状基线，也是低配机器与用户手动关闭流光后的状态。

实现要点：沿用现有 `_apply_dynamic_seed`（`main_window.py:4068-4075`）产出的单一主色，背景层画一个双色标线性渐变（主色 + 相邻明度），色相随时间极缓慢漂移。开销可忽略（一次 `fillRect` + 一个 `QLinearGradient`，远低于 1ms）。

这一级不需要新增任何依赖，也不改 QSS 结构，可作为第一个可交付版本。

### 4.2 第二级：完整流光（目标）

结构：一个 `QWidget` 子类（记作 `FlowBackground`）挂在 `body` 底层，五个 `QRadialGradient` 逐帧全屏叠加，配一层噪声抖动与上下轻压遮罩。

已验证的关键参数（原型迭代得出，直接照抄）：

| 参数 | 值 | 说明 |
| --- | --- | --- |
| 五个光斑基准位置与半径 | (0.52,0.46,0.92) (0.14,0.32,0.74) (0.92,0.30,0.76) (0.26,0.88,0.80) (0.84,0.86,0.84) | 原样取自 `BgEffectPainter.java:60-66` |
| 光斑半径换算 | `radius * max(w,h) * 0.42` | 0.42 是关键；用 0.62 时光斑摊成雾，失去光核 |
| 权重曲线取样 | 色标按 `w = 1/(1+9.3*(d/r)^2)^2` 取 13 个停止点，alpha = `min(1, w*0.95)` | **最关键的一条**。若用线性缓降色标，光斑会糊成一片均匀雾气 |
| 漂移振幅 | 0.14（Android 原值 0.10） | 0.10 时 1.5 秒仅移动约 30px，静止看几乎察觉不到；0.14 位移可感知且不躁 |
| 漂移频率 | `x += offset*sin(t+y)`、`y += offset*cos(t+x)`，t 每秒 +1 | 周期约 6.3 秒，与原实现一致 |
| 叠加方式 | `QPainter.CompositionMode_SourceOver` | 相加（`Plus`）在中部与两侧大面积过曝削顶，色彩细节丢失，只在深色主题且 alpha < 0.25 时可考虑 |
| 抖动 | 预生成 128x128 灰度噪声 `QPixmap`，`setOpacity(0.05)` 平铺 | 不打散会有可见色带；图只生成一次，逐帧复用 |
| 上下遮罩 | 纵向 `QLinearGradient`，alpha 105/20/25/120（0/0.22/0.80/1.0 处） | 只压标题区与底栏；全局重压会抹平光斑 |
| 帧率 | 50fps（`QTimer` 20ms） | 60fps 也可，实测留有余量；50fps 更省电且人眼对缓慢漂移不敏感 |

权重曲线数值（说明 0.42 半径为何配得上陡衰减）：

| d/r | 0.1 | 0.2 | 0.316 | 0.5 | 0.707 | 1.0 |
| --- | --- | --- | --- | --- | --- | --- |
| 权重 w | 0.837 | 0.531 | 0.269 | 0.091 | 0.031 | 0.009 |

即光斑在半径一半处只剩 9% 强度，是「小亮核 + 快速衰减」，这也是画面能立起来的原因。

停表与不可见处理：`hideEvent` 停 `QTimer`，`showEvent` 重启；窗口最小化到托盘时窗口 `hide()`（`main_window.py:4620-4635`），事件链自然覆盖。这样后台常驻时不占 CPU。

### 4.3 与现有主题共存

按 3.5 的三条结论落地：

- `QWidget { background: @surface; }`（`theme.py:102-105`）不能全局改成透明，否则所有控件失去底色。做法是给 `body` 与 `central_stack` 的每一页显式设 `background: transparent`，其余控件维持原样。
- 表格、分组框、播放条这类大容器改成高不透明度 rgba（91%-95%），视觉上仍能透出背景，但不产生接缝。
- 按钮、输入框、下拉框保持实色。
- 文字颜色不动，靠遮罩 + 容器实底保证对比度。

### 4.4 换歌颜色平滑过渡

对齐 Android 的 520ms + smoothStep（`HyperBackground.kt:71,421-465`）：`_apply_dynamic_seed` 之后向背景层投递一组新光斑色，背景层在 520ms 内逐帧 `lerp` 五组 RGB，`ease = t*t*(3-2t)`。只需插值颜色，不插值位置（位置本来就连续）。

### 4.5 五色从哪来

现有 `cover_seed.py` 只出单一主色（`cover_seed.py:47-70`），没有多色板。两条路：

1. 从 `tonal_spot_palette(seed)` 派生：取 primary、tertiary、primary 的高明度与低明度变体、neutralVariant，凑五色。零成本，且与 QSS 主题天然同源，换歌时背景与界面色调一致。
2. 扩 `cover_seed.py` 出多色直方图聚类：更接近 Android 的 Palette 行为，但要多写聚类代码，且结果与 QSS 主题脱钩（背景可能出现界面里没有的颜色）。

推荐先走 1，视觉一致性更好、代码更少；若观感不够丰富再评估 2。

## 5. 落点与改造清单

| 项 | 位置 | 改动 |
| --- | --- | --- |
| 背景层类 | 新建 `src/neriplayer_win/ui/flow_background.py` | `FlowBackground(QWidget)`，含绘制、插值、停表 |
| 挂载 | `main_window.py:1597-1619` 的 `body`/`root` 组装处 | 背景层作为 `body` 的底层子控件，`resizeEvent` 同步 geometry；`root` 与 `central_stack` 置透明 |
| 色组下发 | `main_window.py:4068-4075` `_apply_dynamic_seed` | 追加一行调用，把五色交给背景层并触发 520ms 插值 |
| 主题共存 | `theme.py:100-539` | 新增透明化选择器（只针对 `body`/页面容器），表格与分组框背景改高不透明度 rgba |
| 设置开关 | `settings_page.py:240-252` 外观组 | 新增三档下拉：关闭 / 动态纯色 / 流光；信号 `ambient_effect_changed` |
| 持久化 | `data/store.py` `default_settings()` | 新增 `SETTING_AMBIENT_EFFECT`，默认 `solid` |
| 文案 | `i18n.py` | 三档中英文案 + 设置项标签 |
| 停表 | `FlowBackground` | `hideEvent`/`showEvent` 控制 `QTimer` |

## 6. 风险与验收

风险：

1. **对比度**。半透明容器叠在亮光斑上会降低文字对比度。原型里表格用 91% 不透明后文字清晰，但浅色主题下需要单独调（亮底 + 亮光斑更易糊）。验收时必须两个主题各看一遍。
2. **色带**。8bit 径向渐变在大面积缓变上必然出现阶梯，抖动是必需品不是可选项。实测噪声不透明度低于 0.03 时压不住，高于 0.08 会显脏。
3. **QSS 透明化的连带影响**。改动 `QWidget { background }` 会影响全部控件，必须只针对容器选择器改，改完需全界面走查（登录页、表格页、设置页、队列窗口、对话框）。
4. **打包体积**。主方案零新增依赖，但若最终选了 3.3/3.4，需重新核算 464MB 基线与 15-30MB 增量。
5. **托盘后台**。停表依赖 `hideEvent`，若用户用「关闭到托盘」路径隐藏窗口，需确认事件确实送达（`main_window.py:4620-4635` 走 `self.hide()`，应可覆盖）。

验收：

- 冷启动增量 ≤ 100ms（背景层首次绘制不计入，因其在首帧后才启动）。
- 常驻内存增量 ≤ 10MB（一张 128x128 噪声图 + 若干渐变对象，量级远低于此）。
- 播放态 CPU 增量：50fps 下 ≤ 单核 12%（实测绘制 10.2%，留 2% 余量）。
- 托盘隐藏后 CPU 回落至基线（停表生效）。
- 亮/暗两个主题下，表格正文、表头、按钮文字在任意光斑位置均可读。
- 切换歌曲时背景颜色在约 0.5 秒内平滑过渡，无跳变。

## 调研进度

- [x] 读 pyproject.toml（依赖与版本）
- [x] 读 theme.py 全文
- [x] 读 material_color.py
- [x] 读 player_bar.py
- [x] grep main_window.py 的 apply_dynamic / seed / material
- [x] 读 settings_page.py 分组结构
- [x] 读 Android shader 与 HyperBackground.kt / BgEffectPainter.java
- [x] 本机实测 PySide6 模块可用性（QRhiWidget / QOpenGLWidget / QQuickWidget）
- [x] CPU 绘制开销实测（三种分辨率）
- [x] 逐像素纯 Python 对照实测（证明不可行）
- [x] 原型窗口渲染 + 视觉评估三轮迭代（光核、色带、接缝、按钮）
- [ ] 联网核实 Qt6 Windows Mica 的 Win10/11 差异（未做，§3.6 结论基于接口门槛，不影响主方案）
- [ ] 暗色/亮色两主题下的对比度实测（原型只测了暗色）
