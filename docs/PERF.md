# 性能验收(M4:打包与性能)

## v0.2.0 复测(2026-09-15)

| 目标 | 冷启动中位(3 次) | 常驻 WorkingSet(空闲 20s) | 私有提交 | 验收线 | 判定 |
| --- | --- | --- | --- | --- | --- |
| 发行 exe | **555 ms** | **140 MB** | 83 MB | ≤1.5s / ≤150MB | **PASS** |
| dev 对照 | 704 ms | 118 MB | 77 MB | (对照) | — |

与 0.1.0 基线持平。本次曾出现 **+50MB WorkingSet 回归**,根因与修复见
下文「字形回退陷阱」,已加防回归测试。

### 字形回退陷阱(重要经验)

侧栏分区头的折叠指示曾用文字符号「▾/▸」(U+25BE/U+25B8)。这两个
几何字形不在主字体(及 CJK 回退链)覆盖内,会触发 DirectWrite 额外
加载整套回退字体并映射进进程——隔离测试:**仅两个字符就带来约
+45~50MB 常驻 WorkingSet**(纯中文文本对照组仅 +18MB,属正常 CJK
字体回退)。

- 修复:折叠三角改为 QPainter 画进图标(与品牌标合成 30x18 画布),
  不经过字体系统;
- 防回归:`tests/test_main_window_layout.py::test_sidebar_text_has_no_exotic_glyph`
  拦截侧栏文本中的低频区字符(几何形状/箭头/杂项符号区)。

结论:UI 文本一律只用主字体与 CJK 覆盖的字符;指示符号画图标。

## v0.1.0 首测(2026-09-14)

测量日期:2026-09-14。结论先行:**两项指标均达标**。

| 目标 | 冷启动中位(5 次) | 常驻 WorkingSet(空闲 20s) | 私有提交 | 验收线 | 判定 |
| --- | --- | --- | --- | --- | --- |
| 发行 exe(`dist/neriplayer-win/NeriPlayerWin.exe`) | **530 ms** | **141 MB** | 83 MB | ≤1.5s / ≤150MB | **PASS** |
| dev 对照(`uv run python -m neriplayer_win`) | **589 ms** | **117 MB** | 77 MB | (对照) | — |

发行版启动比 dev 还快约 60ms:Nuitka 编译后无解释器逐模块 import 的开销;
exe 常驻内存略高于 dev(编译版常量段整体映射进工作集 + 静态链接的运行时),
量级不变。

## 测量环境

- Windows 11(10.0.26100),AMD Ryzen x16,28 GB 内存
- Python 3.12.**,PySide6 6.11.2,Nuitka 4.2.1(standalone + LTO)
- 工具:`uv run python tools/measure_perf.py --target both --runs 5 --idle 20`

## 口径与注意事项

- **冷启动** = CreateProcess → 主窗口可见(`EnumWindows` 精确匹配标题
  "NeriPlayer Win"),每轮结束强杀进程树并间歇 1s,取 5 次中位。
  这是「OS 文件缓存已热」的口径——等价于日常点图标启动;真·首次冷启
  (开机后第一次)会因磁盘读取略慢,但单目录发行没有 onefile 的自解压
  一次性惩罚,量级不变。
- **常驻内存** = 主进程 WorkingSet / 私有提交,窗口可见后空闲 20s,
  连续采样 3 次取中位。libmpv 以 DLL 形式跑在主进程内,**已包含**在数字里。
- **WebEngine 按需加载是现状**:登录弹窗才会创建 `QWebEngineView`,常态
  没有 `QtWebEngineProcess` 子进程,本测量反映"不开登录窗"的常态。
  打开登录窗后 WebEngine 子进程会额外占内存(一次性,关窗即释放)。
- exe 以发行目录为 cwd 启动(等价双击);dev 从仓库根启动(含 uv 与
  解释器的启动开销,作为对照参考)。
- 151MB 级别的 WorkingSet 中大头是 libmpv(shinchiro 全功能构建)与
  Qt;若未来要压内存,可换精简构建的 mpv-2.dll,预计可省 20-40MB。

## 发行目录体积(瘦身后 464 MB / 159 文件)

| 体积 | 内容 |
| --- | --- |
| 195 MB | qt6webenginecore.dll(Chromium,登录窗所需,不可省) |
| 115 MB | bin/mpv-2.dll(shinchiro 全功能 libmpv) |
| 31 MB | icudt73.dll(WebEngine 的 ICU) |
| 25 MB | NeriPlayerWin.exe(全部业务代码 + 依赖编译为原生码) |
| 21 MB | PySide6 运行时(plugins/pyd/翻译;翻译已裁到 zh_CN) |
| 3 MB | qtwebengine_resources*.pak + v8_context_snapshot.bin |
| 其余 | icudtl.dat、python312.dll、Qt6 其余 DLL、certifi 等 |

已做的瘦身(共 **-154.8 MB**,见 `tools/build_exe.py: prune_dist`):

- 删 `qtwebengine_devtools_resources*.pak`(仅 devtools/F12 用,88MB);
- 删全部 `*.debug.pak` / `v8_context_snapshot.debug.bin`(调试资源);
- Qt 翻译仅保留 zh_CN;Chromium locale 仅保留 zh-CN + en-US;
- 删 qt6pdf.dll(所有二进制导入闭包均未引用;qt6svg.dll 保留——
  qsvg 图标/图片插件运行时动态加载)。

启动性能已做的优化:单目录 standalone(不做 onefile,避免每次启动自解压)、
`--lto=yes`、`--python-flag=no_site`。

## 复测方法

```bash
uv run python tools/build_exe.py                        # 重建(增量约 1-2 分钟)
uv run python tools/measure_perf.py --target both       # 复测,输出汇总与判定
```
