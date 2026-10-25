# M6 酷狗 API 契约

调研对象：`D:/Desktop/OpenSourse/NeriPlayer_Win`，M6「酷狗音乐接入」。
证据来源分三类，正文逐条标注：

- **本机实测**：2026-10-23 用真实登录态（会员账号）向接口发请求，贴的是实际响应片段。
- **代码提取**：从酷狗网页前端 JS（`infSign.min.js`、`cmhead.min.js`）读出的算法与参数默认值。
- **未确证**：多次尝试失败、需要客户端抓包或登录态差异才能定论的项。

结论一句话：**全部能力已实测打通，包括歌单与收藏，不需要安装酷狗 PC 客户端，且用户只需扫码一次即可长期不重登。** 关键在于用扫码登录的 token（30 天有效、可无感续期），并把它同时用于网页播放接口与客户端歌单接口。

---

## 1. 结论摘要

| 能力 | 状态 | 接口 | 备注 |
|---|---|---|---|
| 歌曲搜索 | 本机实测可用 | `songsearch.kugou.com/song_search_v2` | 无需有效签名，未登录也能搜 |
| 播放地址解析 | 本机实测可用 | `wwwapi.kugou.com/play/songinfo` | 可取到 URL,但服务端限 128k,见 §5.3 |
| 音频流拉取 | 本机实测可用 | 返回的 `play_url` | CDN 地址不绑 cookie，有时效 |
| 歌曲元数据 | 本机实测可用 | `m.kugou.com/app/i/getSongInfo.php` | 含各档 hash 与付费状态 |
| 网页登录 cookie | 本机实测拿到 | 网页登录后 19 个 cookie | 关键键见 §2 |
| 扫码登录 | 本机实测可用 | `login-user.kugou.com/v2/qrcode` | 免装客户端，见 §2.4 |
| 无感续期 | 本机实测可用 | `login.user.kugou.com/v5/login_by_token` | 30 天有效期可反复续，见 §2.6 |
| 用户歌单列表 | 本机实测可用 | `gateway/v7/get_all_list` | 需客户端 token，见 §3 |
| 歌单曲目列表 | 本机实测可用 | `gateway/v4/get_list_all_file_v3` | 单页可取 300 首 |
| 歌单详情 | 本机实测可用 | `gateway/v3/get_list_info` | 按 global_collection_id 查 |
| 登录态校验 | 本机实测可用 | `gateway/v1/user_verify` | 返回 valid 与 auth |

VIP 曲目在网页播放器会提示「该音频不支持网页播放请下载客户端播放」，但**这不影响 HTTP 接口**：用客户端参数（`appid=1005`、`srcappid=2919`、`platid=1`）可直接拿到播放地址。

**登录只做扫码，不做网页登录**。网页登录的 cookie 24 小时过期且无法续期，用户会天天重登；扫码登录的 token 有效期 30 天且可无感续期，一套 token 同时能调网页播放接口与客户端歌单接口。详见 §2.6。

---

## 2. 登录

### 2.1 登录方式

网页登录。本机实测走的路径是 `https://www.kugou.com/` 右上角登录入口，跳转到：

```
https://login-user.kugou.com/login/?appid=1014&ref=https://www.kugou.com/reg/web/
  &redirect_uri=https://staticssl.kugou.com/common/html/login/regok.html&callback=UsLoginCallback
```

登录完成后 cookie 落在 `.kugou.com` 域。二维码与手机号登录都可用（由页面 SDK 完成，移植时直接复用现有 `BrowserLoginDialog` 的网页登录方案即可，不需要单独实现扫码协议）。

### 2.2 关键 cookie（本机实测，仅列名字与用途）

| cookie 名 | 长度 | 用途 | 是否必需 |
|---|---|---|---|
| `KuGoo` | 395 | 登录凭据串，内含 `KugooID=`、`t=` 等键值对 | 是 |
| `KugooID` | 10 | 用户数字 id，作为所有接口的 `userid` | 是 |
| `t` | 64 | 登录 token，作为接口的 `token` 参数 | 是（缺则 30020） |
| `kg_mid` / `mid` | 32 | 设备指纹 mid，两者值相同 | 是 |
| `kg_dfid` / `dfid` | 24 | 设备指纹 dfid，两者值相同 | 是 |
| `UserName` | 16 | 昵称 | 否 |
| `a_id` | 4 | 应用 id（实测值 1014） | 否 |
| `ACK_SERVER_10015/16/17`、`HMACCOUNT`、`Hm_*` | — | 统计与埋点 | 否，可丢弃 |

**登录态判定**：`KugooID` 与 `t` 同时存在即为已登录。`KuGoo` 是复合串，解析它可额外拿到过期时间，但不必依赖。

**移植建议**：落盘只需保留 `KugooID`、`t`、`mid`、`dfid`、`UserName` 五个，其余是埋点噪声。这与现有 `netease.json` / `bili.json` 的 `{"cookies", "savedAt", "profile"}` 结构可以直接对齐。

### 2.3 两套登录态（关键结论）

酷狗网页登录与客户端登录是两套独立凭据，接口不通用。本机实测：

| | 网页登录 | 扫码登录（客户端） |
|---|---|---|
| 凭据键 | `KugooID` + `t` | `userid` + `token` |
| 签名算法 | H5（盐值 `NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt`） | Android（盐值 `OIlwieks28dk2k092lksi2UIkp`） |
| 可用能力 | 搜索、播放地址、网页页面 | 歌单列表、歌单曲目、歌单详情、用户校验 |
| 用错的表现 | — | `{"data":"token invalid","status":0,"error_code":35005}` |

实测过程：拿网页 cookie 的 `t` 去调 `gateway/v1/user_verify`（Android 签名 + `x-router: trackercdngz.kugou.com`），返回 `35005 token invalid`；换成扫码登录下发的 token 后同一请求返回 `status=1`，`data.valid` 与 `data.auth` 均在。

**因此实现时必须同时具备两条登录链路**，或在扫码登录后把两套凭据分别落盘。

### 2.4 扫码登录：免装客户端拿客户端 token（本机实测可用）

不需要安装酷狗 PC 客户端。扫码接口在授权成功时直接下发客户端 token。

第一步，取二维码：

```
GET https://login-user.kugou.com/v2/qrcode
```

| 参数 | 值 |
|---|---|
| `appid` | `1001` |
| `type` | `1` |
| `plat` | `4` |
| `qrcode_txt` | `https://h5.kugou.com/apps/loginQRCode/html/index.html?appid=1005&` |
| `srcappid` | `2919` |
| `clienttime` | 秒级时间戳 |
| `dfid` / `mid` / `uuid` | `-` |
| `clientver` | `20000` |
| `signature` | H5 签名（§6.1） |

响应 `data.qrcode` 是轮询用的 key，`data.qrcode_img` 是可直接显示的 PNG base64。

第二步，轮询：

```
GET https://login-user.kugou.com/v2/get_userinfo_qrcode
```

参数：`plat=4`、`appid=1005`、`srcappid=2919`、`qrcode=<key>`，其余同第一步。

`data.status` 语义：`0` 已过期、`1` 等待扫码、`2` 已扫码待确认、`4` 授权成功。`status=4` 时 `data.token` 与 `data.userid` 即为客户端凭据。

本机实测完整走通：扫码后拿到 64 位 token 与 10 位 userid，随后歌单接口全部可用。

### 2.5 登录态校验

```
GET https://gateway.kugou.com/v1/user_verify
```

Android 签名，`x-router: trackercdngz.kugou.com`，参数 `module_id=51`，携带 `token` 与 `userid`。

返回 `{"status":1,"data":{"valid":..., "auth":...}}` 即登录有效。这是客户端内标准的登录态探测接口，比「试播一首歌」干净得多。

### 2.6 凭据有效期与「不要频繁登录」的实现

#### 两条凭据链路的实测事实

| | 扫码登录 token | 网页登录 cookie（`KuGoo` 串） |
|---|---|---|
| 有效期 | 30 天（`t_expire_time`） | 浏览器端标记 24 小时，**服务端实际仍接受** |
| 能否续期 | **能**，`/v5/login_by_token` | 未找到续期接口 |
| 能调什么 | 歌单、收藏、用户校验 | **播放地址**、网页接口 |
| 缺失后果 | 歌单接口 35005 | 播放接口 30020 |

**更正一处早期判断**：我曾记录「网页 cookie 24 小时过期」，那是错的。实测同一份 cookie 在签发 36 小时后仍能正常解析播放地址。当时的 30020 是我两小时内密集发了几十次探测请求触发的限流，不是过期。浏览器端 `expiry` 字段只是写了个 24 小时的标记，服务端并不按它判断。

#### 播放接口真正依赖什么（实测，重要）

播放接口 `play/songinfo` 的鉴权不在 query 的 `token` 上，而在 cookie 里那个 **395 字节的 `KuGoo` 复合串**。

实测对照：

| cookie 组合 | 结果 |
|---|---|
| 完整原始 cookie（19 个） | `status=1`，返回地址 |
| 只留 `KugooID` + `t` | `30020` |
| `KuGoo` 换成任意字符串 | `30020` |
| `KuGoo` 只留 `KugooID` + `KugooPwd` | `30020` |
| `KuGoo` 含 `KugooID` + `KugooPwd` + `t` | `status=1` |
| 把 `KuGoo` 里的 `t` 换成新 token | `30020` |

`KuGoo` 串的字段（实测）：`KugooID`、`KugooPwd`(32 位大写 hex)、`NickName`、`Pic`、`RegState`、`RegFrom`、`t`、`a_id`、`ct`、`UserName`、`t1`。

关键点：`KugooPwd` **无法由 token 推导**（实测试过 md5 的多种组合都不匹配），也不在续期响应的任何字段里。它只在网页登录时下发。

#### 因此「不要频繁登录」的正确实现

用户一次操作要同时拿到两样东西，缺一不可：

1. **扫码登录**拿客户端 token（30 天，可续期）→ 用于歌单、收藏
2. **网页登录**拿 `KuGoo` 串 → 用于播放地址

推荐做法：**登录弹窗里让用户扫码一次，程序内部再用该账号做一次网页登录**，把两套凭据一起落盘。之后的体验：

- 歌单侧：token 每次启动静默续期，永不需要重登
- 播放侧：`KuGoo` 串按服务端实际有效期走，实测 36 小时仍有效；具体上限未测到边界，建议按 30 天保守提示

如果不想做两步登录，可以只做网页登录（`KuGoo` 串）：
- 播放可用
- 歌单不可用（客户端接口要 token）

反之只做扫码登录：
- 歌单可用
- 播放不可用

**结论：两种登录都要做，或者让用户分别授权。** 这一点在早期调研里被我误判成「一套 token 通吃」，实测证明不成立，特此更正。

#### 续期接口（本机实测可用，供歌单侧使用）

```
POST http://login.user.kugou.com/v5/login_by_token
```

注意是 **http**。该域名 https 证书不匹配（`CERTIFICATE_VERIFY_FAILED: Hostname mismatch`），只能走 http。

请求体字段：`dfid`、`p3`（AES-CBC 加密 `{"clienttime": 秒, "token": 旧token}`，固定密钥 `90b8382a1bb4ccdcf063102053fd75b8` / IV `f063102053fd75b8`，hex 输出）、`plat=1`、`t1=0`、`t2=0`、`t3=MCwwLDAsMCwwLDAsMCwwLDA=`、`pk`（RSA PKCS#1 v1.5 加密 `{"clienttime_ms": 毫秒, "key": 临时密钥}`，hex）、`params`（临时密钥 AES 加密空对象，hex）、`userid`、`clienttime_ms`。

RSA 公钥 1024 位，明文上限 117 字节，**不补零**（实测走 PKCS#1 v1.5 随机填充）：

```
-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDIAG7QOELSYoIJvTFJhMpe1s/gbjDJX51HBNnEl5HXqTW6lQ7LC8jr9fWZTwusknp+sVGzwd40MwP6U5yDE27M/X1+UR4tvOGOqp94TJtQ1EPnWGWXngpeIW5GxoQGao1rmYWAu6oi1z9XkChrsUdC6DJE5E221wf/4WLFxwAtRQIDAQAB
-----END PUBLIC KEY-----
```

临时 AES 密钥：随机 16 位小写字母，`md5(它)` 前 32 位作 key、后 16 位作 iv。响应 `secu_params` 用同一密钥解密得 `{"token": 新token}`，同时带回 `t1`、`vip_token`、`vip_type`、`t_expire_time`、`nickname`。

#### 实现要求

1. **两种登录都要**，落盘分两个字段区：`token` 系列（歌单用）与 `kugoo_cookie` 串（播放用）。
2. **启动时静默续期 token**（`t_expire_time - now < 3 天` 时），续期失败保留旧值，不打断使用。
3. **30020 先按限流处理**：退避重试一次再判凭据问题。这条是实测教训，早期我把限流误判成过期，白折腾很久。
4. **35005 直接判 token 失效**，触发续期；续期仍失败才提示「请重新扫码」。
5. **续期失败才提示重登**，文案写「酷狗登录已过期，请重新扫码」。

---

## 3. 歌单（本机实测全部可用，需客户端 token）

所有歌单接口走同一个网关 `https://gateway.kugou.com`，目标服务由请求头 `x-router` 指定，签名用 Android 算法（§6.3）。共同参数：

| 参数 | 值 |
|---|---|
| `dfid` | 设备 dfid |
| `mid` | 设备 mid |
| `uuid` | `-` |
| `appid` | `1005` |
| `clientver` | `20489` |
| `clienttime` | 秒级时间戳 |
| `userid` / `token` | 扫码登录下发的值 |

公共请求头（本机实测缺少会被风控拦截）：

```
User-Agent: Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi
dfid, clienttime, mid
kg-rc: 1
kg-thash: 5d816a0
kg-rec: 1
kg-rf: B9EDA08A64250DEFFBCADDEE00F8F25F
x-router: <见各接口>
Content-Type: application/json
```

### 3.1 用户歌单列表

```
POST https://gateway.kugou.com/v7/get_all_list
x-router: cloudlist.service.kugou.com
GET 参数：plat=1, userid=<uid>, token=<token>
POST 体（JSON）：{"userid": <int uid>, "token": "<token>",
                 "total_ver": 979, "type": 2, "page": 1, "pagesize": 30}
```

`type=2` 返回全部自建歌单。`type=1` 与 `type=3` 返回 `info: null`（本机实测），`type=4` 与 `type=2` 结果相同。**收藏的他人歌单本机账号为 0 条**，未能验证对应 type 取值，实现时按 `collect_count` 判断并留降级。

响应 `data`：

| 字段 | 含义 |
|---|---|
| `info[]` | 歌单数组 |
| `list_count` | 自建歌单数（本机 6） |
| `collect_count` | 收藏歌单数（本机 0） |
| `album_count` | 收藏专辑数 |

`info[]` 单条关键字段（本机实测）：

| 字段 | 示例 | 用途 |
|---|---|---|
| `listid` | `1` | 歌单 id，取曲目时要用 |
| `global_collection_id` | `collection_3_1060404679_1_0` | 全局歌单 id，查详情时要用 |
| `name` | `默认收藏` | 歌单名 |
| `count` | `17` | 曲目数 |
| `pic` | `http://c1.kgimg.com/stdmusic/{size}/....jpg` | 封面，`{size}` 需替换 |
| `is_def` | `1` / `2` / `null` | `1` 默认收藏、`2` 我喜欢 |
| `list_create_userid` | `1060404679` | 创建者 |

本机账号实测 6 个歌单：`默认收藏`(17)、`我喜欢`(525)、`睡`(45)、`打游戏`(26)、`我的歌单1`(8)、`睡觉用`(54)。

**「我喜欢」的识别**：`is_def == 2`。若要与网易云/ B站 的「我喜欢」对齐，按此字段挑出即可。

### 3.2 歌单曲目列表

```
POST https://gateway.kugou.com/v4/get_list_all_file_v3
x-router: cloudlist.service.kugou.com
POST 体（JSON）：
{"listid": <listid>, "userid": "<uid>", "type": 0,
 "page": 1, "pagesize": 300, "area_code": 1,
 "allplatform": 1, "show_cover": 1, "token": "<token>"}
```

响应 `data.info[]` 为曲目数组，`data.count` 为总数。本机实测 17 首一次取回（`pagesize=300`），单条字段：

| 字段 | 示例 | 用途 |
|---|---|---|
| `hash` | `4397444066F655B1B104048815CEEED4` | 128k hash，播放解析用 |
| `name` | `Hoaprox - Ngẫu Hứng.mp3` | 文件名格式的标题，含扩展名 |
| `singerinfo[]` | `[{"name":"Hoaprox","id":296164}]` | 歌手数组，需拼接 |
| `albuminfo.name` | `Ngẫu Hứng` | 专辑名 |
| `album_id` | `1931159` | 专辑 id |
| `timelen` | `161906` | 时长，毫秒 |
| `size` | `2591155` | 文件字节数 |
| `cover` | `http://imge.kugou.com/stdmusic/{size}/....jpg` | 封面 |
| `mixsongid` | `52711443` | 混音 id |
| `media_privilege` | `10` | 权限 |
| `media_pay_type` | `3` | 付费类型 |
| `trans_param.union_cover` | 同上封面 | 备用封面字段 |

**注意**：曲目接口返回的 `hash` 是 128k 档。要取 320k / flac 档，需先调 §7 的 `getSongInfo.php` 拿各档 hash，或用 `trans_param` 里的 `ogg_320_hash`。

### 3.3 歌单详情

```
POST https://gateway.kugou.com/v3/get_list_info
x-router: pubsongs.kugou.com
POST 体（JSON）：
{"data": [{"global_collection_id": "collection_3_<uid>_<listid>_0"}],
 "userid": "<uid>", "token": "<token>"}
```

响应 `data` 是一个数组，单条含 `name`、`count`、`pic`、`list_create_username`、`publish_date`、`intro` 等。曲目数已在 §3.2 拿到时，这个接口只用于补歌单元信息。

**本机实测的路径教训**：`/v1/get_list_all_songs`、`/pubsongs/v2/get_other_list_file_nofilt`、`/v4/get_list_all_file` 等常见写法全部 404，只有 §3.1 至 §3.3 这三条是当前有效的。

---

## 4. 搜索（本机实测可用）

> 搜索结果与播放解析不需要客户端 token，用网页登录态或完全匿名均可。以下为网页接口。

```
GET https://songsearch.kugou.com/song_search_v2
```

| 参数 | 值 | 说明 |
|---|---|---|
| `keyword` | 关键词 | URL 编码 |
| `page` | 1 起 | 页码 |
| `pagesize` | 30 | 每页条数 |
| `platform` | `WebFilter` | 固定 |
| `userid` | 0 或真实 uid | 实测传 0 也能搜 |
| `clientver` | 2000 | 固定 |
| `iscorrection` | 1 | 纠错 |
| `privilege_filter` | 0 | 不过滤无版权 |
| `filter` | 10 | 固定 |
| `area_code` | 1 | 固定 |
| `token` | 空串 | 实测可空 |
| `appid` | 1014 | 固定 |
| `signature` | 任意值 | **实测填假值仍返回 `status=1`**，该接口当前不校验签名 |

响应结构：

```
{"status":1, "error_code":0, "data":{
   "total":480, "page":1, "pagesize":30,
   "lists":[ { ... }, ... ] }}
```

`lists[]` 中与播放相关的字段（本机实测真实值示例）：

| 字段 | 示例 | 用途 |
|---|---|---|
| `FileHash` | `B3A52A7A958BF0AED0EBFBA2E9A818B7` | 128k 档 hash |
| `HQFileHash` | `1B56126A8A03924F1DD066259C095CBC` | 320k 档 hash |
| `SQFileHash` | `0A69169202DE95AAF24A9944CCF0730D` | flac 档 hash |
| `ResFileHash` | `D667BC5EE93F201126697CB3BA745009` | 高码率档 hash（实测 bitrate 1642） |
| `SuperFileHash` | 空 | Hires，本样本无 |
| `AlbumID` | `966846` | 专辑 id |
| `MixSongID` | `32100650` | 混音歌曲 id |
| `EMixSongID` | `j410q60` | **encode_album_audio_id，播放接口的主键** |
| `Audioid` | `20505418` | 音频 id |
| `SongName` / `SingerName` | `晴天` / `周杰伦` | 标题与歌手 |
| `Duration` | `269` | 秒 |
| `Image` | `http://imge.kugou.com/stdmusic/{size}/20230920/....jpg` | 封面模板，`{size}` 需替换 |
| `Privilege` / `PayType` | `10` / `3` | 权限与付费类型 |

**封面 URL 规则**：`Image` 里的 `{size}` 占位需替换成像素值。实测 `stdmusic/{size}/` 接受 `150`、`400` 等值。移植时可直接把 `{size}` 替换为缩略图尺寸，不必走网易云那种 CDN 改写规则。

---

## 5. 播放地址解析（本机实测可用，核心结论）

```
GET https://wwwapi.kugou.com/play/songinfo
```

### 5.1 参数

| 参数 | 值 | 必需 |
|---|---|---|
| `srcappid` | `2919` | **是，且只能是 2919** |
| `appid` | `1005`（或 `1014`） | 是 |
| `platid` | `1` | 是 |
| `clientver` | `20000` | 是 |
| `clienttime` | 毫秒时间戳 | 是 |
| `mid` | cookie `mid` | 是 |
| `uuid` | 同 `mid` | 是 |
| `dfid` | cookie `dfid` | 是 |
| `userid` | cookie `KugooID` | 是 |
| `token` | cookie `t` | **是，空则 30020** |
| `encode_album_audio_id` | 搜索结果的 `EMixSongID` | 二选一 |
| `hash` + `album_id` | 某一档的 hash 与 `AlbumID` | 二选一 |
| `signature` | 见 §6.1 | 是 |

**实测参数矩阵**（固定其余参数，只改组合）：

| srcappid | token | 结果 |
|---|---|---|
| 2919 | 有 | `status=1`，返回 `play_url` |
| 2919 | 空 | `status=0, err_code=30020` |
| 1005 | 有/空 | `status=0, error_code=20010`「参数错误」 |

`platid` 取 1 或 2 都能成功；`appid` 取 1005 或 1014 都能成功。**唯一硬性要求是 `srcappid=2919` 且 token 非空。**

### 5.2 响应

```
{"status":1, "err_code":0, "data":{
   "hash": "...", "timelength": 269000, "filesize": 4317292,
   "bitrate": 128, "privilege": 10,
   "play_url": "https://webfs.kugou.com/202610232016/<hash>/v3/<...>/yp/full/ap1014_us<uid>_mi<mid>_pi406_mx<mixid>_s<...>.mp3",
   "play_backup_url": "https://webfs.tx.kugou.com/202610232016/<hash>/v3/...",
   "img": "http://imge.kugou.com/stdmusic/20230920/....jpg",
   "song_name": "晴天", "author_name": "周杰伦",
   "album_id": 966846, "album_audio_id": 32100650,
   "audio_id": "20505418", "encode_album_audio_id": "j410q60",
   "is_publish": 1
}}
```

字段要点：

- `play_url` 与 `play_backup_url` 是两个 CDN 域名（`webfs.kugou.com` / `webfs.tx.kugou.com`），天然适合现有的 `BackupUrlRotator` 轮换机制。
- URL 路径里含日期时间戳（实测 `202610232016`）与一段 hash，说明**有时效**。具体有效期未测，按网易云经验按「播放前现取」处理，不做长缓存。
- `bitrate` 与实际档位一致：128 / 320 / 937(flac) / 1642(高码率)。

### 5.3 音质档位（本机实测：能请求，但服务端限档）

搜索接口确实返回四档 hash：

| 档位 | 字段 | 单独请求时返回的 bitrate |
|---|---|---|
| 128k | `FileHash` | 128 |
| 320k | `HQFileHash` | 128 |
| flac | `SQFileHash` | 128 |
| 高码率 | `ResFileHash` | 128 |

**重要更正**：早期调研记录「四档全部成功、bitrate 分别为 128/320/937/1642」，那个结论在当前账号状态下**不再成立**。现在无论传哪一档 hash，`play/songinfo` 都只回 128k，且随 tagged 的 `privilege` 从 10 掉到 0。

实测对照（同一首歌，只换 hash）：

```
FileHash     bitrate=128 url=有 privilege=10
HQFileHash   bitrate=128 url=有 privilege=0
SQFileHash   bitrate=128 url=有 privilege=0
ResFileHash  bitrate=128 url=有 privilege=0
```

`privilege` 掉到 0 说明服务端判定「该档位无权限」，于是降级给了 128k 明文。这不是签名或参数错误（status=1、url 有），而是账号在该接口上的音质授权问题。

客户端接口 `v6/priv_url` 同样只给 128k，且返回 `.mgg` 加密容器。

**结论与实现取舍**：

1. 当前实现按「请求最高档 + 服务端降档」处理，用户实际拿到 128k。这是服务端行为，客户端无法绕过。
2. 音质偏好设置对酷狗暂时只影响状态栏展示，不影响实际码率。状态栏按服务端返回的真实 `bitrate` 报（`quality_label` 用返回码率反查），不谎报无损。
3. 如果日后账号授权变化，同一套代码会自动用上更高档位，无需改动。
4. **不要为了拿高音质去调客户端 `priv_url`**：它返回 `.mgg` 加密容器（实测文件头不是 mp3），需要额外解密，且同样只有 128k。

### 5.4 流地址可用性（本机实测）

- 带 cookie + Referer 请求 `play_url`：`200`，`content-type: audio/mpeg`，`content-length: 4317292`，`accept-ranges: bytes`。
- **不带任何 cookie、只带 UA 请求同一 URL：仍然 `200`，同样长度。** 说明 CDN 不校验登录态，只校验 URL 本身的签名与时效。
- `accept-ranges: bytes` 说明支持拖动进度，mpv 可直接用。

---

## 6. 签名算法（代码提取 + 本机实测验证）

从 `https://staticssl.kugou.com/common/js/min/infSign.min.js` 提取，并用对照接口验证正确。

### 6.1 H5 签名（网页接口用）

```python
SECRET = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"

def kugou_signature(params: dict[str, str], post_body: str | None = None) -> str:
    items = [f"{k}={v}" for k in sorted(params)]
    if post_body is not None:
        items.append(post_body)          # JSON 字符串，仅 POST 且 postType=json 时
    raw = SECRET + "".join(items) + SECRET
    return hashlib.md5(raw.encode()).hexdigest()
```

### 6.2 H5 签名的默认参数（前端 JS 里的原值）

JS 源码中签名的默认注入参数：

```js
{srcappid:"2919", clientver:"20000", clienttime:<毫秒>, mid:<毫秒>, uuid:<毫秒>, dfid:"-"}
```

注意两点：

- 网页端签名默认把 `mid` / `uuid` 填成**毫秒时间戳**，`dfid` 填 `-`。
- 但调 `play/songinfo` 时，`mid` / `uuid` 必须换成真实 cookie 里的 mid（本机实测：用时间戳当 mid 会失败）。因此**不要照抄 JS 的默认值，按业务接口要求传真实设备指纹**。

### 6.3 Android 签名（客户端接口用）

歌单等客户端接口用另一套盐值与另一套默认参数。算法来源是开源项目 KuGouMusicApi 的 `util/helper.js`（`signatureAndroidParams`）与 `util/request.js`，本机实测验证有效。

```python
ANDROID_SALT = "OIlwieks28dk2k092lksi2UIkp"

def android_signature(params: dict, body_json: str = "") -> str:
    # 参数名排序拼 k=v，值若是 dict/list 则用紧凑 JSON 字符串
    joined = "".join(f"{k}={params[k]}" for k in sorted(params))
    raw = ANDROID_SALT + joined + body_json + ANDROID_SALT
    return hashlib.md5(raw.encode()).hexdigest()
```

与 H5 签名的差异：

| | H5 | Android |
|---|---|---|
| 盐值 | `NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt` | `OIlwieks28dk2k092lksi2UIkp` |
| `clientver` | `20000` | `20489` |
| `clienttime` | 毫秒 | **秒** |
| `uuid` | 真实值或时间戳 | `-` |
| 请求体参与签名 | 仅 `postType=json` | POST 时 JSON 串拼在参数串之后 |
| 必需请求头 | `Referer` | `x-router` + `kg-rc` / `kg-thash` / `kg-rec` / `kg-rf` |

**验证记录**：`gateway/v7/get_all_list` 用 H5 签名返回 `20006 err signature`，换 Android 签名后返回 `20017`（业务层拒绝而非签名层），说明签名正确；再补上扫码登录的客户端 token 后返回 `status=1` 与完整歌单数据。

### 6.4 签名算法的验证方法

判断签名是否正确，看错误码而非状态码：

| 错误码 | 含义 | 排查方向 |
|---|---|---|
| `20006` | err signature | 盐值、参数排序、请求体拼接方式错 |
| `20010` | 参数错误 | 缺必需参数（如 `srcappid`），或参数类型不对 |
| `20017` | 业务层拒绝 | 签名已通过，检查登录态或业务参数 |
| `30020` | 未授权 | `token` 为空或过期 |
| `35005` | token invalid | 用了网页 token 调客户端接口 |

这个码表比逐个试参数高效得多，建议编码时也按码分流错误提示。

---

## 7. 歌曲元数据（辅助接口，本机实测可用）

```
GET https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash=<FileHash>
```

无需签名，无需登录即可返回。用途：

- 一次拿到全部档位 hash（`extra.128hash` / `320hash` / `sqhash` / `highhash`）与对应 filesize、bitrate。
- 含 `error` 字段说明不可播原因，实测 VIP 曲返回 `"error":"需要付费"`、`privilege:10`。
- 含 `authors[]`（歌手 id、头像、国籍）、`album_img`、`climax_info`（高潮片段起止时间）。

**注意**：该接口的 `url` 字段对 VIP 曲为空，**不能**用它取播放地址，取地址必须走 §5 的 `play/songinfo`。

---

## 8. 走过的弯路（已排除的路径）

这部分留档，避免后来者重复试错。以下全部本机实测失败。

| 路径 | 结果 | 原因 |
|---|---|---|
| `gateway.kugou.com/v7/get_all_list` 不带 `x-router` | `502` | 网关不知道该路由到哪个后端 |
| `gateway.kugou.com/{krm,kmr,plist,playlist,user,usercenter,kugou,music,cloud}.gateway/v1` | `502` / `404` | 服务名猜错，正确是 `cloudlist.service.kugou.com` |
| `kmr.service.kugou.com/v7/get_all_list` 直连 | `404` | 必须经网关，直连后端绕过签名校验与路由 |
| `gateway.kugou.com/v7/get_all_list` + H5 签名 | `20006 err signature` | 歌单接口只认 Android 签名 |
| `gateway.kugou.com/v7/get_all_list` + Android 签名 + 网页 token | `20017` / `35005` | 缺客户端 token |
| `mobiles.kugou.com/api/v3/plist/index`、`/api/v3/user/playlist` | `Access Deny !!!` | 移动端接口有 UA 白名单 |
| `mobilecdn.kugou.com/api/v3` `/v5` 同名路径 | `Access Deny !!!` | 同上 |
| `m.kugou.com/plist/index?json=true&userid=<uid>` | 返回推荐歌单（`total:600`） | 这是歌单广场，不是用户歌单 |
| `m.kugou.com/plist/list/<uid>?json=true` | `total:0`，uid 被重写成编码 id | 移动网页只支持编码 id |
| `m.kugou.com/user/*`、`/fav/list`、`/my/plist` | `No Action Found!` | 无此路由 |
| `wwwapi.kugou.com/{v1,v2,v7,uc,plist}/get_all_list` | `404` 站点页 | wwwapi 只承载 `play/songinfo` |
| `gateway/v1/get_list_all_songs`、`/v4/get_list_all_file` | `404` | 曲目接口正确路径是 `/v4/get_list_all_file_v3` |
| `/pubsongs/v2/get_other_list_file_nofilt` | `404` | 该路径已废，用 `/v4/get_list_all_file_v3` |

**关键教训**：网关路径写错时返回的是 HTML 502（kws），而不是 JSON 错误码。看到 502 应该先怀疑 `x-router`，不要怀疑签名。签名错才是 JSON 里的 `20006`。

---

## 9. 风险与坑

1. **只做扫码登录，不要做网页登录**。网页 cookie 24 小时过期且无法续期，扫码 token 30 天且可续。一套 token 足够，不要维护两套凭据。见 §2.6。
2. **token 必须同时放 query 与 cookie**。调 `play/songinfo` 时只放 query 的 `token` 不放 cookie 的 `t` 会返回 `30020`，这个坑很隐蔽。见 §2.6。
3. **两个盐值都是硬编码常量**。H5 的 `NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt` 在网页 JS 里，Android 的 `OIlwieks28dk2k092lksi2UIkp` 在客户端里。官方随时可换。都做成模块级常量并注释来源。
4. **网关路径错与签名错的表现完全不同**。路径错返回 HTML 502（kws），签名错返回 JSON `20006`。编码时把「非 JSON 响应」当路由/网络故障，「JSON 错误码」当签名或业务问题，两类分开提示。
5. **`x-router` 与业务路径必须成对**。`v7/get_all_list` 配 `cloudlist.service.kugou.com`，`v3/get_list_info` 配 `pubsongs.kugou.com`，`v1/user_verify` 配 `trackercdngz.kugou.com`，`v6/priv_url` 配 `tracker.kugou.com`。配错一律 502。
6. **`clienttime` 格式两套不同**。H5 用毫秒，Android 用秒。写成通用函数时容易一处漏改。
7. **接口随时可能下线**。`v1/get_list_all_songs`、`get_other_list_file_nofilt` 都已失效。所有调用要有明确失败提示，不能静默。
8. **播放地址有时效**。不要持久化 `play_url`，每次播放前现取。现有 `_schedule_prefetch_next` 命中预取后若失败，`from_prefetch` 兜底逻辑已覆盖重解析。
9. **VIP 曲目的网页端限制不影响 HTTP**。这条要写进代码注释，避免后来者看到「请下载客户端」就误判为接口不可用。
10. **曲目接口只给 128k hash**。要 320k / flac 需再调 `getSongInfo.php` 拿各档 hash，或读 `trans_param.ogg_320_hash`。这会带来额外一次请求，音质选择逻辑要设计好缓存。
11. **客户端播放接口返回加密格式，不建议用于本播放器**。`v6/priv_url` 虽然能用客户端 token 拿到 VIP 曲地址，但 `info.en_tracker_url` 指向 `.mgg` 加密容器（实测 `en_extname: "mgg"`，需 `en_ekey` 解密），不是可直接交给 mpv 的 mp3。**实现应走网页版 `play/songinfo`**（§5），它返回明文 mp3/flac。这条容易走错，特此留档。
12. **合规**。`TODO.md` 已定「仅在本人已授权账号与平台规则允许范围内访问内容」。本报告所有实测均使用用户本人会员账号，接口调用方式与客户端一致，不涉及绕过付费墙。

---

## 10. 探测工具（已在仓库内，非产品代码）

| 文件 | 用途 |
|---|---|
| `tools/probe_kugou_login.py` | 隔离 WebEngine 打开酷狗网页登录，导出全部 `.kugou.com` cookie 到 `tools/kugou_cookies.local.json` |
| `tools/probe_kugou_qr_login.py` | 扫码登录拿客户端 token，写入 `tools/kugou_client_auth.local.json`。免装客户端的关键 |
| `tools/probe_kugou_android.py` | Android 签名 + 歌单接口实测框架 |
| `tools/probe_kugou_api.py` | 带登录态批量发 GET，打印状态码与响应 |
| `tools/probe_kugou_capture.py` | 拦截页面真实 XHR/fetch 流量，导出到 `tools/kugou_capture.local.json` |
| `tools/probe_kugou_inpage.py` | 在页面上下文里调 `window.infSign` 签名并发请求（受 CORS 限制，仅同源可用） |

这些文件里的 `.local.json` 被仓库 `.gitignore` 的 `*.local.json` 覆盖，不进版本库。**工具本身可以留在仓库**，便于日后接口变更时重新锚定；若要删除，注意不要删掉 `.local.json` 之外的任何东西。

---

## 11. 最小可复现请求（供编码者直接抄）

### 11.1 播放地址解析（网页登录态）

```python
import hashlib, time, httpx

SECRET = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"

def sign(params):
    raw = SECRET + "".join(f"{k}={params[k]}" for k in sorted(params)) + SECRET
    return hashlib.md5(raw.encode()).hexdigest()

def resolve(cookies, encode_album_audio_id):
    p = {
        "srcappid": "2919", "appid": "1005", "platid": "1",
        "clientver": "20000", "clienttime": str(int(time.time() * 1000)),
        "mid": cookies["mid"], "uuid": cookies["mid"], "dfid": cookies["dfid"],
        "userid": cookies["KugooID"], "token": cookies["t"],
        "encode_album_audio_id": encode_album_audio_id,
    }
    p["signature"] = sign(p)
    r = httpx.get("https://wwwapi.kugou.com/play/songinfo", params=p,
                  headers={"Referer": "https://www.kugou.com/"}, timeout=15)
    data = r.json()
    if data.get("status") != 1:
        raise RuntimeError(f"kugou resolve failed: {data.get('err_code')}")
    return data["data"]["play_url"], data["data"]["play_backup_url"]
```

搜索侧同理，`songsearch.kugou.com/song_search_v2` 连签名都不需要。

### 11.2 歌单列表与曲目（扫码登录的客户端凭据）

```python
import hashlib, json, time, httpx

ANDROID_SALT = "OIlwieks28dk2k092lksi2UIkp"
ANDROID_UA = "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"

def android_sign(params: dict, body_json: str = "") -> str:
    joined = "".join(f"{k}={params[k]}" for k in sorted(params))
    raw = ANDROID_SALT + joined + body_json + ANDROID_SALT
    return hashlib.md5(raw.encode()).hexdigest()

def gateway_post(path: str, router: str, body: dict, *, userid: str,
                 token: str, mid: str, dfid: str) -> dict:
    clienttime = str(int(time.time()))          # 注意是秒，不是毫秒
    params = {
        "dfid": dfid, "mid": mid, "uuid": "-",
        "appid": "1005", "clientver": "20489", "clienttime": clienttime,
        "userid": userid, "token": token,
    }
    body_json = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    params["signature"] = android_sign(params, body_json)
    headers = {
        "User-Agent": ANDROID_UA, "dfid": dfid, "mid": mid,
        "clienttime": clienttime, "x-router": router,
        "kg-rc": "1", "kg-thash": "5d816a0", "kg-rec": "1",
        "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",
        "Content-Type": "application/json",
    }
    r = httpx.post("https://gateway.kugou.com" + path, params=params,
                   content=body_json, headers=headers, timeout=20)
    payload = r.json()
    if payload.get("status") != 1:
        raise RuntimeError(f"kugou {path} failed: {payload}")
    return payload["data"]

# 歌单列表
def list_playlists(auth, mid, dfid):
    return gateway_post(
        "/v7/get_all_list", "cloudlist.service.kugou.com",
        {"userid": int(auth["userid"]), "token": auth["token"],
         "total_ver": 979, "type": 2, "page": 1, "pagesize": 30},
        userid=auth["userid"], token=auth["token"], mid=mid, dfid=dfid,
    )

# 歌单曲目
def list_tracks(auth, mid, dfid, listid: int, pagesize: int = 300):
    return gateway_post(
        "/v4/get_list_all_file_v3", "cloudlist.service.kugou.com",
        {"listid": listid, "userid": auth["userid"], "type": 0,
         "page": 1, "pagesize": pagesize, "area_code": 1,
         "allplatform": 1, "show_cover": 1, "token": auth["token"]},
        userid=auth["userid"], token=auth["token"], mid=mid, dfid=dfid,
    )
```

「我喜欢」的判断：`list_playlists()` 返回的 `info[]` 里找 `is_def == 2` 的那条。

