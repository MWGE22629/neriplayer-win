# M7 · 本地歌单 实施方案调研

调研对象:仓库当前实现(v0.8.1,TODO.md 的 M7 立项文本)。
本文只给方案与代码依据,不落实现代码。

## 0. 现有代码基线

| 关注点 | 现状 | 依据 |
| --- | --- | --- |
| 设置存储 | `settings.json`,单文件 JSON,加载即逐键校验回落默认 | `src/neriplayer_win/data/store.py:366-400` |
| 设置写入 | `save_settings` 全量重算合法值后整文件覆盖写 | `src/neriplayer_win/data/store.py:402-435` |
| 登录态存储 | `netease.json` / `bili.json`,独立文件 | `src/neriplayer_win/data/store.py:28-30` |
| 侧栏控件 | `QTreeWidget` 子类 `_SidebarTree` | `src/neriplayer_win/ui/main_window.py:629-755` |
| 条目身份 | `UserRole` 存 `(kind, payload)` 元组 | `src/neriplayer_win/ui/main_window.py:1425,1437` |
| 分区折叠 | `SETTING_SIDEBAR_EXPANDED` 字典 | `src/neriplayer_win/data/store.py:50,155-163` |
| 分区排序 | 三个 `*_order` 设置键 + 纯函数 `apply_stored_order` | `src/neriplayer_win/data/store.py:51-53,210-225` |
| 统一歌曲条目 | `QueueSong(source, id, bvid, title, artist, duration_ms, cover_url)` | `src/neriplayer_win/player/queue.py:58-68` |
| 封面加载 | `CoverLoader.request/preload` + 内存 FIFO 512 + 磁盘 sha1 缓存 32MiB LRU | `src/neriplayer_win/ui/covers.py:35-47,125-233` |
| 列表引用 | `_ListRef(kind, id, title)`,kind 受 `RECENT_KINDS` 白名单约束 | `src/neriplayer_win/ui/main_window.py:261-272`,`src/neriplayer_win/data/store.py:78-85` |
| 右键菜单 | 单动作 `QMenu` + `addAction` + `exec` | `src/neriplayer_win/ui/main_window.py:2162-2171` |
| 后台任务 | `workers.run_async(fn, on_done, on_error)`,信号回 UI 线程 | `src/neriplayer_win/ui/workers.py:31-65` |

## 1. 数据模型

### 1.1 存储位置:新建 `local_playlists.json`

结论:新建独立文件 `<数据目录>/local_playlists.json`,不塞进 `settings.json`。

理由,逐条对应现有实现:

1. `settings.json` 的读写是「整份 dict 进出 + 逐键白名单」模式,见
   `load_settings` 与 `save_settings`(`store.py:366-435`)。歌单是变长集合,
   每次增删一首歌都要全量序列化整棵树;放进 settings 会让每次改歌单都
   重写全部设置键,也让 settings 的校验函数膨胀成一个歌单解析器。
2. 现有代码已经确立了「不同生命周期数据分文件」的先例:登录态用
   `netease.json`/`bili.json`(`store.py:28-30`),缓存用 `covers/`
   子目录(`covers.py:135-136`)。歌单属于用户产出数据,生命周期独立于
   界面偏好。
3. `save_settings` 在 MainWindow 里被 11 处调用(`main_window.py` 中
   `save_settings` 行号 1775/1791/1811/2375/2623/2639/2644/2656/2664/2679/2691),
   任意一处用旧快照覆盖都会写回整份 settings;歌单若混在里面,一次
   「设置页改语言」就会连带重写全部歌单,损坏面被放大。

新增内容(与现有命名习惯一致):

```
_SETTING_ 前缀是 settings 键的惯例(store.py:44-58)。
歌单文件用 _LOCAL_PLAYLISTS_FILE = "local_playlists.json"(与 _SETTINGS_FILE 同级常量,store.py:27-30)
```

`LocalStore` 增加 `local_playlists_path` 属性(对照 `settings_path`,`store.py:245-247`),
以及 `load_local_playlists()` / `save_local_playlists()` 两个方法。

### 1.2 文件结构与字段设计

顶层用一个带版本号的对象,而不是裸数组:

```json
{
  "version": 1,
  "playlists": [
    {
      "id": 3,
      "name": "通勤",
      "created_at": 1761177600000,
      "cover_url": "",
      "items": [
        {
          "source": "netease",
          "id": 1234567,
          "extra": {},
          "title": "歌名",
          "artist": "歌手",
          "duration_ms": 240000,
          "cover_url": "http://p1.music.126.net/..."
        }
      ]
    }
  ]
}
```

字段语义与依据:

- `version`:整数,当前 1。现有 store 没有任何版本位(`default_settings`
  `store.py:94-109` 是纯键表,`load_settings` 直接按键回落),所以这是
  第一个需要迁移位的文件。放版本号而不是靠字段探测,是因为 M7 之后
  条目结构还要为酷狗扩字段(见 1.3)。
- `id`:整数,单调递增的本地序号。取号方式用 `max(已有 id) + 1`,
  与 `RECENT_KINDS` 用 `int` 作 id 的既有约定同类型
  (`store.py:196-201` 校验 `isinstance(entry_id, int) and not isinstance(entry_id, bool)`)。
  用整数而非 uuid 字符串的理由:
  1. 侧栏 `section_child_ids` 读 payload 后强制 `int(payload)`
     (`main_window.py:745`),整数无需改这段;
  2. `_validated_id_order` 只接受 `list[int]`(`store.py:166-172`),
     排序键天然复用;
  3. 本地单用户单进程,不需要跨设备合并,不承担 uuid 的 36 字符成本。
- `name`:字符串,界面净化后展示(`sanitize_ui_text`,`textsafe.py:173-175`),
  存储时保留原文。空名在创建时拒绝(弹窗校验),不做静默改名。
- `created_at`:epoch 毫秒,与 `save_netease` 的 `savedAt`
  (`store.py:287` `int(time.time() * 1000)`)同一口径。
- `items`:数组,顺序即歌单内顺序(不做二次排序)。
- `cover_url`:手动指定封面的 URL,空串表示未指定、走自动封面。
  这是 1.5 节「手动优先」的判定字段。

### 1.3 条目标识:如何不因新增音源改结构

现状:`QueueSong` 的标识是 `(source, id)`,`bvid` 是 B站专用字段
(`queue.py:62-64`);预取槽的命中校验正是按 `(song.source, song.id)` 比对
(`main_window.py:2479-2483`),表格当前曲行定位也是 `source + id`
(`main_window.py:1934-1941`)。

结论:**唯一键用 `(source, id)`,但条目里额外保留一个自由 `extra` 字典**,
B站把 `bvid` 放进去,酷狗将来放 `hash` / `album_id`,不改顶层结构。

理由:

1. `(source, id)` 已足够区分现有两源:网易云 id 是 song id,B站 id 是 avid
   (`queue.py:62-63` 注释)。两源 id 空间独立,`source` 前缀消除碰撞。
2. B站的 `bvid` 是 avid 的另一种表示,播放时 `resolve_audio_stream` 用的正是
   `song.bvid`(`main_window.py:2231-2234`)。如果本地歌单只存 avid,重新
   解析前要先 `get_video_basic_info_by_bvid`(`api/bili/client.py:1171`),
   而用 avid 查 bvid 需要额外接口;直接存 `bvid` 省一次网络往返。
3. 酷狗的播放地址解析按 TODO M6 描述需要 hash + album_id(哈希与专辑 id
   是酷狗 playurl 的输入)。若把这类参数写成顶层具名字段,每接一个音源
   就要在 store 校验、序列化、快照比对里各加一处分支。
4. 用一个 `extra: dict[str, str]` 把「音源私有的解析参数」隔离,结构稳定,
   校验时只需断言它是 `dict[str, str]` 且键值长度有限。B站存
   `{"bvid": "BV1xx"}`;网易云存 `{}`;酷狗存 `{"hash": "...", "album_id": "..."}`。

不推荐把 `source` 做成枚举类:现有 `QueueSong.source` 是裸字符串
(`queue.py:62`),`_resolve_song_context` 用 `if song.source == "bili"` 分发
(`main_window.py:2228`)。保持字符串,新增音源时只需在该分发处加分支,
与现有风格一致。

### 1.4 快照 vs 只存标识

| 维度 | 存完整快照(推荐) | 只存标识、打开时回源 |
| --- | --- | --- |
| 离线展示 | 断网/未登录也能列出歌单与曲目名 | 断网时列表空白,只剩占位 |
| 侧栏封面 | 启动即可取 `cover_url` 请求封面,无需登录 | 需先回源,启动后封面长时间空白 |
| 远端失效 | 歌名仍在,只是播放失败(与现有 `_handle_play_failure` 一致) | 歌被删则整行消失,用户感知为「我加的歌没了」 |
| 字段新鲜度 | 歌名/封面可能过期(改名、换封面不跟随) | 永远最新 |
| 网络成本 | 0(打开歌单不请求) | 每次打开一次批量详情请求 |
| 代码成本 | 条目校验要处理 6 个字段;文件体积随歌单条数线性增长 | 校验简单;打开路径要复用 `get_song_detail`(`api/netease/client.py:851`)与 B站视频信息接口 |
| 播放正确性 | 播放仍走 id 重新解析,与快照无关 | 同 |

推荐:**存快照**。决定性依据是封面需求。TODO 要求「启动后逐个异步获取
歌单封面」,而自动封面的来源是第一首歌的 `cover_url`。若只存标识,启动时
拿不到 `cover_url`,必须为每个歌单发一次详情请求才能知道封面地址,
既慢又要登录态。存快照后,启动即可把 URL 交给 `CoverLoader.preload`,
与表格封面的路径完全一致(`main_window.py:1894-1896`)。

快照的过期处理:不做后台刷新(增量同步是另一个需求)。播放时一律按
`source + id` 重新解析(`_resolve_song_context`,`main_window.py:2218-2261`),
所以快照只影响展示,不影响能否播放。

条目校验的具体规则见 2.2。

## 2. 持久化与校验

### 2.1 现有原子性现状与本地歌单的写策略

现有写盘**没有**临时文件 + `os.replace` 的原子写:`save_settings` 直接
`write_text`(`store.py:428-432`),`save_netease`/`save_bili` 同样
(`store.py:291-293`、`352-354`)。项目里唯一做了「临时文件 + replace」的
是封面缓存(`covers.py:223-225`),因为那是多线程并发写。

本地歌单的写频率远高于设置(每次加歌/新建/改名/排序),且是用户产出
数据,建议比设置更保守:

1. 写临时文件 `local_playlists.json.<pid>.tmp`,再 `os.replace` 到正式路径。
   这是项目内已有写法(`covers.py:223-225`),不引入新依赖。
2. 写失败(`OSError`)返回 `False`,与 `save_settings` 的契约一致
   (`store.py:433-435`),调用方在状态栏提示。
3. 不做写合并/防抖:M7 的写操作都是用户显式动作(新建/加歌/拖拽),
   频率是「几次每分钟」量级,不需要为它引入定时器。若后续出现批量导入,
   再在调用侧合并。

### 2.2 校验模式:沿用 `validate_and_sanitize` 的思路

现有模式是「加载时逐字段判断类型与白名单,不合格就回落默认,绝不抛异常」,
见 `_validated_sidebar_expanded`(`store.py:155-163`)、`_validated_id_order`
(`store.py:166-172`)、`_validated_recent_lists`(`store.py:182-207`)。
其中 `_validated_recent_lists` 是最接近的模板:逐条过滤、保序去重、
超限截断、非法条目静默丢弃。

本地歌单校验函数建议命名为 `_validated_local_playlists(value)`,规则:

| 层级 | 规则 | 依据模板 |
| --- | --- | --- |
| 顶层 | 非 dict / `version` 非 int → 返回空列表 | `_validated_recent_lists` 的 `if not isinstance(value, list): return []`(`store.py:185-186`) |
| 版本 | `version > 1` 时保留数据但只解析已知字段(前向兼容);`version <= 0` 视为 1 | 新增,无模板 |
| 歌单 | `id` 正整数、`name` 非空 str、`created_at` int、`items` list、`cover_url` str;任一不合格丢弃该歌单 | `_validated_recent_lists` 的逐条 `continue`(`store.py:189-202`) |
| 歌单 id 去重 | 同 id 只留首个 | `seen: set` 模式(`store.py:187,203-205`) |
| 歌单名长度 | 截断到上限(建议 100 字符),避免超长名撑破侧栏 | 新增 |
| 条目 | `source` 属于白名单(见下)、`id` 正整数、`extra` 为 `dict[str,str]`、`title`/`artist` str、`duration_ms` 非负 int、`cover_url` str | `_validated_recent_lists` 同构 |
| 条目去重 | 同一歌单内 `(source, id)` 只留首个(重复添加的兜底) | `seen` 模式 |
| 条目上限 | 单歌单条目数上限(建议 5000),超出截断 | `recent_max` 截断先例(`store.py:395-399`) |

`source` 白名单建议放在 `store.py` 顶层常量,与 `RECENT_KINDS` 并列
(`store.py:78-85`),命名 `LOCAL_SONG_SOURCES = ("netease", "bili")`。
M6 接酷狗时只改这一处加 `"kugou"`,与 `_resolve_song_context` 的分发
(`main_window.py:2228-2245`)配对维护。

### 2.3 损坏文件兜底

对照 `load_netease`(`store.py:251-274`)的三段式:

```python
try:
    raw = path.read_text(encoding="utf-8")
except OSError:
    return ...        # 文件不存在/无权限:静默返回空
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    return ...        # 损坏:静默返回空
if not isinstance(data, dict):
    return ...
```

`load_local_playlists` 完全照此结构,差别只有兜底值从 `None` 变成
`{"version": 1, "playlists": []}`。不建议在损坏时备份原文件:现有
`load_settings` 遇到损坏也是直接回落默认(`store.py:369-373`),一致性
优先;用户可在日志里看到(M7 可加一行 `logger.warning`)。

## 3. 侧栏集成

### 3.1 现有侧栏的元组约定

条目身份是 `UserRole` 里的 `(kind, payload)`(`main_window.py:1425` 分区头、
`1437` 子节点)。读取侧有两处解析:

- `_SidebarTree.item_kind`(`main_window.py:668-674`):只取 `role[0]`。
- `_on_sidebar_item_clicked`(`main_window.py:1648-1695`):`kind, payload = role`。
- `_apply_item_icons`(`main_window.py:1587-1607`):`role[0]` 查图标表;
  `recent-item` 额外看 `role[1]` 是三元组。

`payload` 现有两种形态:裸 `int`(歌单/收藏夹 id,`main_window.py:1487,1510,1538`)
与 `None`(固定项,`main_window.py:1503,1532`),以及 `recent-item` 的
三元组 `(kind, id, title)`(`main_window.py:1469-1471`)。

### 3.2 需要改动的函数清单

| 函数/常量 | 位置 | 改动 |
| --- | --- | --- |
| `_SIDEBAR_SECTIONS` | `store.py:86` | 加 `"local"` |
| `default_settings` 的 `sidebar_expanded` | `store.py:103` | 自动跟随 `_SIDEBAR_SECTIONS` |
| `_SETTING_ID_ORDERS` | `store.py:87-91` | 加 `SETTING_LOCAL_PLAYLIST_ORDER` |
| `default_settings` 排序键 | `store.py:104-106` | 加同名空列表 |
| `_HEADER_SECTION` | `main_window.py:195-200` | 加 `"local-header": "local"` |
| `_SidebarTree._SECTION_CHILD_KIND` | `main_window.py:643-647` | 加 `"local": "local-playlist"` |
| `_SidebarTree.order_changed` 注释 | `main_window.py:640` | 补 `"local"` |
| `_rebuild_sidebar` | `main_window.py:1445-1562` | 插入「本地歌单」分区 |
| `_SIDEBAR_KIND_ICON` | `main_window.py:1564-1574` | 加 `"local-playlist": "library_music"` |
| `_apply_item_icons` | `main_window.py:1587-1607` | 加 `local-header` 单色标分支 |
| `_sidebar_list_ref` | `main_window.py:1609-1627` | 加 `local-playlist` 分支 |
| `_open_list_ref` | `main_window.py:1629-1646` | 加 `local-playlist` 分支 |
| `_on_sidebar_item_clicked` | `main_window.py:1648-1695` | 加 `local-playlist` 分支 |
| `_on_sidebar_order_changed` | `main_window.py:1778-1800` | 加 `local` 分支 |
| `_apply_stored_order` 调用点 | `main_window.py:1802-1812` | 新增调用,在启动加载时 |
| `RECENT_KINDS` | `store.py:78-85` | 加 `"local-playlist"` |

### 3.3 kind 命名与 payload 类型

命名沿用现有 `"<source>-<object>"` 与 `"<section>-header"` 两套后缀
(`main_window.py:195-200` 与 `149-154` 的图标表):

- 分区头:`"local-header"`,section 键 `"local"`
- 子节点:`"local-playlist"`,payload = `int`(歌单 id)
- 图标:`"library_music"`(已在 `ICON_NAMES`,`icons.py:30-51`),或新增一个
  独立图标区分平台歌单。推荐先复用 `library_music`,因为 M7 不新增资产,
  而 `ICON_NAMES` 里每个名字都有一条测试断言文件存在
  (`tests/test_ui_assets.py:36-39`),新增图标要同时改 `scripts/make_assets.py`
  (`_MATERIAL_SYMBOLS`,`scripts/make_assets.py:190`)。

payload 用 `int` 而非 dict。理由:`section_child_ids` 对 payload 做
`int(payload)`(`main_window.py:745`),用 int 不需要改;`_validated_id_order`
只接受 `list[int]`(`store.py:166-172`);而 dict 会让拖拽排序的 id 提取
变成字段解包,与「recent-item 用三元组」那种特例风格混在一起,增加分支。
歌单标题这类可变信息由 `_sidebar_list_ref` 从内存列表现查
(`main_window.py:1612-1627` 的既有做法),不进 payload。

### 3.4 参与拖拽排序

`_SidebarTree.dropEvent` 的准入条件是 `item_kind(dragged)` 在
`_SECTION_CHILD_KIND.values()` 里(`main_window.py:682`),目标必须同父
(`main_window.py:687-695`),移动后按父节点 kind 反查 section 并发信号
(`main_window.py:706-708`)。因此只要把 `"local": "local-playlist"` 加进
`_SECTION_CHILD_KIND`,拖拽自动生效,`dropEvent` 本身无需改动。

`_on_sidebar_order_changed` 目前是 `if/elif/else` 三段硬编码
(`main_window.py:1781-1800`),其中 `else` 兜底到 B站。加第四个分区时
必须把 `else` 改成显式 `elif section == "bili"`,否则 `local` 会走进
B站分支、把 `by_id` 建成收藏夹字典、写错设置键。这是 M7 唯一一处
「不改就会静默写错数据」的改动点。

排序键建议名 `SETTING_LOCAL_PLAYLIST_ORDER = "local_playlist_order"`
(对照 `SETTING_NETEASE_PLAYLIST_ORDER`,`store.py:51`)。

### 3.5 与「最近」分区特例逻辑的冲突规避

「最近」分区的特例集中在四处:

1. 只有非空才出现(`main_window.py:1458-1461`);
2. 分区头没有品牌标,用 `_monochrome_header_icon("history", ...)`
   (`main_window.py:1593-1597`);
3. 子节点 `recent-item` 的 payload 是三元组,图标按 `role[1][0]` 二次查表
   (`main_window.py:1600-1601`);
4. `_open_list_ref` 的 `search` 分支做前缀剥离(`main_window.py:1642-1646`)。

「本地歌单」与这四条都不重叠:

- 分区始终出现(即使空,也要给用户看到「+」入口),所以走
  `_add_header_item` 的无条件分支,不复制 1458 的判空。
- 分区头用单色标 + 折叠三角,复用 `_monochrome_header_icon`
  (`main_window.py:1735-1737`),传一个非品牌图标名即可;不要传
  `"local"` 给 `_section_header_icon`,那会去 `_SOFT_BRAND_COLORS` 查品牌色
  并 KeyError(`main_window.py:1403`,这正是 v0.2.0 那个 bug 的形态,
  `_on_sidebar_section_toggled` 的注释记录了成因,`main_window.py:1758-1766`)。
- payload 是裸 int,不需要三元组分支;`_apply_item_icons` 里
  `kind == "recent-item"` 的判断天然不命中。
- `_open_list_ref` 走新分支,不碰 search 前缀。

另一个隐式冲突:`_rebuild_sidebar` 里 `expanded` 的应用是「先对 netease
单独 setExpanded,再遍历顶层按 `_HEADER_SECTION` 通用处理」
(`main_window.py:1554-1560`)。那行 `netease_header.setExpanded(...)` 是
冗余的(遍历已覆盖),新分区不需要复制它。

## 4. 分区头「+」按钮

### 4.1 约束

分区头是 `QTreeWidgetItem`,不是 widget(`_add_header_item`,
`main_window.py:1417-1429`)。同时有三条既有约束:

1. 分区头必须 `ItemIsEnabled` 且不可选中,不能用 `NoItemFlags`
   (`main_window.py:1420-1423` 的注释说明:禁用父项会让子节点无法选中)。
2. 分区头单击 = 折叠切换(`main_window.py:1652-1654`),命中区判定已经
   被这个语义占满。
3. 图标是 30x18 的合成画布(品牌标 + 折叠三角),DPI 2x 栅格化
   (`main_window.py:1739-1756`);任何叠加控件都要考虑与它的排布。

### 4.2 方案 A:`setItemWidget` 挂一个按钮

做法:`_add_header_item` 后调用 `sidebar.setItemWidget(header, 0, button)`,
按钮放进一个右对齐的容器(左 stretch + 按钮)。

优点:

- 是真控件,自带 hover/pressed 样式(`QPushButton` 的 QSS 已在
  `theme.py:255-273`),点击不参与树的选中与折叠语义。

缺点,逐条对照现有实现:

1. `setItemWidget` 会遮住 item 自身的绘制区域。分区头的文字与图标都由
   item 绘制,一旦挂 widget,要么 widget 容器透明并让 item 文字透出
   (需要 `QWidget` 背景透明 + 不覆盖整行),要么自己重画整行,后者等于
   放弃 `QTreeWidget::item` 的 QSS(`theme.py:167-172` 的圆角/hover/选中态)。
2. 整树重建是常态(`_rebuild_sidebar` 在登录、加载、语言切换、最近压栈时
   都会调用,`main_window.py:1130,1251,1338,1393,2376,2706`)。每次重建
   都要重新 `setItemWidget` 并管理旧 widget 的销毁,否则泄漏。
3. 拖拽:widget 覆盖区域内的鼠标事件被按钮吃掉,分区头本身的折叠点击
   在按钮区域失效(可接受),但树的行高/命中区与 widget 几何要手动同步,
   `_clamp_sidebar_width` 改变宽度时(`main_window.py:1820-1829`)需要重新
   布局,而 item widget 的几何由 Qt 内部管理,存在滞后风险。
4. 测试:现有侧栏测试全部基于 `QTreeWidgetItem` 的 `data(0, UserRole)`
   与 `visualItemRect` 断言(如 `tests/test_main_window_layout.py:90-107,228-253`),
   widget 方案要多写控件查找与点击注入。

### 4.3 方案 B:在侧栏顶部放一行工具条(树外)

做法:侧栏容器从裸 `_SidebarTree` 改成一个 `QWidget` + `QVBoxLayout`,
顶部一行放「本地歌单 + 新建」,或者干脆在树的顶部放一个常驻「+ 新建歌单」
条目。

变体 B1(树外工具条):

- 优点:完全不动树;按钮是真控件;`_apply_sidebar_icons` 与拖拽零影响。
- 缺点:位置与「分区头右侧」的需求不符(TODO 明确写「分区头右侧带 + 按钮」);
  且 `self.sidebar` 现在直接加进 QSplitter(`main_window.py:1031`),
  改成容器后 `_clamp_sidebar_width`(`main_window.py:1820-1829`)里的
  `self.sidebar.setMinimumWidth/MaximumWidth` 与 `_splitter.setSizes` 都要
  跟着改,`resizeEvent` 的钳制逻辑牵连 3 处。

变体 B2(树内固定条目):在「本地歌单」分区头下固定一个
`("local-create", None)` 子节点,图标用 `add`。

- 优点:零新增机制,与「稍后再看固定首位」(`main_window.py:1527-1533`)、
  「每日推荐固定首位」(`main_window.py:1501-1504`)完全同构;不参与排序
  只需不把它放进 `_SECTION_CHILD_KIND` 的 `kind`(现有实现正是靠
  `section_child_ids` 按 kind 过滤,`main_window.py:743-745`)。
- 缺点:不是「分区头右侧」,而是「分区内第一行」;用户要先展开分区才看得到。
  分区折叠时入口消失。

### 4.4 方案 C:在分区头行内绘制命中区(自绘按钮)

做法:重写 `_SidebarTree.paintEvent` 或在 `_SidebarTree` 里装
`QStyledItemDelegate`,对 `local-header` 这一行在右侧画一个「+」图标;
`mousePressEvent` 里判断点击点是否落在该矩形内,命中则发
`create_local_playlist_requested` 信号并 `event.accept()`,否则走原有
折叠逻辑。

优点:

1. 视觉与需求完全一致:按钮就在分区头右侧,和文字同一条基线,不受
   QSS 圆角/hover 影响(自绘在 item 之上)。
2. 不引入子控件,`_rebuild_sidebar` 重建时无需管理 widget 生命周期,
   只需在 `_apply_item_icons` 之后重绘。
3. 折叠与拖拽语义不受影响:命中区在 `mousePressEvent` 里先判,未命中
   直接 `super()`,现有 `itemClicked` 链路不变。
4. 图标复用现有资产体系:把「+」画成 QPixmap(与 `_arrow_pixmap`
   `main_window.py:1711-1729` 同一套路,`QPainter` 画两条线),不新增 SVG,
   不触发字体回退(这是 `_arrow_pixmap` 注释里明确要规避的坑,
   `main_window.py:1713-1714`)。

缺点:

1. 需要处理 DPI:命中区矩形用逻辑坐标算,绘制用 2x 画布,与
   `_composite_header_icon` 的 DPR 处理同构(`main_window.py:1742-1756`)。
2. 需要处理侧栏宽度变化:命中区右边缘按 `viewport().width()` 现算,
   在 `paintEvent` 与 `mousePressEvent` 里用同一个私有函数取矩形,避免两处
   漂移。
3. hover 反馈要自己做(`mouseMoveEvent` 记录 hover 行),否则按钮没有
   视觉反馈。可用 `setMouseTracking(True)` + 记录 `_hover_plus_rect`,
   复杂度约 30 行。

### 4.5 对比与推荐

| 维度 | A setItemWidget | B2 树内固定条目 | C 自绘命中区 |
| --- | --- | --- | --- |
| 位置符合需求 | 是 | 否(分区内首行) | 是 |
| 对 `_apply_sidebar_icons` 影响 | 需保证不遮图标,重建要重挂 | 无 | 无(在图标之后绘制) |
| 对拖拽影响 | 按钮区域吞事件 | 无(不在可排序 kind 内) | 无(先判命中再 super) |
| 对折叠影响 | 按钮区域不触发折叠 | 无 | 无 |
| 重建成本 | 每次重建挂/销毁 widget | 无 | 一次 `update()` |
| 新增代码量 | 中(容器 + 生命周期) | 最小 | 中(绘制 + 命中 + hover) |
| 与现有绘制风格一致性 | 低(控件与 item 绘制混排) | 中 | 高(与折叠三角同套路) |

推荐 **C(自绘命中区)**,并把 B2 作为第一版落地形态的退路:

- M7 第一步先按 B2 上线(一个 `local-create` 固定子节点),功能完整、测试
  好写,不引入新的绘制代码。
- 第二步把 B2 换成 C:分区头右侧自绘「+」,命中区判定抽成
  `_local_header_plus_rect(header)` 纯函数,便于单测。B2 的固定条目同时
  删掉。

不推荐 A:`setItemWidget` 与「item 自绘 + QSS 圆角 + 图标合成 + 整树频繁
重建」这套现状的耦合成本高于它省下的代码量。

若最终选 A,必须注意:`setItemWidget` 的容器要
`setAttribute(Qt.WA_TransparentForMouseEvents, False)` 且背景透明,并且
`_rebuild_sidebar` 的 `sidebar.clear()`(`main_window.py:1447`)会销毁
item widget,不需要手动 delete,但要保证按钮信号在重建后重新连接。

## 5. 「添加到…」交互

### 5.1 现有右键菜单结构

`_on_table_context_menu`(`main_window.py:2162-2171`):

```python
menu = QMenu(self.song_table)
play_next_action = menu.addAction(tr("play.play_next_menu"))
chosen = menu.exec(self.song_table.viewport().mapToGlobal(pos))
if chosen is play_next_action:
    self._insert_next_from_table(row)
```

单动作 + 返回对象比对。`QMenu` 的 QSS 在 `theme.py:469-486`(含
`::separator`),二级菜单会继承同一套样式,无需新增样式。

### 5.2 二级菜单「添加到…」

做法:`menu.addMenu(tr("play.add_to_menu"))` 得到子 `QMenu`,
再 `submenu.addAction(歌单名)` 逐个加已有歌单,末尾加一个分隔符
(`submenu.addSeparator()`)与 `submenu.addAction(tr("play.add_to_new"))`。

动作与歌单的映射用 `QAction.setData(playlist_id)` 而不是闭包捕获:
`menu.exec` 只返回被选中的 `QAction`,`action.data()` 取回 id。
闭包捕获在歌单较多时会为每个 action 建一个 lambda,且无法在测试里
从 action 反查目标。

菜单项文案直接用歌单名(经 `sanitize_ui_text` 净化),不加「(N 首)」计数:
计数在加歌后会变,而菜单是一次性构建,不需要精确。

排序:按侧栏显示顺序(即 `self._local_playlists` 的顺序,已被
`apply_stored_order` 排过,`main_window.py:1802-1812`),与侧栏一致。

### 5.3 「当场新建」流程

两个候选:

方案 a:`QInputDialog.getText` 一行拿名字。代码最少,但
`QInputDialog` 在项目里没有先例(全仓只有 `BrowserLoginDialog` 一个
`QDialog`,`ui/browser_login.py:99`),且 QSS 里 `QDialog` 已有背景样式
(`theme.py:107`),`QInputDialog` 会自动套用,外观可接受。

方案 b:自建小 `QDialog`(一个 `QLineEdit` + 确定/取消),样式完全可控。

推荐 **a**,理由是零新增文件、零新增样式,且 `QInputDialog` 的返回值语义
(ok, text)足够覆盖「取名 + 空名拒绝」这一个需求。校验放在返回后:
`text.strip()` 为空则状态栏提示、不创建。

完整流程(新建并加入):

1. 用户在二级菜单点「新建歌单并添加」。
2. 弹 `QInputDialog`,拿到名字。
3. 调 `_create_local_playlist(name)` → 分配 id、追加到内存列表、落盘。
4. 调 `_add_song_to_local_playlist(new_id, song)` → 追加条目、落盘。
5. `_rebuild_sidebar()`(新歌单要出现在侧栏)。
6. 状态栏提示 `tr("local.added", title=song.title, playlist=name)`。

### 5.4 去重

按 `(source, id)` 在目标歌单内查重。命中时不追加,状态栏提示
`tr("local.already_added", ...)`,并把菜单里的该项置灰更友好:

- 构建子菜单时对每个歌单先算 `already = any(...)`,`action.setEnabled(not already)`,
  文案追加「(已在)」后缀。这样用户点之前就知道结果,避免「点了没反应」。
- 仍然保留 5.4 的写入侧查重,防并发/重复点击。

注意:`insert_next` 的插播路径(`_insert_next_from_table`,
`main_window.py:2173-2181`)允许队列里出现重复曲目,本地歌单则应该去重,
因为歌单是「集合」语义而非「播放序列」。

### 5.5 加入后是否刷新视图

分两种情况:

- 若当前浏览的正是这个本地歌单(`self._table_context.kind == "local-playlist"`
  且 `id` 相同):重填表格,否则用户加了歌却看不到。重填走
  `_set_table_songs(self._local_playlist_songs(pid), "netease" 或按首曲来源, ref)`。
  注意表头来源:本地歌单可混收两源,现有表头只有 `_header_netease` 与
  `_header_bili` 两套(`main_window.py:148-155`),列数相同、只有第 4 列
  文案不同(歌手/UP主)。混收歌单建议固定用 `_header_netease`
  (即「歌手」),因为列语义是「作者」。这是 M7 需要明确的一个小决策。
- 若当前浏览的是别处:只重建侧栏(歌单计数/封面可能变化)与状态栏提示,
  不动表格,避免打断用户。

不建议给本地歌单做「条目数」角标:侧栏现有计数都来自接口
(`main_window.py:1484-1488`、`1536-1538`),本地计数是现成的,但加角标
会让每次加歌都触发一次 `_rebuild_sidebar` 的字符串变化;M7 可以先不加,
等有需求再说。

## 6. 封面

### 6.1 自动封面:取第一首歌的 cover_url

数据侧:`_local_playlist_cover_url(playlist)` 纯函数,
`playlist.cover_url or (playlist.items[0].cover_url if playlist.items else "")`。
手动优先的判定就落在这一个函数里,是唯一判定点。

### 6.2 异步批量获取,不卡 UI

现有机制已完全够用,不需要新线程模型:

- `CoverLoader.preload(urls)` 是整批预取,常驻 5 线程池,再次调用会作废
  上一批(`covers.py:156-176`)。表格封面正是这么用的
  (`main_window.py:1894-1896`)。
- `CoverLoader.request(url)` 是单 URL 一次请求,同 URL 去重
  (`covers.py:144-154`)。

本地歌单封面是「每个歌单一个 URL」的少量集合(通常 < 50),用
`preload` 一次性把全部歌单封面 URL 交给 loader 最省事。但有一个冲突:
`_cover_loader` 的 `cover_ready` 已经接给 `_on_table_cover_ready`
(`main_window.py:836`),而那个槽会丢弃「不在当前表里」的 URL
(`main_window.py:1898-1905`),歌单封面 URL 会被它丢弃,且
`preload` 的批次作废语义会与表格预取互相打架(切表会作废歌单封面批次)。

因此建议**新增第三个 CoverLoader 实例** `_sidebar_cover_loader`,理由与
`_seed_loader` 完全同构:`main_window.py:833-843` 的注释写明「预取与播放条
各用各的实例,磁盘缓存按 URL 共享」,`_seed_loader` 就是第三实例。磁盘缓存
按 URL 共享(`covers.py:209-218`),所以同一首歌的封面不会重复下载。

启动流程:

1. `_boot_local_playlists()` 在 `__init__` 末尾、`_boot_from_store` 附近调用
   (`main_window.py:1052-1057` 的启动序列末尾)。
2. 读文件 → 校验 → 存入 `self._local_playlists` → `_apply_stored_order`。
3. 收集 `[_local_playlist_cover_url(p) for p in playlists]`,调
   `_sidebar_cover_loader.preload(urls)`。
4. `cover_ready` 回调 `_on_sidebar_cover_ready(url, data)`:遍历
   `self._local_playlists`,对封面 URL 等于该 url 的条目 `setIcon`。

「逐个异步获取」的字面需求:preload 的 5 线程池本身就是逐个消费、逐个
投递,视觉上就是一个接一个亮起,不需要为它做串行化。如果一定要严格
串行(避免 5 个并发打 CDN),用 `request()` 在 `cover_ready` 里请求下一个
即可,但那是自找复杂度,且 `preload` 的并发上限本来就是 5
(`covers.py:39`)。

### 6.3 缓存,避免每次启动重取

三层,全部已存在:

1. `_sidebar_cover_loader._mem` 内存字典(同进程内二次访问零成本,
   `covers.py:229-233`);
2. `<数据目录>/covers/<sha1(url)>` 磁盘缓存,跨启动共享
   (`covers.py:209-218`);
3. 磁盘缓存 LRU 32MiB 预算,启动后后台清理(`covers.py:55-110`,
   `app.py` 里 `schedule_disk_cache_trim()` 已在调用)。

所以「避免每次启动重取」不需要新代码:启动时 `preload` 会在
`_load_sync` 里命中磁盘缓存直接读文件,不发网络请求(测试已覆盖,
`tests/test_covers.py:135-156`)。

侧栏图标尺寸是 30x18(`main_window.py:983`),而 `build_thumb_url` 生成
96x96 缩略(`covers.py:35,113-122`)。歌单封面是方形,显示时会按 18px
高缩放,96px 底图在 200% DPI 下刚好(18*2=36 逻辑像素需求,96 足够)。
建议绘制时用 `rounded_pixmap(pixmap, 18*dpr, 4*dpr)` 做成圆角小方图
(函数在 `ui/player_bar.py:75`,表格缩略图就是这么用的,
`main_window.py:1913-1916`)。

### 6.4 手动指定封面:交互与持久化

TODO 原文是「点击歌单可手动设置封面」。三个候选入口:

| 入口 | 交互 | 问题 |
| --- | --- | --- |
| 文件选择(`QFileDialog`) | 选一张本地图片 | 要新增本地文件路径的持久化与失效处理;路径失效后封面空白,还得回退自动;违背「数据自持,只存本地」里「不引入额外文件管理」的倾向 |
| 从歌单内歌曲里选 | 弹一个封面网格,点一首歌的封面 | 零新增文件、零新增失效路径;数据全在已有快照里;与「自动封面取首曲」同一数据源 |
| 粘贴 URL | 输入框贴图床链接 | 需要网络可达性判断,且用户为什么要这么做 |

推荐 **从歌单内歌曲里选**。实现:`QDialog` + `QListWidget`(IconMode,
`setIconSize(QSize(64,64))`,`QListWidget` 的 QSS 已在
`theme.py:188-200`),每项显示歌名 + 封面缩略图(直接复用
`_sidebar_cover_loader` 已缓存的字节,或对每首 `request`)。选中后写
`playlist.cover_url = 该曲 cover_url`,落盘,`_rebuild_sidebar`。

这里有个语义细节:`cover_url` 字段既可能是「手动指定的 URL」,也可能
恰好等于「第一首歌的 URL」。两者无法从值上区分。两个选项:

- 方案 1:加 `cover_manual: bool`。多一个字段,判定清晰。
- 方案 2:不加字段,`cover_url` 非空即视为手动。自动封面永远从
  `items[0].cover_url` 现算,不回写 `cover_url`。

推荐 **方案 2**:`cover_url` 字段只承载手动值,自动值永不落盘。判定函数
就是 6.1 的 `playlist.cover_url or items[0].cover_url`。这样「手动优先」
是数据结构自带的,不需要额外的布尔位,也不会有「手动设成与首曲相同后
无法恢复自动」的困境(清空 `cover_url` 即回自动)。

还需要一个「恢复自动封面」入口:同一菜单里加一项「使用自动封面」,
把 `cover_url` 置空。否则手动设置是一次性的、不可逆。

### 6.5 手动优先的判定位置

唯一判定点在数据访问函数 `_local_playlist_cover_url(playlist)`(6.1)。
三处调用:

1. 侧栏图标(`_rebuild_sidebar` / `_apply_item_icons`);
2. 启动批量预取(`_boot_local_playlists`);
3. 手动设置后刷新(`_rebuild_sidebar`)。

不要在 `_rebuild_sidebar` 里内联 `or` 表达式,否则三处会漂移。

## 7. 与「最近」/队列/播放的联动

### 7.1 点开歌单走 `_open_list_ref` 的哪条分支

新增分支,不做网络加载:

```python
elif list_ref.kind == "local-playlist":
    self._show_local_playlist(list_ref)
```

`_show_local_playlist` 做三件事(对照 `_load_playlist_tracks`
`main_window.py:1253-1274` 的结构,但去掉 `run_async`):

1. `_clear_table()`;
2. `_set_table_songs(songs, header_source, list_ref)`;
3. 状态栏 `tr("list.loaded", title=..., count=...)`。

`songs` 由快照直接转成 `QueueSong`:每个条目
`QueueSong(source=item.source, id=item.id, bvid=item.extra.get("bvid",""),
title=..., artist=..., duration_ms=..., cover_url=...)`。
`sanitize_ui_text` 在展示侧做(与 `_netease_song_to_queue`
`main_window.py:241-249` 一致),存储侧保留原文。

空歌单:`_set_table_songs([], ...)` 会让 `_update_table_empty_state`
(`main_window.py:1833-1841`)切到空态视图。现有文案是
「从左侧选择歌单或收藏夹,双击即可播放」,对空歌单不贴切。建议加一个
分支:当 `_table_context.kind == "local-playlist"` 且表空时,提示
`tr("empty.local_playlist")`(「这个歌单还没有歌,右键歌曲「添加到…」」)。
这是 `_update_table_empty_state` 里加一个 if,不改其他路径。

### 7.2 `_ListRef.kind` 冲突

`_ListRef.kind` 的注释说「取 `data.store.RECENT_KINDS` 白名单」
(`main_window.py:265-268`),而 `RECENT_KINDS`(`store.py:78-85`)同时被
`_validated_recent_lists` 用作持久化白名单。所以新增 `"local-playlist"`
必须同时加到 `RECENT_KINDS`,否则:

1. 侧栏 `_open_list_ref` 能走,但起播后 `_push_recent` 把
   `_ListRef("local-playlist", id, title)` 写进 settings
   (`main_window.py:2372-2375`),下次启动 `load_settings` 会把它过滤掉
   (`store.py:196-202`),表现为「本地歌单不出现在最近里」。

`RECENT_KINDS` 加一项是 M7 的必要改动,不是可选。注意它同时受
`tests/test_store.py:248-267` 的白名单测试覆盖(那条测试用 `"youtube"`
作非法 kind),新增后该测试仍然成立。

`_queue_context` 与 `_table_context` 都是 `_ListRef`
(`main_window.py:807-808`),所以本地歌单不需要新类型:
双击表格 → `_play_from_table` 比较 `_table_context != _queue_context`
(`main_window.py:2156`),`_ListRef` 是 frozen dataclass
(`main_window.py:261`),相等性按字段比较,天然正确。

`_push_recent` 的去重键是 `(kind, id)`(`main_window.py:2362-2364`),
本地歌单 id 是本地整数,与网易云歌单 id 可能数值相同但 `kind` 不同,
不会误去重。

### 7.3 播放路径

不需要改:`_play_at`(`main_window.py:2183-2216`)按 `song.source` 分发,
`_resolve_song_context`(`main_window.py:2218-2261`)只认 `"bili"` 与其余
(网易云)。本地歌单的条目在填表时就已经是 `QueueSong`,`source` 正确,
播放链路完全复用。

B站条目的 `bvid`:快照的 `extra["bvid"]` 必须还原进 `QueueSong.bvid`,
否则 `_resolve_song_context` 的 B站分支会拿到空 bvid
(`main_window.py:2231-2234`)而解析失败。这是 7.1 转换函数里唯一
不能漏的字段。

### 7.4 队列窗口

`QueueWindow` 只读 `PlayQueue`(`ui/queue_window.py:41-44`),
与列表来源无关,无需改动。

## 8. 测试建议

### 8.1 conftest 提供什么

`tests/conftest.py` 只有一个 session 级 `qapp` fixture
(`tests/conftest.py:22-36`),负责:
- 设置 `QT_QPA_PLATFORM=offscreen`(`conftest.py:19`);
- 复用进程内 `QApplication`,字体强制设为 `Microsoft YaHei UI`
  (`conftest.py:33-35`),因为离屏默认字体族是 `Sans Serif`,会让
  `textsafe` 的字形探测判定失真;
- `pytest_unconfigure` 里 `os._exit` 跳过解释器终结(`conftest.py:45-49`)。

没有 `qtbot`(未装 pytest-qt,dev 依赖组里只有 `nuitka` 与 `pytest`,
`pyproject.toml:30-34`)。
所以 UI 测试一律是「手工 `MainWindow()` + `window.show()` +
`qapp.processEvents()`」,点击用 `QTest.mouseClick`(`tests/test_main_window_layout.py:228-237`),
拖拽直接构造 `QDropEvent` 调 `dropEvent`(`tests/test_main_window_layout.py:590-602`)。

数据目录隔离靠 `monkeypatch.setenv("NERIPLAYER_WIN_DATA_DIR", str(tmp_path))`
(`tests/test_main_window_layout.py:45-51`、`tests/test_store.py:15-17`)。

### 8.2 新增测试文件与用例

建议新增两个文件,不往 67KB 的 `test_main_window_layout.py` 里继续堆。

**`tests/test_local_playlists_store.py`**(纯数据层,无 Qt)

| 用例 | 断言 |
| --- | --- |
| 默认加载(无文件) | 返回 `{"version": 1, "playlists": []}` |
| 损坏 JSON | 返回空列表,不抛异常 |
| 顶层非 dict / 非 list | 返回空列表 |
| 往返 | 存 2 个歌单再读,字段全等 |
| 非法歌单被丢 | 缺 id / id 为 bool / name 空 / items 非 list |
| 非法条目被丢 | source 不在白名单 / id 为 bool / extra 非 dict[str,str] |
| 歌单 id 重复 | 只留首个 |
| 条目 `(source,id)` 重复 | 只留首个 |
| 条目数超上限 | 截断到上限 |
| 名称超长 | 截断 |
| `version` 未知(如 2) | 仍能读出已知字段 |
| 原子写:写临时文件后 replace | 断言正式文件内容完整(可用 `monkeypatch` 让 `os.replace` 抛错,断言原文件未被破坏) |

参照 `tests/test_store.py` 的 `make_store(tmp_path, monkeypatch)` 模式
(`tests/test_store.py:15-17`)与 `class Test...` 分组风格。

**`tests/test_local_playlists_ui.py`**(UI 层)

| 用例 | 断言 |
| --- | --- |
| 侧栏分区存在且可折叠 | `find_header("local")` 非 None;折叠后 `settings.json` 的 `sidebar_expanded.local` 为 False |
| 空歌单时仍有「+」入口 | 分区子节点含 `local-create`(B2)或命中区可点(C) |
| 新建歌单 | 点入口 → 输入名 → 侧栏出现 `("local-playlist", 1)` |
| 拖拽排序 | 两个歌单拖动后 `section_child_ids("local")` 变化,`local_playlist_order` 落盘 |
| 右键「添加到…」 | 构建菜单(把 `QMenu.exec` 猴补成返回指定 action),断言目标歌单 `items` 增加 |
| 重复添加 | 第二次添加不增加条目,状态栏文案为「已在」 |
| 当场新建并加入 | 新建 + 加入后歌单 items 长度为 1 |
| 点开歌单 | `_on_sidebar_item_clicked` 后表格行数 = items 长度,不触发 `run_async` |
| 空歌单空态 | `table_stack.currentIndex() == 1`,提示文案为本地歌单专用 |
| 封面自动取首曲 | `_local_playlist_cover_url` 返回 `items[0].cover_url` |
| 封面手动优先 | 设 `cover_url` 后该函数返回手动值;清空后回自动 |
| 启动预取 | `_sidebar_cover_loader.preload` 被调用且 URL 集合正确(猴补记录) |
| 封面回调点亮 | `_on_sidebar_cover_ready` 后对应歌单项 icon 非空 |
| 最近联动 | 起播本地歌单后 `_recent` 含 `_ListRef("local-playlist", id, name)`,重启后仍在 |

UI 用例的公共写法直接复用 `_make_window` / `_close` /
`_capture_async` 三个 helper 的形态(`tests/test_main_window_layout.py:45-57,863-871`),
在本文件里各复制一份(现有测试文件之间也是各自定义,不共享 helper)。

### 8.3 需要同步修改的既有测试

| 文件 | 位置 | 原因 |
| --- | --- | --- |
| `tests/test_store.py` | `test_other_settings_survive_appearance_save` 的整份断言(`tests/test_store.py:186-205`) | 新增两个设置键后该断言会失败,必须补上 `local_playlist_order` 与 `sidebar_expanded.local` |
| `tests/test_store.py` | `TestSidebarExpandedSetting` 三处字典断言(`tests/test_store.py:313-360`) | 同上 |
| `tests/test_main_window_layout.py` | `test_three_sections_logged_out`(`:83-109`)的 `topLevelItemCount() == 4` 与按索引取项 | 新增分区后变 5;该测试用索引 `(tree.topLevelItem(i) for i in range(4))` 解包,必须改 |
| `tests/test_main_window_layout.py` | `test_loading_placeholders`(`:111-131`)、`test_subscribed_rendered_parallel_to_created`(`:390` 的 `== 5`)、`test_stale_login_clears_subscribed`(`:441` 的 `== 4`) | 同上 |
| `tests/test_main_window_layout.py` | `_make_standalone_tree`(`:562-588`)与 `TestSidebarDropConstraints` | 若 `dropEvent` 不改则无需动;但若新增分区,standalone 树可以顺便加一个 local 分区覆盖拖拽 |
| `tests/test_ui_assets.py` | 若新增图标名,`ICON_NAMES` 参数化测试(`:36-39`)会自动覆盖,无需改 | 仅需保证 SVG 文件存在 |

索引式断言是最大的回归面:新增分区插在「B站」之后、「设置」之前,
所有 `topLevelItem(N)` 的硬编码索引都要复查。建议 M7 顺手把这类断言
改成 `find_header("netease")` 形式(该方法已存在,
`main_window.py:748-755`),减少后续插入分区的成本。这属于可选重构,
不作为 M7 的验收项。

## 9. 开工顺序

拆成 6 步,每步可独立提交、独立测试。

### 步骤 1:数据层(store)

改:
- `src/neriplayer_win/data/store.py`:新增 `_LOCAL_PLAYLISTS_FILE`、
  `SETTING_LOCAL_PLAYLIST_ORDER`、`LOCAL_SONG_SOURCES`、
  `local_playlists_path` 属性、`load_local_playlists` / `save_local_playlists`、
  `_validated_local_playlists`;`_SIDEBAR_SECTIONS` 加 `"local"`;
  `_SETTING_ID_ORDERS` 加新键;`default_settings` 补两项。
- `tests/test_local_playlists_store.py`(新建)。
- `tests/test_store.py`:更新 2.3 节列出的整份断言。

验收:
- 新文件往返一致;损坏/非法输入全部回落空列表;
- `load_settings()` 的默认值包含 `local_playlist_order: []` 与
  `sidebar_expanded.local: true`;
- `pytest tests/test_store.py tests/test_local_playlists_store.py` 全绿。

### 步骤 2:侧栏分区(只读展示)

改:
- `main_window.py`:`_HEADER_SECTION`、`_SECTION_CHILD_KIND`、
  `_rebuild_sidebar`(插入分区,固定 `local-create` 子节点)、
  `_SIDEBAR_KIND_ICON`、`_apply_item_icons`(local-header 单色标)、
  `_on_sidebar_order_changed`(把 `else` 改显式 `elif bili`)、
  `_boot_local_playlists`。
- `i18n.py`:`sidebar.local_playlists`、`sidebar.local_create`。
- 测试:侧栏结构、折叠持久化、拖拽排序、启动加载。

验收:
- 启动后侧栏出现「本地歌单」分区,折叠状态可持久化;
- 手动往 `local_playlists.json` 塞两个歌单,启动后按 `local_playlist_order` 显示;
- 拖拽后顺序落盘,重建不回退;
- 现有 `test_main_window_layout.py` 的索引断言已更新。

### 步骤 3:新建与点开

改:
- `main_window.py`:`_create_local_playlist`、`_show_local_playlist`、
  `_open_list_ref` 加 `local-playlist` 分支、
  `_on_sidebar_item_clicked` 加 `local-playlist` 与 `local-create` 分支、
  `_sidebar_list_ref` 加分支、`_update_table_empty_state` 加空歌单文案。
- `i18n.py`:`local.create_prompt`、`local.created`、`empty.local_playlist`、
  `list.loaded`(复用)。
- 测试:新建、点开、空歌单空态。

验收:
- 点 `local-create` → 输入名 → 侧栏出现新歌单,`local_playlists.json` 落盘;
- 点歌单 → 表格显示其条目,无任何网络请求(`run_async` 未被调用);
- 空歌单显示专用提示。

### 步骤 4:右键「添加到…」

改:
- `main_window.py`:`_on_table_context_menu` 加二级菜单、
  `_add_song_to_local_playlist`、`_menu_local_playlist_actions`。
- `i18n.py`:`play.add_to_menu`、`play.add_to_new`、`local.added`、
  `local.already_added`、`local.already_tag`。
- 测试:添加、重复添加、当场新建并加入、当前视图刷新。

验收:
- 右键任意歌曲 → 「添加到…」→ 选歌单 → 条目落盘;
- 重复添加不产生重复条目,状态栏提示「已在」;
- 「新建并添加」一步完成;
- 若正在浏览该歌单,表格即时刷新。

### 步骤 5:封面

改:
- `main_window.py`:`_sidebar_cover_loader` 实例与接线、
  `_local_playlist_cover_url`、`_on_sidebar_cover_ready`、
  `_boot_local_playlists` 里发起预取、`_rebuild_sidebar` 里设置歌单图标。
- 测试:自动取首曲、启动预取 URL 集合、回调点亮、磁盘缓存命中不触网。

验收:
- 启动后歌单封面逐个亮起(可用离屏测试断言 icon 非空);
- 第二次启动不再发网络请求(磁盘缓存命中,参照
  `tests/test_covers.py:135-156` 的断言方式);
- 无封面歌单回落到 `library_music` 单色图标。

### 步骤 6(进阶):手动封面

改:
- `main_window.py`:歌单右键菜单(侧栏的 `customContextMenuRequested`)、
  封面选择对话框(新文件 `ui/cover_picker.py`,或内联在 main_window)、
  「使用自动封面」项。
- `i18n.py`:`local.cover_menu`、`local.cover_pick`、`local.cover_auto`、
  `local.cover_set`。
- 测试:手动优先、恢复自动、持久化往返。

验收:
- 手动设置后重启仍生效;
- 「使用自动封面」后回落到首曲封面;
- 手动值不因首曲变化而变化(自动值不落盘)。

### 步骤顺序的依赖关系

1 → 2 → 3 是硬依赖(数据 → 展示 → 交互)。4 依赖 3(要能新建歌单)。
5 依赖 2(要有侧栏分区挂图标)。6 依赖 5(手动优先的判定函数在 5 里定义)。
每一步都可以单独合并,合并后应用处于可用状态。
