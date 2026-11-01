# Android 端流光动态背景（Hyper 背景）实现调研

> 目标：为 Windows 端移植提供可直接翻译的算法说明。所有结论标注来源 `file:line`。
> 参考实现根目录：`D:/Desktop/OpenSourse/NeriPlayer_Win/reference/NeriPlayer-Android/`
> 下文中路径均相对该根目录。

## 概览

流光背景由四个部件组成，缺一不可：

| 部件 | 文件 | 职责 |
|---|---|---|
| Compose 门面 | `app/src/main/java/moe/ouom/neriplayer/ui/view/HyperBackground.kt` | 封面取色、生命周期、帧循环、换歌过渡、缓存 |
| 参数计算 | `app/src/main/java/moe/ouom/neriplayer/ui/view/BgEffectPainter.java` | 把音频 level/beat 换算成 5 个光斑的坐标、半径与各类增益 uniform，编译并持有 `RuntimeShader` |
| 片元着色器 | `app/src/main/assets/shaders/hyper_background_effect.glsl` | 光斑加权混色 + 三层波动形变 + HSV 增益 + 抖动 |
| 帧格 | `app/src/main/java/moe/ouom/neriplayer/ui/animation/AmbientFrameGrid.kt` | 60 Hz 全局帧格，让波形/歌词/背景落在同一批 vsync |

数据流：

```
封面 URL
  → Coil 以 320px 解码、allowHardware(false)
  → androidx Palette（maximumColorCount(16)、clearFilters）
  → 5 个 swatch 角色选择（Base/Accent/Light/Dark/Bridge）
  → softenPaletteColor（HSL 混合、饱和度/明度整形）
  → FloatArray(20) 的 vec4 数组 + lightOffset + saturateOffset
  → BgEffectPainter.setColors / setLightOffset / setSaturateOffset
  → RuntimeShader.setFloatUniform("uColors", ...)
  → 每帧 setAnimTime + setReactive + updateMaterials
  → View.setRenderEffect(RenderEffect.createRuntimeShaderEffect(shader, "uTex"))
```

来源：`HyperBackground.kt:247-285`（取色）、`HyperBackground.kt:399-514`（角色与软化）、`BgEffectPainter.java:90-104`（构造与初始 uniform）、`BgEffectPainter.java:117-128`（每帧更新与 RenderEffect）。

关键点：整个效果是 **RenderEffect 后处理**，不是独立绘制的背景层。宿主是一个透明 `View`，`RenderEffect` 作用于该 View 自身的绘制结果，着色器里用 `uTex` 回采原始内容（Android 传入的是透明内容，因此 `uiColor.a < 0.01` 时输出程序生成的颜色）。见 `HyperBackground.kt:198-210` 与 `hyper_background_effect.glsl:109-116`。

## 提色管线

### 1. 解码与 Palette

```kotlin
ImageRequest.Builder(context)
    .data(coverUrl)
    .size(320)                      // PaletteCoverDecodeSizePx = 320
    .precision(Precision.INEXACT)
    .allowHardware(false)           // Palette 需要 software bitmap
```
来源：`HyperBackground.kt:70`、`HyperBackground.kt:249-261`。

Palette 配置：
```kotlin
Palette.from(bmp)
    .clearFilters()                 // 保留更真实的颜色
    .maximumColorCount(16)
    .generate()
```
来源：`HyperBackground.kt:264-269`。`clearFilters()` 去掉默认的白色/黑色/近灰过滤，避免封面主色被滤掉。

离线模式下若封面是远端 URL，则 `CachePolicy.DISABLED`；否则 `ENABLED`（`HyperBackground.kt:253-260`）。

### 2. 五个颜色角色

角色枚举见 `HyperBackground.kt:80-86`：`Base / Accent / Light / Dark / Bridge`。

**Base**：`mutedSwatch → dominantSwatch → darkMutedSwatch → vibrantSwatch`，取第一个非空非 0（`HyperBackground.kt:471-476`，`pickPaletteColor` 在 `:516-518`）。

**Accent**：候选集为 `vibrantSwatch, lightVibrantSwatch, darkVibrantSwatch, dominantSwatch, mutedSwatch`，按 `colorfulnessScore` 取最大者；分数 < 0.10 时退回 base。若选中的和 fallback 都是无彩色，直接返回；否则过 `ensureAccentColor`。
来源：`HyperBackground.kt:477-484`、`pickAccentPaletteColor` 在 `:520-533`。

`colorfulnessScore = S * (0.45 + min(|L-0.5|, 0.5))`（`HyperBackground.kt:614-618`），即饱和度为主、明度偏离中位为辅。

`ensureAccentColor`：若 `S >= 0.24` 原样返回；否则色相取自身（`S>0.04`）或 fallback（`fallback S>0.04`），都没有就放弃；然后把 `S=0.48`、`L` 夹到 `[0.40, 0.68]`（`HyperBackground.kt:620-640`）。

**Light**：`lightVibrantSwatch → lightMutedSwatch → accentColor → baseColor`（`HyperBackground.kt:485-490`）。

**Dark**：`darkVibrantSwatch → darkMutedSwatch → darkenColor(accentColor, isDark?0.44:0.34) → mutedSwatch → baseColor`（`HyperBackground.kt:491-497`）。`darkenColor` 只降明度不抬升（`HyperBackground.kt:642-647`）。

**Bridge**：`blend(blend(accent, light, 0.50), base, 0.24)`，即先取 accent 与 light 的中点，再向 base 拉 24%（`HyperBackground.kt:498-502`）。

### 3. softenPaletteColor 分支表

函数签名 `softenPaletteColor(rawColor, anchorColor, isDark, role)`，anchor 恒为 baseColor（`HyperBackground.kt:503-509`）。流程：

1. 转 HSL（raw、anchor）
2. 判定色相冲突
3. 按 role 混合 anchor
4. 重新取 HSL，算 `sourceChroma = S * lightnessSpan(L)`
5. 按 role 缩放饱和度、夹上下限
6. 按 role 拉向目标明度、夹明度区间

**色相冲突判定**（`HyperBackground.kt:552-554`）：
```
hueConflict = hueDistance(raw.H, anchor.H) > 105°  &&  raw.S > 0.28  &&  anchor.S > 0.20
```
`StrongHueConflictDegrees = 105f`（`HyperBackground.kt:68`）。

**anchor 混合比**（`HyperBackground.kt:555-561`）：

| Role | 无冲突 | 有冲突 |
|---|---|---|
| Base | 0.08 | 0.08（不区分） |
| Accent | 0.04 | 0.18 |
| Light | 0.08 | 0.24 |
| Dark | 0.22 | 0.42 |
| Bridge | 0.20 | 0.20（不区分） |

**饱和度缩放与上下限**（`HyperBackground.kt:568-586`）：

| Role | scale | max (dark/light) | floor |
|---|---|---|---|
| Base | 0.94 | 0.64 / 0.56 | 见下 |
| Accent | 1.14 | 0.70 / 0.64 | 见下 |
| Light | 1.02 | 0.64 / 0.56 | 见下 |
| Dark | 0.76 | 0.64 / 0.56 | 见下 |
| Bridge | 0.96 | 0.64 / 0.56 | 见下 |

floor 规则（`HyperBackground.kt:578-586`）：
- 若 `blended S < 0.05` 或 anchor 无彩色 → `0f`
- 否则 Dark → `0.10f`，Accent → `0.30f`，其余 → `0.14f`

无彩判定 `isAchromaticCoverColor`：`S * lightnessSpan(L) < 0.06`（`AchromaticChromaThreshold`，`HyperBackground.kt:69`、`:535-539`）。

**目标明度与拉向比例**（`HyperBackground.kt:588-601`）：

| Role | target L (dark / light) | lightnessBlend |
|---|---|---|
| Base | 0.34 / 0.58 | 0.18 |
| Accent | 0.48 / 0.66 | 0.18 |
| Light | 0.62 / 0.82 | 0.24 |
| Dark | 0.18 / 0.34 | 0.34 |
| Bridge | 0.46 / 0.66 | 0.20 |

明度夹取区间：dark `[0.12, 0.66]`，light `[0.28, 0.86]`（`HyperBackground.kt:602-605`）。

**保色度的饱和度重算**（`HyperBackground.kt:606-607`）：
```
chromaPreservingSaturation = sourceChroma / max(lightnessSpan(newL), 0.05)
S = clamp(chromaPreservingSaturation * scale, floor, max)
```
这一步的意义：近黑 swatch 的 HSL 饱和度是噪声（黑体色相不可靠），把「真实色度量」`S*span` 搬到提亮后的明度上重算，避免提亮后颜色发灰。

`lightnessSpan(L) = 1 - |2L - 1|`（`HyperBackground.kt:612`）。

### 4. 转 vec4 与附加偏移

`colorsToShaderVec4Array`：每个颜色展开成 `(r,g,b,1)`，每通道 `(v & 0xFF)/255`（`HyperBackground.kt:649-663`）。5 色共 20 个 float，按 `uColors[5]` 的 vec4 数组传。

两个附加 uniform（`buildDynamicBackgroundShaderPalette`，`HyperBackground.kt:399-418`）：

```kotlin
luma = 0.2126R + 0.7152G + 0.0722B     // colorLuma, :665-670
lightOffset = if (isDark) -0.06 + 0.12*(luma-0.5)
              else         0.08 + 0.10*(0.5-luma)
              .coerceIn(-0.12, 0.12)
saturateOffset = if (isDark) 0.24 else 0.16
```

`lightOffset` 的语义：深色主题下封面越亮，整体越压暗（负向修正）；浅色主题下封面越暗，整体越提亮。范围 ±0.12。

### 5. 兜底

取色失败（异常或 URL 空）时保留上一组颜色，不回退默认背景（`HyperBackground.kt:282-284`）。完全没有调色板时的兜底色 `0xFF808080`（`HyperBackground.kt:67`）。`BgEffectPainter` 自带一套出厂默认 5 色（浅色偏蓝粉米，深色偏青褐），见 `BgEffectPainter.java:60-73` 与 `:159-197`。

## shader 算法逐段

文件：`app/src/main/assets/shaders/hyper_background_effect.glsl`（117 行，AGSL，语法与 GLSL ES 近似）。

### 0. uniform 清单（:1-18）

| uniform | 类型 | 来源 | 含义 |
|---|---|---|---|
| `uResolution` | vec2 | `setResolution` | 视图像素尺寸 |
| `uTex` | shader | `createRuntimeShaderEffect(..., "uTex")` | 被后处理的原始图层 |
| `uAnimTime` | float | `setAnimTime` | 动画秒数，`% 62.831852`（=20π）循环 |
| `uBound` | vec4 | `setBound` | 有效动画矩形 `(x, y, w, h)`，归一化 |
| `uTranslateY` | float | 构造时固定 0 | 纵向平移，未用 |
| `uPoints[5]` | vec3 | `updateAnimatedPoints` | 每点 `(x, y, radius)` |
| `uColors[5]` | vec4 | `setColors` | 每点 RGBA |
| `uAlphaMulti` | float | 构造时固定 1 | 整体透明度 |
| `uSaturateOffset` | float | `setSaturateOffset` | 0.16 或 0.24 |
| `uLightOffset` | float | `setLightOffset` | ±0.12 |
| `uLevelEase` | float | `updateReactiveUniforms` | 音量包络的函数值 |
| `uBeatEase` | float | 同上 | 节拍包络的函数值 |
| `uMotionEase` | float | 同上 | 合成运动强度 |
| `uZoom` | float | 同上 | 缩放系数 ≥ 1 |
| `uColorPulse` | float | 同上 | 颜色脉冲强度 |
| `uGlobalMotion` | vec2 | `updateGlobalMotionUniform` | 全局漂移 |

### 1. 坐标与缩放（:44-54）

```glsl
vec2 vUv = fragCoord / uResolution;
vUv.y = 1.0 - vUv.y;                 // 翻转到数学坐标（左下原点）
vec2 uv = vUv - vec2(0.0, uTranslateY);
vec2 center = vec2(0.5);
uv = (uv - center) / uZoom + center;  // 以中心为原点缩放
```

`vUv` 保留未缩放版本，后续三层波形和顶部渐暗都用 `vUv`，只有光斑距离场用 `uv`。

### 2. 三层波动（:56-63）

三个波形全部用 `sin(2π·...)`，角频率由系数给出：

```glsl
// A. beat 棋盘波：横向 × 纵向的行波相乘
beatWave   = sin((vUv.y + uAnimTime*0.12) * 6.2832)
           * cos((vUv.x - uAnimTime*0.10) * 6.2832);   // 空间频率 1 周期/屏高，时间 0.12 Hz
// B. radial 环波：以中心为原点的同心圆
radialPulse = sin((distance(vUv, center)*5.5 - uAnimTime*0.18) * 6.2832);  // 5.5 环/半屏，0.18 Hz 外扩
// C. ribbon 斜向条带：沿 (1.7, 2.3) 方向的行波
ribbonWave  = sin(((vUv.x*1.7 + vUv.y*2.3) - uAnimTime*0.12) * 6.2832);
```

位移叠加（:60-63）：

```glsl
uv += beatEase   * 0.0110 * vec2(beatWave, -beatWave);                       // 对角推挤，振幅 0.011
uv += beatEase   * 0.0085 * normalize(vUv-center+1e-4) * radialPulse;         // 径向推挤，振幅 0.0085
uv += motionEase * 0.0062 * vec2(ribbonWave, -ribbonWave*0.75);               // 斜向推挤，振幅 0.0062
uv += uGlobalMotion;                                                          // 全局慢漂
```

注意 `normalize(... + 1e-4)` 是为中心像素防除零，加的是常量向量而非长度，中心附近方向会有轻微偏置，属可接受近似。

三层的幅度都不大（合计最大约 0.026 uv），主要作用是让色块边界"呼吸"，不改变构图。

### 3. 有效区域归一化（:65-66）

```glsl
uv.xy -= uBound.xy;
uv.xy /= uBound.zw;
```

`uBound` 来自 `calcAnimationBound`（`BgEffectPainter.java:297-310`）：高度 = `416dp*1.3 + toolbar 高`，转像素后除以父容器高得到 `height2`；竖屏时 `bound = (0, 1-h2, 1, h2)`，横屏时 `bound = ((W-H)/2/W, 1-h2, H/W, h2)`。所以 shader 内的 `uv` 空间是「以屏幕中部一个 540.8dp 高的矩形为 0..1」的坐标系。

### 4. 五个光斑加权混色（:74-91）

```glsl
for i in 0..4:
    pointColor = uColors[i];
    pointColor.rgb *= pointColor.a;              // 预乘 alpha
    point = uPoints[i].xy;
    rad   = uPoints[i].z;
    delta = uv - point;
    weight = 1.0 / (1.0 + dot(delta,delta)/(rad*rad) * 9.3);
    weight *= weight;                            // 平方，衰减更陡
    colorAccum += pointColor * weight;
    weightSum  += weight;

color = colorAccum / max(weightSum, 1e-5);
color.rgb /= max(color.a, 1e-5);                 // 反预乘
```

权重函数等价于 `1 / (1 + 9.3·(d/r)²)²`。在 `d = r` 处权重约为 `1/10.3² ≈ 0.0094`，即半径处衰减到峰值的约 1%。9.3 这个常数决定了光斑的"软边"程度，越大越硬。

归一化用 `weightSum` 而不是固定值，因此颜色是加权平均，不会因为光斑叠加而变亮或溢出。

### 5. 点的运动（Java 侧，`BgEffectPainter.java:239-261`）

shader 里的 `uPoints` 是每帧由 Java 算好的，不是 shader 内算。逐点：

```java
pointOffset = 0.10 + 0.022*levelEase + 0.108*beatEase;          // 摆动幅度
radiusMulti = 1.0  + 0.045*levelEase + 0.220*beatEase;          // 半径放大

x += sin(uAnimTime + baseY) * pointOffset;
y += cos(uAnimTime + x)     * pointOffset;      // 注意用已更新的 x
radius = baseRadius * radiusMulti;

// 节拍推动：从中心向外推
pushX = x - 0.5 + 1e-4;  pushY = y - 0.5 + 1e-4;
pushLen = length(pushX, pushY);
scale = beatEase * 0.118 / pushLen;
x += pushX * scale;   y += pushY * scale;
```

所以每个点的轨迹是「以自身基准位置为圆心的李萨如式椭圆（sin/cos 同相但相位由 y/x 决定）+ 节拍时的径向弹出」。基准位置 `uPoints` 出厂值为（`BgEffectPainter.java:60-66`）：

| # | x | y | radius |
|---|---|---|---|
| 0 | 0.52 | 0.46 | 0.92 |
| 1 | 0.14 | 0.32 | 0.74 |
| 2 | 0.92 | 0.30 | 0.76 |
| 3 | 0.26 | 0.88 | 0.80 |
| 4 | 0.84 | 0.86 | 0.84 |

深色主题下换成一组略调过的坐标（`BgEffectPainter.java:182-188`），差异在 0.02 量级。

注意 `uAnimTime` 在 `HyperBackground.kt:362` 被取模 `62.831852f`（20π），因为 `sin/cos(uAnimTime)` 的周期是 2π，取 20π 是为了浮点精度，视觉上无缝。

### 6. 逐帧的响应量换算（Java 侧，`BgEffectPainter.java:216-237`）

```java
uLevelEase = smoothStep(0.04, 0.82, uMusicLevel);   // 低电平死区，高电平饱和
uBeatEase  = smoothStep(0.03, 0.62, uBeat);
uMotionEase = clamp01(0.42*uLevelEase + 0.82*uBeatEase);
uZoom       = 1.0 + 0.024*uLevelEase + 0.105*uBeatEase;
uColorPulse = clamp01(0.68*uLevelEase + 0.32*uBeatEase);

uGlobalMotion = uMotionEase * 0.0060 * vec2(sin(t*1.9), cos(t*1.6));
```

也就是说 shader 收到的是已经做过 smoothstep 压缩的量，而不是原始 RMS。原始 RMS 在 0.04 以下被完全压成 0，在 0.82 以上饱和到 1，这样安静段不会有噪点抖动，高潮段也不会顶死。

两层平滑要区分：
- `HyperBackground.kt:367-370` 的指数平滑（0.12/0.045 与 0.46/0.12），在 60 Hz 帧率下作用于原始的 level/beat 流，是"攻快释慢"包络。
- `BgEffectPainter` 的 smoothStep 是静态映射，不参与时间平滑。

### 7. HSV 增益（:92-103）

```glsl
hsv = rgb2hsv(color.rgb);

boostedSaturation = hsv.y * (1.16 + 0.06*colorPulse) + 0.030*colorPulse*uSaturateOffset;
darkSaturationLimit = mix(0.66, 0.82, smoothstep(0.34, 0.74, hsv.z));   // 越暗允许越低饱和上限
hsv.y = clamp(min(boostedSaturation, darkSaturationLimit), 0.0, 1.0);

hsv.z = clamp((hsv.z - 0.5) * (1.26 + 0.06*colorPulse) + 0.5 + 0.018*colorPulse, 0.0, 1.0);

color.rgb = hsv2rgb(hsv);
color.rgb += 0.010 * colorPulse * uLightOffset;                          // 光脉冲微提亮
color.rgb *= mix(0.68, 1.10, smoothstep(0.10, 0.92, vUv.y));             // 自下而上渐亮
color.rgb = clamp(color.rgb, 0.0, 1.0);
```

三个效果：
1. **饱和度提升**：基础 1.16 倍，脉冲最高 +6%；同时用 `darkSaturationLimit` 限制暗部饱和度上限（0.66 到 0.82 随明度插值），避免暗区颜色"过艳显脏"。
2. **对比度提升**：围绕 0.5 中位做 1.26 倍拉伸，脉冲时 +6%，再加 `0.018*colorPulse` 的绝对提亮。
3. **纵向渐变**：`vUv.y` 从 0（底）到 1（顶），底部乘 0.68，顶部乘 1.10，形成"上亮下暗"的立体感。

`uSaturateOffset` 和 `uLightOffset` 这两个由封面亮度算出的偏移只在脉冲项里参与（0.030 与 0.010 的系数），本身幅度很小，起微调作用。

### 8. 透明度与合成（:105-116）

```glsl
color.a = clamp(color.a, 0.0, 1.0);
color.a *= uAlphaMulti;                                    // 恒为 1

vec4 uiColor = uTex.eval(vec2(vUv.x, 1.0-vUv.y) * uResolution);   // 回采原始 UI

float dither = (gradientNoise(fragCoord.xy) - 0.5) * (5.0/255.0);
color.rgb = clamp(color.rgb + dither, 0.0, 1.0);

vec4 fragColor = (uiColor.a < 0.01) ? color : uiColor;     // UI 不透明处保留 UI
return vec4(fragColor.rgb * fragColor.a, fragColor.a);     // 预乘输出
```

`gradientNoise` 是标准的一维哈希抖动（:38-41）：
```glsl
fract(52.9829189 * fract(dot(uv, vec2(0.06711056, 0.00583715))))
```
幅度 `±2.5/255`，用于打散 8bit 输出时的色带。这在渐变面积大、色阶过渡缓的场景里很关键，移植时不要省。

最终输出是预乘 alpha，因为 Android 的 `RenderEffect` / `RuntimeShader` 输出通道约定如此。如果移植到 HLSL，需要按目标后端的约定改成直通 alpha 或预乘。

### 9. 可直接翻译的伪代码

```text
function render(fragCoord, resolution, tex, uniforms):
    vUv = fragCoord / resolution
    vUv.y = 1 - vUv.y

    uv = vUv
    uv = (uv - 0.5) / uZoom + 0.5

    t = uAnimTime
    beatWave    = sin((vUv.y + t*0.12) * TAU) * cos((vUv.x - t*0.10) * TAU)
    radialPulse = sin((distance(vUv, 0.5) * 5.5 - t*0.18) * TAU)
    ribbonWave  = sin(((vUv.x*1.7 + vUv.y*2.3) - t*0.12) * TAU)

    uv += uBeatEase   * 0.0110 * vec2(beatWave, -beatWave)
    uv += uBeatEase   * 0.0085 * normalize(vUv - 0.5 + 1e-4) * radialPulse
    uv += uMotionEase * 0.0062 * vec2(ribbonWave, -ribbonWave*0.75)
    uv += uGlobalMotion

    uv = (uv - uBound.xy) / uBound.zw

    accum = 0; wsum = 0
    for i in 0..4:
        pc = uColors[i]
        pc.rgb *= pc.a
        d  = uv - uPoints[i].xy
        r2 = max(uPoints[i].z^2, 1e-4)
        w  = 1 / (1 + dot(d,d)/r2 * 9.3)
        w  = w * w
        accum += pc * w
        wsum  += w

    col = accum / max(wsum, 1e-5)
    col.rgb /= max(col.a, 1e-5)

    hsv = rgb2hsv(col.rgb)
    sat = hsv.y * (1.16 + 0.06*uColorPulse) + 0.030*uColorPulse*uSaturateOffset
    limit = mix(0.66, 0.82, smoothstep(0.34, 0.74, hsv.z))
    hsv.y = clamp(min(sat, limit), 0, 1)
    hsv.z = clamp((hsv.z - 0.5)*(1.26 + 0.06*uColorPulse) + 0.5 + 0.018*uColorPulse, 0, 1)
    col.rgb = hsv2rgb(hsv)
    col.rgb += 0.010 * uColorPulse * uLightOffset
    col.rgb *= mix(0.68, 1.10, smoothstep(0.10, 0.92, vUv.y))
    col.rgb = clamp(col.rgb, 0, 1)

    src = tex.sample(vUv.x, 1 - vUv.y)
    col.rgb += (hash(fragCoord) - 0.5) * (5/255)
    col.rgb = clamp(col.rgb, 0, 1)

    out = (src.a < 0.01) ? col : src
    return premultiply(out)      // out.rgb * out.a, out.a
```

### 10. AGSL 与 GLSL/HLSL 的语法差异

移植需要注意的点：
- AGSL 的入口是 `vec4 main(vec2 fragCoord)`，不是 `main()` + `gl_FragCoord`。`fragCoord` 是像素坐标，原点在左上（因此 shader 里做了一次 `1-y`）。
- `uniform shader uTex` 是 AGSL 特有，等价于「同尺寸的输入纹理 + 采样器」，用 `uTex.eval(pixelCoord)` 采样。移植到 HLSL 时换成 `Texture2D` + `SamplerState`。
- `uniform vec4 uColors[5]` 的数组 uniform 在 AGSL 里通过 `setFloatUniform(String, float[])` 传入摊平的 20 个 float。HLSL 常量缓冲里同样按 float4 数组声明。
- 输出必须预乘。Direct2D/D3D 的 `ID2D1Effect` 直出若用直通 alpha，需在最后 `rgb /= a` 或改混合状态。

## 帧调度与性能

### 帧格

`AMBIENT_ANIMATION_FRAME_INTERVAL_NANOS = 1_000_000_000L / 60L`（`ui/animation/AmbientFrameGrid.kt:8`），即 16 666 666 ns 一格。`ambientFrameSlot(t, interval)` 是整除取商（`AmbientFrameGrid.kt:10-11`），`isNewAmbientFrameSlot` 比较当前帧与上一渲染帧的格号是否不同（`:14-21`）。

用绝对帧时间分格，而不是定时器回调或累加计数，原因有三条：

1. 时间基准直接来自 `withFrameNanos` 传入的 vsync 时间戳，动画秒数由时间戳差值算出（`HyperBackground.kt:360-362`）。丢帧或迟一帧只是跳过一格，相位不会累积漂移，也不需要补偿上一帧的延迟。
2. 所有装饰性动画的格长都是这个基准的整数倍：歌词插值默认 2 格、低功耗 4 格（`ui/component/lyrics/LyricPlaybackPosition.kt:21-22`），歌词滚动与波形各 1 格（`ui/component/lyrics/SyncedLyricsView.kt:535`、`ui/component/playback/WaveformSlider.kt:166`）。同相位意味着它们落在同一批 vsync，整窗口按最快那个的 60 fps 出帧，而不是几条互不相干的节奏把每一帧都占满（设计意图见 `AmbientFrameGrid.kt:3-7` 的注释）。
3. 首帧特例：`lastRenderedFrameNanos == 0L` 时直接返回 true（`AmbientFrameGrid.kt:19`）。0 是「还没渲染过」的哨兵值，而首帧时间戳很小、格号也是 0，若不特判，第一格会被判成「与上一帧同格」而跳过，视图要等到下一格才拿到颜色。

### 60 fps 上限与高刷屏

60 Hz 屏上每格正好一个 vsync。120 Hz 屏 vsync 周期 8.33 ms，一格跨两个 vsync，`t / 16 666 666` 的边界多数落在第二个 vsync 上，取整偏差会让个别格跨 1 个或 3 个 vsync，所以实际出帧在 60 fps 量级、间距轻微不均。高刷屏只用来提滚动与触摸的顺滑度，背景不上 120，这是有意的功耗取舍。

### 每帧的 CPU 侧工作

`while (isActive) { withFrameNanos { ... } }`（`HyperBackground.kt:353-389`），进入新格后依次执行：

| 调用 | 执行条件 | 内部跳过逻辑 |
|---|---|---|
| `painter.setAnimTime(seconds % 62.831852f)` | 每格 | 无，只写一个字段（`BgEffectPainter.java:130-132`） |
| `painter.setReactive(smoothLevel, smoothBeat)` | 每格 | 先 `Float.compare` 两个值，与上一帧完全相同就直接返回，只置脏标记（`:106-115`） |
| `painter.updateMaterials()` | 每格 | `uAnimTime` 必写；`updateReactiveUniforms` 仅在脏时重算（`:117-124`）；`updateGlobalMotionUniform` 在 `uMotionEase == 0` 且上帧漂移已为 0 时提前返回（`:230-237`）；`updateAnimatedPoints` 每帧都算 5 个点的 sin/cos 与径向推出（`:239-261`） |
| `painter.setResolution(w, h)` | 仅宽高变化 | 外层比 `lastRenderWidth/Height`（`HyperBackground.kt:375-383`），painter 内再比一次（`BgEffectPainter.java:200-207`） |
| `v.setRenderEffect(painter.renderEffect)` | 每格 | `getRenderEffect()` 每次新建一个 `RenderEffect` 实例（`:126-128`），不复用 |
| `v.invalidate()` | 每格 | 见下 |

安静段的省电路径：`setReactive` 值不变，响应 uniform 不重算；`uMotionEase == 0`，全局漂移早退。剩下的固定开销是 5 次 `sin/cos` 加一次全屏片元 pass。

另有两条一次性开销：`BgEffectPainter` 在 `Dispatchers.IO` 上构造（`HyperBackground.kt:178-183`），shader 编译不占主线程；`awaitViewReady` 用空的 `withFrameNanos` 轮询，直到视图 attach、已布局且宽高非 0（`:225-234`）。

### 音频包络的取样与归一化

`PlayerManager.audioLevelFlow` 与 `beatImpulseFlow` 只是 `AudioReactive.level` / `AudioReactive.beat` 的转发（`modules/playback/runtime/src/main/java/moe/ouom/neriplayer/core/player/PlayerManager.kt:739-740`），真实计算在 `modules/playback/logic/src/main/java/moe/ouom/neriplayer/core/player/audio/reactive/AudioReactive.kt`。

取样在音频管线上而不是在 UI 侧：`AudioReactive` 提供一个 `TeeAudioProcessor.AudioBufferSink`（`AudioReactive.kt:88-103`），ExoPlayer 把 PCM buffer 分一份过来，`handleBuffer` 直接对整段 buffer 求 RMS（`:100-102`）。`readPcmLevel` 按 `encoding` 分派到 `rms8 / rms16 / rms24 / rms32 / rmsFloat`（`:128-140`），每种都把样本归一化到 ±1 再平方求和开方。buffer 不按帧切块，一次算一个 buffer 的 RMS，所以取样率由音频 buffer 大小决定（通常几十毫秒一次），远低于 60 fps 帧率。

归一化分四层：

1. 音量增益：`rawLevel * effectiveVolume`（`effectiveVolume` 夹到 `[0,1]`），再 `coerceIn(0,1)`（`:123-125`）。静音或低音量时画面跟着安静。
2. 感知压缩：`perceptual = sqrt(lvl)`（`:162`），把 RMS 从能量域拉到近似感知域，低声压不再被压成一条直线。
3. 节拍检测：两条 EMA，`aFast = 0.5`、`aSlow = 0.05`（`:143-146`），`delta = max(0, emaFast - emaSlow)`，再算自适应噪声地板 `noiseEma = 0.02*delta + 0.98*noiseEma`（`:149`），阈值 `3*(noiseEma + 1e-9)`（`:150`）。超阈值且距上次节拍超过 `MIN_BEAT_GAP_NS = 120ms`（`:67`）时把 `_beat` 置 1（`:153-157`），否则按 `0.90^(elapsed/16.67ms)` 衰减（`:68-69`、`:181-191`）。节拍命中时 `level` 额外抬 `+0.08`（`:163`）。
4. `AudioReactive.enabled` 为假时两个流都归零（`:55-65`），关闭入口在 `NeriAppPresentationEffects.kt:79-81`：`showNowPlaying && audioReactiveEnabled && lifecycleResumed`，离开播放页或退到后台立即停掉。

注意节拍强制把音频管线从 offload 拉回 PCM：`addOutputRequirements` 把 `audioReactiveActive` 作为一项 PCM 需求（`modules/playback/logic/.../policy/offload/PlaybackAudioOffloadPolicy.kt:38`），所以开着音频联动会失去硬件直通省电。这条对 Windows 端同样成立：要拿 PCM 就得走软件解码或额外分流。

HyperBackground 侧只做最后一段包络：`levelRate` 上行 0.12、下行 0.045，`beatRate` 上行 0.46、下行 0.12（`HyperBackground.kt:367-370`），节拍取「当前值」与「本帧内累计峰值」的较大者再乘 0.94（`:365`），峰值消费后清零（`:366`）。攻快释慢，节拍不会瞬间消失。

### GPU 侧

`RenderEffect.createRuntimeShaderEffect(shader, "uTex")`（`BgEffectPainter.java:126-128`）是渲染节点上的后处理，作用于宿主 `View` 自身的绘制结果（`HyperBackground.kt:198-210`，View 透明且 `setWillNotDraw(false)`）。`uTex` 回采的就是这个 View 本帧已经光栅化的内容，因此不存在额外的一张离屏纹理：应用侧没有分配第二块 surface，代价是一次全屏片元 pass。若换成独立绘制层（先画背景层，再与窗口内容合成），就会多一层 surface 和一次合成。

### invalidate 的取舍

帧末用 `v.invalidate()`（`HyperBackground.kt:386-387`），注释写明 `postInvalidateOnAnimation` 会「再排一帧，整窗口出帧翻倍」。`withFrameNanos` 的回调在帧的绘制遍历之前，`invalidate()` 让这个 View 在本次绘制里重画；`postInvalidateOnAnimation` 则向下一帧投递一次新的失效，于是每个动画格都额外产生一个窗口帧。动画时间取自绝对时间戳，晚一帧重画只影响延迟，不影响相位，所以这里选前者。

### 停帧与复位

帧循环整体包在 `repeatOnLifecycle(Lifecycle.State.RESUMED)` 里（`HyperBackground.kt:333`），退到后台协程被取消，`finally` 中取消两个 collect job（`:390-393`）；`DisposableEffect` 销毁时 `setRenderEffect(null)`（`:212-222`）。

移植要注意：`startNs`、`smoothLevel`、`smoothBeat` 都是 `repeatOnLifecycle` 块内的局部变量（`:337-340`），每次回到前台重新进入该块都会从 0 开始，动画时间归零、包络重置，回前台的第一帧是相位跳变而不是接着走。

## 换歌过渡

### 两段式状态

颜色不是「一份当前值」，而是目标和生效两份：

- `targetShaderPalette` / `targetShaderPaletteIdentity`：提色完成后的目标，以及它对应的请求标识（`HyperBackground.kt:190-193`）。
- `activeShaderPalette`：画面上此刻真正在用的值，插值结束后才更新（`:194-196`、`:300-307`）。

请求标识 `coverRequestIdentity` 由 `coverIdentityKey | coverUrl | refreshKey | offlineMode | isDark` 拼成（`:161-171`）。这五项任一变化都算一次新请求，所以换歌、重取封面、切明暗都会走一条新的提色路径，同一首歌重复进入则命中缓存。

### 触发条件

`LaunchedEffect` 的键是 `(painter, hostView, shaderInitialized, targetShaderPalette, targetShaderPaletteIdentity, coverRequestIdentity)`（`:287-294`），函数体内有三重门：

```kotlin
val target = targetShaderPalette ?: return@LaunchedEffect
if (targetShaderPaletteIdentity != coverRequestIdentity) return@LaunchedEffect
if (painter == null || !shaderInitialized) return@LaunchedEffect
```
（`HyperBackground.kt:296-299`）

第二行是防串色的关键：提色是异步的，用户在解码完成前又切了一首，旧请求的结果可能晚到。只有「目标色板所对应的标识」等于「当前请求标识」才允许上屏，否则丢弃，等新请求自己走完。提色侧也有一次同样的检查，`shouldCommitDynamicBackgroundPalette(requestIdentity, latestCoverRequestIdentity)` 不通过就直接 return，连缓存都不写（`:270-272`、`:75-78`）。

### 首次与应用

`animateDynamicBackgroundPalette(from = activeShaderPalette, to = target)`（`:300-305`）。`from` 为 null（首次进入、切换明暗后重建 painter）或长度不一致时，不插值，直接 `setColors` / `setLightOffset` / `setSaturateOffset` 并 `postInvalidateOnAnimation`（`:427-433`）。

否则走 520ms 插值。时长常量 `DynamicBackgroundPaletteTransitionDurationMs = 520L`（`:71`）。

### 插值过程

（`HyperBackground.kt:435-465`）

```kotlin
rawFraction = (frameNanos - startNanos) / (520 * 1e6)
fraction    = smoothStep01(clamp01(rawFraction))        // t*t*(3-2t)
lerpFloatArrayInto(startColors, to.colors, fraction, interpolatedColors)  // 20 个 float 逐元素
painter.setColors(interpolatedColors)                    // 复用同一个数组，避免每帧分配
painter.setLightOffset(lerpFloat(from.lightOffset, to.lightOffset, fraction))
painter.setSaturateOffset(lerpFloat(from.saturateOffset, to.saturateOffset, fraction))
view.invalidate()
finished = rawFraction >= 1f
```

几个细节：

- 插值也走帧格：`rawFraction < 1f` 时用 `isNewAmbientFrameSlot` 节流（`:447-449`）；最后一帧（`rawFraction >= 1f`）不再节流，保证一定收尾。
- 插值的是 20 个 float 的调色板数组，即 RGBA 线性插值。着色器里的 HSV 增益是在插值之后逐帧算的，所以过渡过程会经过中间饱和度与明度，而不是绕道色相环。两端色相差很大时会短促地经过一段「灰」的中间态，520ms 加 smoothStep 之后几乎看不出。
- `lightOffset` 和 `saturateOffset` 同步插值，否则颜色渐变期间亮度修正会突变。
- `interpolatedColors` 在循环外只分配一次（`:436`），每帧原地覆写。
- 结束后再设一次精确终值并 `postInvalidateOnAnimation`（`:460-463`），避免最后一帧四舍五入留下微小偏差。

### 失败时

三种失败都不改颜色：

| 情况 | 行为 | 位置 |
|---|---|---|
| 提色抛异常 | 只记注释，保留上一组颜色，不回退默认背景 | `HyperBackground.kt:280-284` |
| 解码结果不是 `SuccessResult` | `?: return@LaunchedEffect` | `:263` |
| 请求已过期 | 丢弃结果，不写缓存 | `:270-272` |

`catch` 里单独先 `throw e` 处理 `CancellationException`（`:280-281`），保证协程取消不被当成提色失败吞掉。

### 缓存的角色

`LruCache<String, DynamicBackgroundShaderPalette>(32)`（`:138-140`）缓存的键就是 `coverRequestIdentity`，值只含 20 个 float 加两个偏移，不含 bitmap。命中时 `LaunchedEffect(coverRequestIdentity)` 直接返回（`:242-246`），`initialPalette` 也从这里取（`:173`），所以旋转屏幕重建视图不必重新解码封面。

重建时还有一条恢复路径：`shaderInitialized` 为假时，用 `targetShaderPalette.takeIf { targetShaderPaletteIdentity == latestCoverRequestIdentity } ?: cache.get(latestCoverRequestIdentity)` 取颜色直接设上（`:317-325`），这是不走插值的直设，属于同一套「首次不插值」规则。

### 图层上的位置

唯一的正式调用点是 `NowPlayingDynamicBackdrop`（`ui/playback/visual/NowPlayingBackdropLayers.kt:132-142`），宿主 `graphicsLayer { alpha = 0.80f }`，`isDark = true` 写死，`coverIdentityKey = songKey`，`refreshKey = coverAssetRefreshKey`，`offlineMode` 透传。`NowPlayingBackdropLayers` 在封面模糊背景和动态背景之间二选一（`:46-50`），所以两者不会同时存在。

## 降级策略

按从硬到软排，每一层都有独立判据，不靠单一开关兜底。

### 1. 平台门槛

`if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return`（`HyperBackground.kt:156`），API 33 以下整个 Composable 直接返回，一个 View 都不创建，`HyperBackgroundCompatibilityTest` 在 sdk 28 上断言 `runtime-shader-view` 节点不存在（`app/src/test/java/moe/ouom/neriplayer/ui/view/HyperBackgroundCompatibilityTest.kt:26-40`）。`BgEffectPainter` 本身带 `@RequiresApi(TIRAMISU)`（`BgEffectPainter.java:49`）。低版本没有 RuntimeShader，也没有可靠的路径在 Compose 里做等价后处理，所以是「没有背景」而不是「降级成纯色」。

设置页也同步这条限制：`dynamicBackgroundApiAvailable = Build.VERSION.SDK_INT >= TIRAMISU`（`SettingsMotionSection.kt:155`），API 不足时把开关置灰、加 `settings_android13_required` 说明，并在 `LaunchedEffect` 里把已存的 `nowPlayingDynamicBackgroundEnabled` / `audioReactiveEnabled` 写回 false（`:169-176`）。开关值被主动纠正，而不只是界面禁用，避免升级系统后状态对不上。

### 2. shader 初始化失败

shader 编译和首帧初始化包在 `try { ... } catch (_: Throwable) { return@LaunchedEffect }` 里（`HyperBackground.kt:315-329`）。`showRuntimeShader`、取缓存色板、`setRenderEffect` 任一环节抛异常，协程直接结束，`shaderInitialized` 保持 false，宿主 View 保持全透明，UI 正常显示，只是没有背景。`loadShader()` 里读 assets 失败也只记日志，返回空字符串，异常会在 `new RuntimeShader(...)` 处抛出，最终落进同一个 catch（`BgEffectPainter.java:272-284`）。

拆除时 `runCatching { view?.setRenderEffect(null) }.onFailure { NPLogger.w(...) }`（`HyperBackground.kt:214-221`），清理失败也不影响页面销毁。

### 3. 封面缺失

`if (... || coverUrl.isNullOrBlank()) return@LaunchedEffect`（`:239-241`），没有封面就不发请求，画面维持出厂 5 色或上一组颜色。

### 4. 提色失败

见「换歌过渡」：异常、非 SuccessResult、请求过期三种情况都保留上一组颜色（`:263`、`:270-272`、`:280-284`），不回退 `DynamicBackgroundFallbackColor = 0xFF808080`（`:67`）那类灰色底。

### 5. 离线模式

`networkCachePolicy` 在「offlineMode 且 URL 是远端源」时设为 `CachePolicy.DISABLED`，否则 `ENABLED`（`:253-260`）。判定用 `isRemoteImageSource(coverUrl)`，本地文件不受影响。这里禁的是网络缓存策略，本地磁盘缓存仍可命中，所以离线时已经看过的封面照样能提色。

### 6. 与封面模糊背景互斥

`resolveNowPlayingBackgroundMode(dynamicRequested, coverBlurEnabled, audioReactiveRequested)` 返回 `dynamic = dynamicRequested && !coverBlurEnabled`、`audioReactive = audioReactiveRequested && dynamic`（`ui/NeriAppPresentationEffects.kt:28-34`）。封面模糊开启时动态背景和音频联动都被压掉。设置页还有一层同类约束：`dynamicBackgroundAvailable = dynamicBackgroundApiAvailable && !nowPlayingCoverBlurBackgroundEnabled`（`SettingsMotionSection.kt:180-181`），并且开封面模糊会主动关掉另外两个开关（`:198-209`）。`EnforceNowPlayingBackgroundExclusion` 处理持久化状态里已有的冲突组合（`appearance/SettingsBackgroundAppearanceController.kt:180-205`）。

### 7. 性能侧的自然降级

安静时 `uLevelEase = uBeatEase = 0`，`setReactive` 值不变使得响应 uniform 不重算，`updateGlobalMotionUniform` 提前返回，只剩 `uPointOffset = 0.10` 的常驻漂移（`BgEffectPainter.java:78`、`:240`）和 5 次三角运算。这不算主动降级，但意味着没有音频时开销本身就很低。

## 移植要点清单

Windows 端的对应手段取自 `docs/research/M9-win-ambient-background.md` 已确认的约束：PySide6 下限 6.6（`QRhiWidget` 需 6.7）、无 OpenGL 显式依赖、Nuitka 打包按 import 图收集、常驻内存 ≤ 200MB、UI 线程零阻塞。桌面侧现有渲染原语只有 `QPainter` 自绘（`ui/player_bar.py:199`、`ui/main_window.py:798`），没有现成的 GPU 通道。

| 能力 | Android 实现 | Windows 端对应手段 | 是否可省 |
|---|---|---|---|
| 5 点加权混色 | 片元着色器内 5 次循环，权重 `1/(1+9.3(d/r)²)²` 后按 `weightSum` 归一（`hyper_background_effect.glsl:74-91`） | 必须走片元着色器：`QOpenGLWidget` + GLSL，或抬 PySide6 下限到 6.7 用 `QRhiWidget`。`QRadialGradient` 叠加只能做加色混合，拿不到加权平均的归一化，重叠区会过曝 | 不可省，是视觉主体 |
| 三层波形扰动 | beat 棋盘波、radial 环波、ribbon 斜波，逐层乘 `uBeatEase` / `uMotionEase` 推挤 uv（`glsl:56-63`） | 同一着色器里照抄，成本几乎为零。纯 CPU 路线只能退化为整层平移加缩放，环波与斜波做不出来 | 可省两层，只留 radial 或 ribbon；幅度合计仅 0.026 uv |
| HSV 增益 | 饱和度 ×1.16、暗部饱和上限 `mix(0.66,0.82,·)`、明度绕 0.5 拉伸 ×1.26、底部压到 0.68（`glsl:92-103`） | 着色器内实现。CPU 路线可在取色阶段对 5 个色做同样的 HSV 变换再喂给 gradient，但纵向渐变仍要靠叠加一层线性渐变 | 不可省，省掉后色块发灰、发脏 |
| 颗粒抖动 | `gradientNoise` 一维哈希，幅度 ±5/255（`glsl:38-41`、`:112-113`） | 着色器内实现最省。CPU 路线可预生成一张噪声 tile 平铺叠加，一次生成长期复用 | 可省，代价是大面积渐变出现色带 |
| 调色板提色与软化 | androidx Palette 16 色 + 5 角色选择 + `softenPaletteColor` 的 HSL 整形（`HyperBackground.kt:247-285`、`:467-514`、`:541-610`） | 桌面侧 `cover_seed.py` 只有单一主色（5bit 直方图 + HSL 打分）。要么扩成 5 色，要么从 `material_color.py` 的 `tonal_spot_palette` 派生 5 色（零新增依赖，且与 QSS 主题同源） | 5 色不可省；`softenPaletteColor` 整套可省，用简化版（夹饱和度、拉明度）即可 |
| 520 ms 过渡 | 帧循环内逐帧 `smoothStep01` + 数组逐元素 lerp，`lightOffset`/`saturateOffset` 同步（`HyperBackground.kt:421-465`） | `QVariantAnimation`（520ms，`QEasingCurve::InOutQuad`）驱动一个插值器，或自建 QTimer 帧驱动。颜色数组插值逻辑可直接照抄 | 不可省，切歌硬切观感很差 |
| 60 fps 帧格 | `withFrameNanos` + 绝对时间整除分格（`AmbientFrameGrid.kt:8-21`），与歌词、波形同相位 | `QTimer` 16ms 足够，Qt 定时器不保证与 vsync 对齐，但桌面侧没有「多个动画共享 vsync」的诉求，相位抖动不可见 | 可省，用普通定时器即可；不要在 `paintEvent` 里再投递重绘 |
| 音频电平与节拍响应 | ExoPlayer `TeeAudioProcessor` 分流 PCM，RMS + 双 EMA + 自适应阈值检测节拍（`AudioReactive.kt:88-191`） | libmpv 没有等价 PCM tee。要做得改音频输出链（`ao=pcm` 分流或 audio filter），会牵动 offload 与延迟；Android 侧这条也是独立开关并强制放弃 offload | 可省，桌面端可只保留时间驱动的漂移；若要加，作为独立可选功能 |
| 后处理 vs 独立绘制层 | `RenderEffect.createRuntimeShaderEffect` 作用在透明宿主 View 自身，`uTex` 回采 UI 并逐像素判断 `uiColor.a < 0.01`（`glsl:105-116`） | Windows 没有等价的后处理节点。只能做成独立绘制层：`QOpenGLWidget` 或自绘 `QWidget` 铺在 `body` 底层，所有内容页透明。混合由层序完成，不需要逐像素采样 UI | 必须换形态，不可省的是「背景在下、内容在上」这个层序 |

补充三条与上表直接相关的落地约束：

- 混合模式要显式设。独立层方案里背景层不透明，上层控件透明，靠 QSS 改成 `rgba` 或 `background: transparent`（`theme.py:102-105` 的 `QWidget { background: @surface }` 是主要障碍，见 Windows 文档 §2.1）。
- 着色器输出 alpha 约定不同。Android 要求预乘输出（`glsl:116`），GLSL/HLSL 走标准混合时用直通 alpha 并在混合状态里配 `SRC_ALPHA / ONE_MINUS_SRC_ALPHA`。
- 首帧特例要保留。Windows 定时器首帧没有哨兵值概念，但初始化完成前不要画，否则会闪一帧全默认色。

## 调研进度

- [x] 读 HyperBackground.kt
- [x] 读 HyperBackgroundShaderView.kt
- [x] 读 BgEffectPainter.java（任务未列但为必要依赖）
- [x] 逐行解析 hyper_background_effect.glsl
- [x] 读 AmbientFrameGrid.kt
- [x] 找 audioLevelFlow / beatImpulseFlow 产生处（AudioReactive.kt）
- [x] 找 HyperBackground( 调用点与叠层方式
- [x] 读 BgEffectPainterRenderTest.kt
- [x] 读 appearance 设置下的背景开关与策略
- [x] 写概览
- [x] 写提色管线
- [x] 写 shader 逐段
- [x] 写帧调度与性能
- [x] 写换歌过渡
- [x] 写降级策略
- [x] 写移植要点
