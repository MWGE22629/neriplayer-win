<h1 align="center">NeriPlayer Win</h1>

<div align="center">

<h3>✨ 一个把网易云与 Bilibili 搬进 Windows 桌面的轻量音频播放器 🎵</h3>

<p>
  <a href="https://github.com/MWGE22629/neriplayer-win/releases">
    <img alt="Release" src="https://img.shields.io/github/v/release/MWGE22629/neriplayer-win?label=Release" />
  </a>
  <a href="https://github.com/MWGE22629/neriplayer-win/releases">
    <img alt="Downloads" src="https://img.shields.io/github/downloads/MWGE22629/neriplayer-win/total?style=social" />
  </a>
  <img alt="Platform" src="https://img.shields.io/badge/Platform-Windows%2010%2B%20x64-0078D6?logo=windows&logoColor=white" />
  <img alt="License" src="https://img.shields.io/badge/License-GPL--3.0-blue" />
</p>

<p>
  <img src="https://github.com/MWGE22629/neriplayer-win/raw/main/src/neriplayer_win/assets/app.ico" width="140" alt="NeriPlayer Win" />
</p>

</div>

> [!WARNING]
> 本项目仅供学习与研究使用,请勿将其用于任何非法用途。
> 请只在你拥有权利、授权或第三方平台规则允许的范围内访问、播放内容。
> 本项目不提供媒体内容、密钥或规避付费/DRM/地区限制的方案,
> 也不提供任何公共媒体代理或再分发服务。

---

## 快速体验 / Getting Started

1. 前往 [Releases](https://github.com/MWGE22629/neriplayer-win/releases)
   下载最新的 `NeriPlayer-win-*.zip`;
2. 解压到任意目录(保持文件夹结构完整,`bin/` 内是 mpv 播放运行库);
3. 双击 `NeriPlayerWin.exe` —— 免安装,无需预装 Python、mpv 或任何运行时。

> [!NOTE]
> 首次使用请在应用内完成网易云 / Bilibili 登录:内嵌浏览器会打开平台
> 官方登录页,扫码、手机号等方式均可,登录成功后自动进入应用,重启免再次登录。

---

## 核心特性 / Key Features

- 🎧 **双音源**:网易云音乐(「我喜欢的音乐」、自建歌单与收藏歌单)
  与 Bilibili(收藏夹与「稍后再看」),以歌单视角统一呈现;
- 🔐 **网页登录**:内嵌浏览器打开官方登录页,支持平台提供的任意登录方式,
  应用本身不经手账号密码;
- 📋 **统一播放队列**:网易云与 B 站歌曲混排进同一队列,支持
  顺序 / 随机 / 单曲循环,一首播完自动接下一首,队列窗口可查看与切跳;
- ▶️ **完整播放控制**:播放 / 暂停、上一首 / 下一首、进度拖动、音量调节,
  支持系统全局媒体键;
- 🖼️ **封面与主题**:播放条展示歌曲封面,Material 配色,
  亮色 / 暗色外观可切换;
- 🎚️ **音质选择**:无损 / 极高 / 标准三档偏好(默认无损,自动回退),
  播放时展示实际生效音质;
- 🗂️ **侧栏折叠与排序**:平台分区单击折叠(网易云·歌单 / 网易云·收藏 /
  B站·收藏夹),歌单 / 收藏夹在分区内拖拽排序,状态持久化;
- 💗 **收藏歌单与稍后再看**:网易云收藏的他人歌单独立分区与自建歌单并列;
  B站「稍后再看」并入收藏夹分区首位,一点即播;
- 🛟 **失败不打断听歌**:播放失败改为状态栏 + 托盘气泡提示并自动跳
  下一首(连续失败自动熔断);当前曲开播即预取下一首地址,切歌零等待;
- 📍 **最小化到托盘**:关窗口不退出,托盘图标常驻,后台继续播放
  (进程优先级高于正常,游戏时切歌更跟手);
- 🔁 **登录态本地持久化**:重启免登录,过期可感知并提示重新登录。

---

## 平台特点 / Why Windows Native

- **原生桌面应用**:基于 Qt(PySide6),窗口、托盘、媒体键都是 Windows
  原生体验,双击即开,后台不驻留任何服务;
- **mpv 播放内核**:音频播放由 libmpv 承担,解码能力与稳定性同 mpv 一致;
- **浏览器级风控兼容**:登录发生在真实网页上下文里,指纹与风控由平台
  自身的 SDK 完成,比客户端直连方案更稳。

---

## 轻量与隐私 / Lightweight & Private

- **轻量是硬指标**:实测冷启动中位约 0.5s、常驻内存约 140MB;
  无遥测、无统计 SDK、无开机自启;
- **只访问官方接口**:除 music.163.com / bilibili.com 及其 CDN 外,
  不连接任何第三方服务器;
- **凭据纯本地**:登录 Cookie 只保存在本机 `%APPDATA%\neriplayer-win\`,
  不会上传到任何地方;
- **无痕登录会话**:内嵌登录浏览器为 off-the-record 会话,不落盘浏览
  记录与缓存;删除上述本地目录即彻底清除全部数据。

---

## 问题反馈 / Bug Report

使用中遇到问题欢迎到 [Issues](https://github.com/MWGE22629/neriplayer-win/issues)
提交,附上 Windows 版本、应用版本与复现步骤即可。

---

## 发展规划 / Roadmap

正在计划的方向(不承诺时间表):

- 🌱 **推荐内容**:接入每日推荐、私人 FM 等网易云推荐源;
- 🖼️ **应用图标与 About**:补齐 GPL-3.0 合规标注(衍生自 NeriPlayer)。

> 欢迎到 [Issues](https://github.com/MWGE22629/neriplayer-win/issues)
> 提交 bug 与新功能建议——会在不破坏「轻量、简洁」的前提下尽可能考虑。

---

## 鸣谢 / Reference

- [NeriPlayer](https://github.com/cwuom/NeriPlayer) —
  本项目衍生自它的 Android 实现,音源 API 行为以其为参考翻译;
- [mpv](https://mpv.io) / [mpv-winbuild](https://github.com/shinchiro/mpv-winbuild-cmake) —
  播放内核与 Windows 运行库来源。

---

## 许可证 / License

本项目以 **GPL-3.0** 开源,详见 [LICENSE](./LICENSE)。

- ✅ 你可以自由使用、修改和分发本软件;
- ⚠️ 分发修改版时须继续遵守 GPL-3.0,并保留对上游 NeriPlayer 的衍生标注;
- 🧩 发行包内含 Qt / QtWebEngine(PySide6)、libmpv、Python 及若干
  PyPI 依赖,均按各自许可条款随包分发,对应源码见本仓库与各上游项目。
