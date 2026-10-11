# 性能验收

验收线:**冷启动 ≤ 1.5s / 常驻 WorkingSet ≤ 200MB**(2026-10-11 由 150MB 放宽:
v0.5.0 起封面缩略图、v0.7.0 起动态取色与双语、v0.8.0 起路由守卫等特性累积把
空闲常驻推到 148-157MB,播放态约 220MB;150MB 线已无法容纳合理的新特性)。

## v0.8.1 复测(2026-10-11,启动性能修复)

| 目标 | 冷启动中位(5 次) | 常驻 WorkingSet(空闲 20s) | 私有提交 | 验收线 | 判定 |
| --- | --- | --- | --- | --- | --- |
| 发行 exe | **744 ms** | **148 MB** | 90 MB | ≤1.5s / ≤200MB | **PASS** |
| dev 对照 | 836 ms | 128 MB | 86 MB | (对照) | — |

修复前(v0.8.0 同机同日测量):发行 exe 冷启动 **979 ms** / 常驻 **157 MB**;
dev 1113 ms / 136 MB。真实使用(非热缓存)启动日志实测 2.3-2.9s。

### 根因与修复(两处,合计约 -240ms,内存 -9MB)

1. **`platform.platform()` 拖慢每次启动 300-500ms**
   (log.py 启动头用它记 OS 版本;Python 3.12 起该函数在 Windows 走
   `_wmi` 查询 Win32_ComputerSystem 等类,本机实测单次 491ms)。
   修复:换 `sys.getwindowsversion()`(RtlGetVersion 薄包装,微秒级),
   输出 `Windows-10.0.26100`,语义不减。
2. **qt6webenginecore.dll(195MB)每次启动都被加载**
   (`main_window → login_page → browser_login → PySide6.QtWebEngineCore`
   模块级 import,`api.netease.__init__ → yd` 同样)。README 承诺的
   「登录弹窗才加载 WebEngine」此前只对子进程成立,DLL 本体一直在
   启动链上;热缓存下 ~20-50ms,真冷启动(开机后首开)还要从磁盘
   读 195MB,是真实使用 2-2.9s 的主要成分。
   修复:import 全部下沉到 `BrowserLoginDialog.__init__` 与拦截器工厂;
   `YdTokenFetcher` 改 PEP 562 惰性导出(`from ... import` 仍可用)。
   发行包仍带 WebEngine DLL(登录功能需要),只是不再随启动加载。

定位方法:`tools/profile_startup.py` 按 MainWindow 构造顺序逐项计时
(platform.platform 项 438ms 一眼可见),配合 `python -X importtime`
看 import 树里的 QtWebEngineCore。

### 顺带发现

- measure_perf.py 按窗口标题清残留实例,**播放中标题是歌名**(任务栏
  歌名特性),会漏杀正在播的实例;构建发行包前若遇 mpv-2.dll
  PermissionError,先 taskkill 干净。
- dev 常驻 128MB < 发行 148MB:Nuitka 常量段映射 + 静态链接运行时,
  与历史版本同规律。

## 历史版本测量(同口径摘要)

| 版本 | 日期 | 发行冷启动 | 发行常驻 | 备注 |
| --- | --- | --- | --- | --- |
| 0.1.0 | 09-14 | 530 ms | 141 MB | 首测,基线 |
| 0.2.0 | 09-15 | 555 ms | 140 MB | 字形回退陷阱 +50MB 已修(见下) |
| 0.3.0 | 09-15 | 555 ms | 142 MB | 收藏歌单标题字形净化(textsafe.py) |
| 0.4.0/0.4.1 | 09-17 | 545-571 ms | 143-146 MB | 最近列表/右键插播/每日推荐;高分屏图标 2x 底图 +3MB |
| 0.7.2 | 09-26 | 804 ms | 143 MB | 任务栏工具栏+五组动效(冷启动较 0.4.x 漂移 +260ms,当时未深究;0.8.1 复盘确认主因是 platform.platform 随 v0.6.3 日志特性引入,WebEngine import 链则自 v0.1.0 就在) |
| 0.8.0 | 10-11 | 979 ms | 157 MB | 路由守卫+封面 LRU;常驻破 150MB 线,验收线放宽到 200MB |
| 0.8.1 | 10-11 | 744 ms | 148 MB | 启动性能修复(见上) |

## 重要经验(长期有效)

### 字形回退陷阱(0.2.0/0.3.0 两度中招)

UI 文本出现主字体(微软雅黑)不覆盖的字符时,DirectWrite 会整套加载
回退字体并映射进进程:**侧栏两个几何符号「▾/▸」就带来 +45~50MB 常驻**;
0.3.0 又被第三方歌单标题里的 ❤/٩/๑/♪ 动态文案重演(+45~67MB)。

- 修复与防线:指示符号一律 QPainter 画图标不走过字体系统;动态文案经
  `ui/textsafe.py`(NFKC 归一 + GDI GetGlyphIndices 探测,未覆盖码点
  静默省略);`test_sidebar_text_has_no_exotic_glyph` 拦截静态文案。
- 结论:UI 文本只用主字体与 CJK 覆盖的字符。

### 测量口径(复测必读)

- **冷启动** = CreateProcess → 主窗口可见(EnumWindows 精确匹配标题
  "NeriPlayer Win"),每轮强杀进程树间歇 1s,取 5 次中位;这是「OS
  文件缓存已热」口径。真·首次冷启(开机后第一次)受磁盘支配,本应用
  历史上被 195MB 的 qt6webenginecore.dll 拖累,0.8.1 懒加载后才不受
  它影响。
- **电源模式显著影响冷启动**:电池模式约为交流电 2 倍;验收测量必须
  接交流电。
- **常驻内存** = 窗口可见后空闲 20s,WorkingSet/私有提交连采 3 次取
  中位;libmpv 跑在主进程内已包含。登录态会因 CJK 字形加载 +10MB 级;
  播放态(封面/取色/mpv 缓冲)约 220MB,不属验收口径但注意别误读
  任务管理器。
- WebEngine 子进程仅登录窗打开期间存在(一次性,关窗即释放);
  0.8.1 起 DLL 本体也只在登录窗期间加载。
- 150MB 量级 WorkingSet 的大头是 libmpv(shinchiro 全功能构建)与 Qt;
  未来要压内存可换精简构建 mpv-2.dll,预计可省 20-40MB。

## 测量环境

- Windows 11(10.0.26100),AMD Ryzen x16,28 GB 内存,交流电
- Python 3.12.**,PySide6 6.11.2,Nuitka(standalone + LTO)
- 工具:`uv run python tools/measure_perf.py --target both --runs 5 --idle 20`
  阶段剖析:`uv run python tools/profile_startup.py`

## 发行目录体积(0.8.1 约 464 MB / 159 文件)

| 体积 | 内容 |
| --- | --- |
| 195 MB | qt6webenginecore.dll(Chromium,登录窗所需,不可省;0.8.1 起不随启动加载) |
| 115 MB | bin/mpv-2.dll(shinchiro 全功能 libmpv) |
| 31 MB | icudt73.dll(WebEngine 的 ICU) |
| 25 MB | NeriPlayerWin.exe(全部业务代码 + 依赖编译为原生码) |
| 21 MB | PySide6 运行时(plugins/pyd/翻译;翻译已裁到 zh_CN) |
| 3 MB | qtwebengine_resources*.pak + v8_context_snapshot.bin |
| 其余 | icudtl.dat、python312.dll、Qt6 其余 DLL、certifi 等 |

已做的瘦身(-154.8 MB,见 `tools/build_exe.py: prune_dist`):删
devtools/调试 pak;Qt 翻译仅 zh_CN;Chromium locale 仅 zh-CN + en-US;
删 qt6pdf.dll。启动优化:单目录 standalone(无 onefile 自解压)、
`--lto=yes`、`--python-flag=no_site`、0.8.1 的 WebEngine 懒加载。

## 复测方法

```bash
uv run python tools/build_exe.py                        # 重建(增量约 1-2 分钟)
uv run python tools/measure_perf.py --target both       # 复测,输出汇总与判定
```
