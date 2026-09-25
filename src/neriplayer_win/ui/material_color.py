"""Material Color Utilities 最小移植(HCT / CAM16 / TonalPalette / TonalSpot 方案)。

来源:Google material-color-utilities(Apache-2.0)TypeScript 实现的手工
Python 移植,仅保留「种子色 → M3 亮/暗色板」所需子集:

- utils/color_utils + math_utils:RGB/XYZ/L* 换算与角度工具;
- hct/viewing_conditions + cam16 + hct_solver + hct:HCT 色彩空间
  (CAM16 色相/色度 + L* 明度),solver 用临界平面二分求 gamut 内解;
- palettes/tonal_palette:定色相色度、按 tone 取色(含 KeyColor 二分
  与黄色 T99 特例);
- scheme:SchemeTonalSpot(SPEC_2021, contrast 0)的角色 tone 表——
  与上游 materialkolor(Android 端 NeriTheme 的取色库)同款参数,
  对应 PaletteStyle.TonalSpot / ColorSpec.SpecVersion.Default。

上游版权:Copyright 2021 Google LLC(见仓库 NOTICE 与 Apache-2.0:
http://www.apache.org/licenses/LICENSE-2.0);移植保持数值行为一致,
参考值回归见 tests/test_dynamic_theme.py(对照 materialyoucolor 3.x
与上游 TypeScript 测试快照冻结)。

模块约定:纯函数、无 Qt 依赖;argb 一律 32 位无符号整数。
"""

from __future__ import annotations

import math

# HctSolver 数值常量(自上游 TypeScript 源逐值提取,勿手改)
_SCALED_DISCOUNT_FROM_LINRGB = [[0.001200833568784504, 0.002389694492170889, 0.0002795742885861124], [0.0005891086651375999, 0.0029785502573438758, 0.0003270666104008398], [0.00010146692491640572, 0.0005364214359186694, 0.0032979401770712076]]
_LINRGB_FROM_SCALED_DISCOUNT = [[1373.2198709594231, -1100.4251190754821, -7.278681089101213], [-271.815969077903, 559.6580465940733, -32.46047482791194], [1.9622899599665666, -57.173814538844006, 308.7233197812385]]
_CRITICAL_PLANES = [0.015176349177441876, 0.045529047532325624, 0.07588174588720938, 0.10623444424209313, 0.13658714259697685, 0.16693984095186062, 0.19729253930674434, 0.2276452376616281, 0.2579979360165119, 0.28835063437139563, 0.3188300904430532, 0.350925934958123, 0.3848314933096426, 0.42057480301049466, 0.458183274052838, 0.4976837250274023, 0.5391024159806381, 0.5824650784040898, 0.6277969426914107, 0.6751227633498623, 0.7244668422128921, 0.775853049866786, 0.829304845476233, 0.8848452951698498, 0.942497089126609, 1.0022825574869039, 1.0642236851973577, 1.1283421258858297, 1.1946592148522128, 1.2631959812511864, 1.3339731595349034, 1.407011200216447, 1.4823302800086415, 1.5599503113873272, 1.6398909516233677, 1.7221716113234105, 1.8068114625156377, 1.8938294463134073, 1.9832442801866852, 2.075074464868551, 2.1693382909216234, 2.2660538449872063, 2.36523901573795, 2.4669114995532007, 2.5710888059345764, 2.6777882626779785, 2.7870270208169257, 2.898822059350997, 3.0131901897720907, 3.1301480604002863, 3.2497121605402226, 3.3718988244681087, 3.4967242352587946, 3.624204428461639, 3.754355295633311, 3.887192587735158, 4.022731918402185, 4.160988767090289, 4.301978482107941, 4.445716283538092, 4.592217266055746, 4.741496401646282, 4.893568542229298, 5.048448422192488, 5.20615066083972, 5.3666897647573375, 5.5300801301023865, 5.696336044816294, 5.865471690767354, 6.037501145825082, 6.212438385869475, 6.390297286737924, 6.571091626112461, 6.7548350853498045, 6.941541251256611, 7.131223617812143, 7.323895587840543, 7.5195704746346665, 7.7182615035334345, 7.919981813454504, 8.124744458384042, 8.332562408825165, 8.543448553206703, 8.757415699253682, 8.974476575321063, 9.194643831691977, 9.417930041841839, 9.644347703669503, 9.873909240696694, 10.106627003236781, 10.342513269534024, 10.58158024687427, 10.8238400726681, 11.069304815507364, 11.317986476196008, 11.569896988756009, 11.825048221409341, 12.083451977536606, 12.345119996613247, 12.610063955123938, 12.878295467455942, 13.149826086772048, 13.42466730586372, 13.702830557985108, 13.984327217668513, 14.269168601521828, 14.55736596900856, 14.848930523210871, 15.143873411576273, 15.44220572664832, 15.743938506781891, 16.04908273684337, 16.35764934889634, 16.66964922287304, 16.985093187232053, 17.30399201960269, 17.62635644741625, 17.95219714852476, 18.281524751807332, 18.614349837764564, 18.95068293910138, 19.290534541298456, 19.633915083172692, 19.98083495742689, 20.331304511189067, 20.685334046541502, 21.042933821039977, 21.404114048223256, 21.76888489811322, 22.137256497705877, 22.50923893145328, 22.884842241736916, 23.264076429332462, 23.6469514538663, 24.033477234264016, 24.42366364919083, 24.817520537484558, 25.21505769858089, 25.61628489293138, 26.021211842414342, 26.429848230738664, 26.842203703840827, 27.258287870275353, 27.678110301598522, 28.10168053274597, 28.529008062403893, 28.96010235337422, 29.39497283293396, 29.83362889318845, 30.276079891419332, 30.722335150426627, 31.172403958865512, 31.62629557157785, 32.08401920991837, 32.54558406207592, 33.010999283389665, 33.4802739966603, 33.953417292456834, 34.430438229418264, 34.911345834551085, 35.39614910352207, 35.88485700094671, 36.37747846067349, 36.87402238606382, 37.37449765026789, 37.87891309649659, 38.38727753828926, 38.89959975977785, 39.41588851594697, 39.93615253289054, 40.460400508064545, 40.98864111053629, 41.520882981230194, 42.05713473317016, 42.597404951718396, 43.141702194811224, 43.6900349931913, 44.24241185063697, 44.798841244188324, 45.35933162437017, 45.92389141541209, 46.49252901546552, 47.065252796817916, 47.64207110610409, 48.22299226451468, 48.808024568002054, 49.3971762874833, 49.9904556690408, 50.587870934119984, 51.189430279724725, 51.79514187861014, 52.40501387947288, 53.0190544071392, 53.637271562750364, 54.259673423945976, 54.88626804504493, 55.517063457223934, 56.15206766869424, 56.79128866487574, 57.43473440856916, 58.08241284012621, 58.734331877617365, 59.39049941699807, 60.05092333227251, 60.715611475655585, 61.38457167773311, 62.057811747619894, 62.7353394731159, 63.417162620860914, 64.10328893648692, 64.79372614476921, 65.48848194977529, 66.18756403501224, 66.89098006357258, 67.59873767827808, 68.31084450182222, 69.02730813691093, 69.74813616640164, 70.47333615344107, 71.20291564160104, 71.93688215501312, 72.67524319850172, 73.41800625771542, 74.16517879925733, 74.9167682708136, 75.67278210128072, 76.43322770089146, 77.1981124613393, 77.96744375590167, 78.74122893956174, 79.51947534912904, 80.30219030335869, 81.08938110306934, 81.88105503125999, 82.67721935322541, 83.4778813166706, 84.28304815182372, 85.09272707154808, 85.90692527145302, 86.72564993000343, 87.54890820862819, 88.3767072518277, 89.2090541872801, 90.04595612594655, 90.88742016217518, 91.73345337380438, 92.58406282226491, 93.43925555268066, 94.29903859396902, 95.16341895893969, 96.03240364439274, 96.9059996312159, 97.78421388448044, 98.6670533535366, 99.55452497210776]

# ---------------------------------------------------------------------------
# math utils(math_utils.ts)
# ---------------------------------------------------------------------------


def _signum(num: float) -> float:
    if num < 0:
        return -1.0
    if num == 0:
        return 0.0
    return 1.0


def _lerp(start: float, stop: float, amount: float) -> float:
    return (1.0 - amount) * start + amount * stop


def _clamp_int(minimum: int, maximum: int, value: float) -> int:
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return int(value)


def _sanitize_degrees(degrees: float) -> float:
    degrees = math.fmod(degrees, 360.0)
    if degrees < 0:
        degrees += 360.0
    return degrees


def _matrix_multiply(row: list[float], matrix: list[list[float]]) -> list[float]:
    return [
        row[0] * matrix[0][0] + row[1] * matrix[0][1] + row[2] * matrix[0][2],
        row[0] * matrix[1][0] + row[1] * matrix[1][1] + row[2] * matrix[1][2],
        row[0] * matrix[2][0] + row[1] * matrix[2][1] + row[2] * matrix[2][2],
    ]


# ---------------------------------------------------------------------------
# color utils(color_utils.ts)
# ---------------------------------------------------------------------------

_SRGB_TO_XYZ = [
    [0.41233895, 0.35762064, 0.18051042],
    [0.2126, 0.7152, 0.0722],
    [0.01932141, 0.11916382, 0.95034478],
]
_XYZ_TO_SRGB = [
    [3.2413774792388685, -1.5376652402851851, -0.49885366846268053],
    [-0.9691452513005321, 1.8758853451067872, 0.04156585616912061],
    [0.05562093689691305, -0.20395524564742123, 1.0571799111220335],
]
_WHITE_POINT_D65 = [95.047, 100.0, 108.883]
_Y_FROM_LINRGB = [0.2126, 0.7152, 0.0722]


def argb_from_rgb(red: int, green: int, blue: int) -> int:
    return ((255 << 24) | ((red & 255) << 16) | ((green & 255) << 8) | (blue & 255)) & 0xFFFFFFFF


def red_from_argb(argb: int) -> int:
    return (argb >> 16) & 255


def green_from_argb(argb: int) -> int:
    return (argb >> 8) & 255


def blue_from_argb(argb: int) -> int:
    return argb & 255


def _linearized(rgb_component: float) -> float:
    normalized = rgb_component / 255.0
    if normalized <= 0.040449936:
        return normalized / 12.92 * 100.0
    return ((normalized + 0.055) / 1.055) ** 2.4 * 100.0


def _delinearized(rgb_component: float) -> int:
    normalized = rgb_component / 100.0
    if normalized <= 0.0031308:
        value = normalized * 12.92
    else:
        value = 1.055 * normalized ** (1.0 / 2.4) - 0.055
    return _clamp_int(0, 255, round(value * 255.0))


def _true_delinearized(rgb_component: float) -> float:
    normalized = rgb_component / 100.0
    if normalized <= 0.0031308:
        value = normalized * 12.92
    else:
        value = 1.055 * normalized ** (1.0 / 2.4) - 0.055
    return value * 255.0


def _argb_from_linrgb(linrgb: list[float]) -> int:
    return argb_from_rgb(
        _delinearized(linrgb[0]),
        _delinearized(linrgb[1]),
        _delinearized(linrgb[2]),
    )


def _xyz_from_argb(argb: int) -> list[float]:
    return _matrix_multiply(
        [
            _linearized(red_from_argb(argb)),
            _linearized(green_from_argb(argb)),
            _linearized(blue_from_argb(argb)),
        ],
        _SRGB_TO_XYZ,
    )


def _argb_from_xyz(x: float, y: float, z: float) -> int:
    linear_r = _XYZ_TO_SRGB[0][0] * x + _XYZ_TO_SRGB[0][1] * y + _XYZ_TO_SRGB[0][2] * z
    linear_g = _XYZ_TO_SRGB[1][0] * x + _XYZ_TO_SRGB[1][1] * y + _XYZ_TO_SRGB[1][2] * z
    linear_b = _XYZ_TO_SRGB[2][0] * x + _XYZ_TO_SRGB[2][1] * y + _XYZ_TO_SRGB[2][2] * z
    return argb_from_rgb(
        _delinearized(linear_r), _delinearized(linear_g), _delinearized(linear_b)
    )


def _lab_f(t: float) -> float:
    e = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    if t > e:
        return t ** (1.0 / 3.0)
    return (kappa * t + 16) / 116


def _lab_inv_f(ft: float) -> float:
    e = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    ft3 = ft * ft * ft
    if ft3 > e:
        return ft3
    return (116 * ft - 16) / kappa


def y_from_lstar(lstar: float) -> float:
    return 100.0 * _lab_inv_f((lstar + 16.0) / 116.0)


def _lstar_from_argb(argb: int) -> float:
    y = _xyz_from_argb(argb)[1]
    return 116.0 * _lab_f(y / 100.0) - 16.0


def _argb_from_lstar(lstar: float) -> int:
    y = y_from_lstar(lstar)
    component = _delinearized(y)
    return argb_from_rgb(component, component, component)


# ---------------------------------------------------------------------------
# viewing conditions(viewing_conditions.ts):sRGB 默认观看条件
# ---------------------------------------------------------------------------


class _ViewingConditions:
    __slots__ = ("n", "aw", "nbb", "ncb", "c", "nc", "rgb_d", "fl", "fl_root", "z")

    def __init__(self, n, aw, nbb, ncb, c, nc, rgb_d, fl, fl_root, z) -> None:
        self.n = n
        self.aw = aw
        self.nbb = nbb
        self.ncb = ncb
        self.c = c
        self.nc = nc
        self.rgb_d = rgb_d
        self.fl = fl
        self.fl_root = fl_root
        self.z = z


def _make_default_viewing_conditions() -> _ViewingConditions:
    xyz = _WHITE_POINT_D65
    adapting_luminance = (200.0 / math.pi) * y_from_lstar(50.0) / 100.0
    background_lstar = 50.0
    surround = 2.0
    r_w = xyz[0] * 0.401288 + xyz[1] * 0.650173 + xyz[2] * -0.051461
    g_w = xyz[0] * -0.250268 + xyz[1] * 1.204414 + xyz[2] * 0.045854
    b_w = xyz[0] * -0.002079 + xyz[1] * 0.048952 + xyz[2] * 0.953127
    f = 0.8 + surround / 10.0
    if f >= 0.9:
        c = _lerp(0.59, 0.69, (f - 0.9) * 10.0)
    else:
        c = _lerp(0.525, 0.59, (f - 0.8) * 10.0)
    d = f * (1.0 - (1.0 / 3.6) * math.exp((-adapting_luminance - 42.0) / 92.0))
    d = max(0.0, min(1.0, d))
    nc = f
    rgb_d = [
        d * (100.0 / r_w) + 1.0 - d,
        d * (100.0 / g_w) + 1.0 - d,
        d * (100.0 / b_w) + 1.0 - d,
    ]
    k = 1.0 / (5.0 * adapting_luminance + 1.0)
    k4 = k * k * k * k
    k4f = 1.0 - k4
    fl = k4 * adapting_luminance + 0.1 * k4f * k4f * (5.0 * adapting_luminance) ** (1.0 / 3.0)
    n = y_from_lstar(background_lstar) / xyz[1]
    z = 1.48 + math.sqrt(n)
    nbb = 0.725 / n ** 0.2
    ncb = nbb
    rgb_a_factors = [
        (fl * rgb_d[0] * r_w / 100.0) ** 0.42,
        (fl * rgb_d[1] * g_w / 100.0) ** 0.42,
        (fl * rgb_d[2] * b_w / 100.0) ** 0.42,
    ]
    rgb_a = [
        (400.0 * rgb_a_factors[0]) / (rgb_a_factors[0] + 27.13),
        (400.0 * rgb_a_factors[1]) / (rgb_a_factors[1] + 27.13),
        (400.0 * rgb_a_factors[2]) / (rgb_a_factors[2] + 27.13),
    ]
    aw = (2.0 * rgb_a[0] + rgb_a[1] + 0.05 * rgb_a[2]) * nbb
    return _ViewingConditions(n, aw, nbb, ncb, c, nc, rgb_d, fl, fl ** 0.25, z)


_DEFAULT_VIEWING_CONDITIONS = _make_default_viewing_conditions()


# ---------------------------------------------------------------------------
# CAM16(cam16.ts):仅保留 ARGB →(hue, chroma, j)与 JCH → ARGB 两条路径
# ---------------------------------------------------------------------------


def _cam16_from_argb(argb: int) -> tuple[float, float, float]:
    """ARGB → (hue, chroma, j);默认观看条件。"""
    vc = _DEFAULT_VIEWING_CONDITIONS
    red_l = _linearized(red_from_argb(argb))
    green_l = _linearized(green_from_argb(argb))
    blue_l = _linearized(blue_from_argb(argb))
    x = 0.41233895 * red_l + 0.35762064 * green_l + 0.18051042 * blue_l
    y = 0.2126 * red_l + 0.7152 * green_l + 0.0722 * blue_l
    z = 0.01932141 * red_l + 0.11916382 * green_l + 0.95034478 * blue_l
    r_c = 0.401288 * x + 0.650173 * y - 0.051461 * z
    g_c = -0.250268 * x + 1.204414 * y + 0.045854 * z
    b_c = -0.002079 * x + 0.048952 * y + 0.953127 * z
    r_d = vc.rgb_d[0] * r_c
    g_d = vc.rgb_d[1] * g_c
    b_d = vc.rgb_d[2] * b_c
    r_af = (vc.fl * abs(r_d) / 100.0) ** 0.42
    g_af = (vc.fl * abs(g_d) / 100.0) ** 0.42
    b_af = (vc.fl * abs(b_d) / 100.0) ** 0.42
    r_a = _signum(r_d) * 400.0 * r_af / (r_af + 27.13)
    g_a = _signum(g_d) * 400.0 * g_af / (g_af + 27.13)
    b_a = _signum(b_d) * 400.0 * b_af / (b_af + 27.13)
    a = (11.0 * r_a + -12.0 * g_a + b_a) / 11.0
    b = (r_a + g_a - 2.0 * b_a) / 9.0
    u = (20.0 * r_a + 20.0 * g_a + 21.0 * b_a) / 20.0
    p2 = (40.0 * r_a + 20.0 * g_a + b_a) / 20.0
    atan_degrees = math.atan2(b, a) * 180.0 / math.pi
    hue = _sanitize_degrees(atan_degrees)
    ac = p2 * vc.nbb
    j = 100.0 * (ac / vc.aw) ** (vc.c * vc.z)
    hue_prime = hue + 360 if hue < 20.14 else hue
    e_hue = 0.25 * (math.cos(hue_prime * math.pi / 180.0 + 2.0) + 3.8)
    p1 = (50000.0 / 13.0) * e_hue * vc.nc * vc.ncb
    t = (p1 * math.sqrt(a * a + b * b)) / (u + 0.305)
    alpha = t ** 0.9 * (1.64 - 0.29 ** vc.n) ** 0.73
    c = alpha * math.sqrt(j / 100.0)
    return hue, c, j


# ---------------------------------------------------------------------------
# HctSolver(hct_solver.ts):hue+chroma+tone → ARGB
# ---------------------------------------------------------------------------


def _sanitize_radians(angle: float) -> float:
    return (angle + math.pi * 8) % (math.pi * 2)


def _chromatic_adaptation(component: float) -> float:
    af = abs(component) ** 0.42
    return _signum(component) * 400.0 * af / (af + 27.13)


def _hue_of(linrgb: list[float]) -> float:
    scaled = _matrix_multiply(linrgb, _SCALED_DISCOUNT_FROM_LINRGB)
    r_a = _chromatic_adaptation(scaled[0])
    g_a = _chromatic_adaptation(scaled[1])
    b_a = _chromatic_adaptation(scaled[2])
    a = (11.0 * r_a + -12.0 * g_a + b_a) / 11.0
    b = (r_a + g_a - 2.0 * b_a) / 9.0
    return math.atan2(b, a)


def _are_in_cyclic_order(a: float, b: float, c: float) -> bool:
    return _sanitize_radians(b - a) < _sanitize_radians(c - a)


def _intercept(source: float, mid: float, target: float) -> float:
    return (mid - source) / (target - source)


def _lerp_point(source: list[float], t: float, target: list[float]) -> list[float]:
    return [
        source[0] + (target[0] - source[0]) * t,
        source[1] + (target[1] - source[1]) * t,
        source[2] + (target[2] - source[2]) * t,
    ]


def _set_coordinate(
    source: list[float], coordinate: float, target: list[float], axis: int
) -> list[float]:
    t = _intercept(source[axis], coordinate, target[axis])
    return _lerp_point(source, t, target)


def _is_bounded(x: float) -> bool:
    return 0.0 <= x <= 100.0


def _nth_vertex(y: float, n: int) -> list[float]:
    k_r = _Y_FROM_LINRGB[0]
    k_g = _Y_FROM_LINRGB[1]
    k_b = _Y_FROM_LINRGB[2]
    coord_a = 0.0 if n % 4 <= 1 else 100.0
    coord_b = 0.0 if n % 2 == 0 else 100.0
    if n < 4:
        g = coord_a
        b = coord_b
        r = (y - g * k_g - b * k_b) / k_r
        return [r, g, b] if _is_bounded(r) else [-1.0, -1.0, -1.0]
    if n < 8:
        b = coord_a
        r = coord_b
        g = (y - r * k_r - b * k_b) / k_g
        return [r, g, b] if _is_bounded(g) else [-1.0, -1.0, -1.0]
    r = coord_a
    g = coord_b
    b = (y - r * k_r - g * k_g) / k_b
    return [r, g, b] if _is_bounded(b) else [-1.0, -1.0, -1.0]


def _bisect_to_segment(y: float, target_hue: float) -> tuple[list[float], list[float]]:
    left = [-1.0, -1.0, -1.0]
    right = left
    left_hue = 0.0
    right_hue = 0.0
    initialized = False
    uncut = True
    for n in range(12):
        mid = _nth_vertex(y, n)
        if mid[0] < 0:
            continue
        mid_hue = _hue_of(mid)
        if not initialized:
            left = mid
            right = mid
            left_hue = mid_hue
            right_hue = mid_hue
            initialized = True
            continue
        if uncut or _are_in_cyclic_order(left_hue, mid_hue, right_hue):
            uncut = False
            if _are_in_cyclic_order(left_hue, target_hue, mid_hue):
                right = mid
                right_hue = mid_hue
            else:
                left = mid
                left_hue = mid_hue
    return left, right


def _midpoint(a: list[float], b: list[float]) -> list[float]:
    return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2]


def _critical_plane_below(x: float) -> int:
    return math.floor(x - 0.5)


def _critical_plane_above(x: float) -> int:
    return math.ceil(x - 0.5)


def _bisect_to_limit(y: float, target_hue: float) -> list[float]:
    left, right = _bisect_to_segment(y, target_hue)
    left_hue = _hue_of(left)
    for axis in range(3):
        if left[axis] != right[axis]:
            if left[axis] < right[axis]:
                l_plane = _critical_plane_below(_true_delinearized(left[axis]))
                r_plane = _critical_plane_above(_true_delinearized(right[axis]))
            else:
                l_plane = _critical_plane_above(_true_delinearized(left[axis]))
                r_plane = _critical_plane_below(_true_delinearized(right[axis]))
            for _ in range(8):
                if abs(r_plane - l_plane) <= 1:
                    break
                m_plane = math.floor((l_plane + r_plane) / 2.0)
                mid_plane_coordinate = _CRITICAL_PLANES[m_plane]
                mid = _set_coordinate(left, mid_plane_coordinate, right, axis)
                mid_hue = _hue_of(mid)
                if _are_in_cyclic_order(left_hue, target_hue, mid_hue):
                    right = mid
                    r_plane = m_plane
                else:
                    left = mid
                    left_hue = mid_hue
                    l_plane = m_plane
    return _midpoint(left, right)


def _inverse_chromatic_adaptation(adapted: float) -> float:
    adapted_abs = abs(adapted)
    base = max(0.0, 27.13 * adapted_abs / (400.0 - adapted_abs))
    return _signum(adapted) * base ** (1.0 / 0.42)


def _find_result_by_j(hue_radians: float, chroma: float, y: float) -> int:
    j = math.sqrt(y) * 11.0
    vc = _DEFAULT_VIEWING_CONDITIONS
    t_inner_coeff = 1 / (1.64 - 0.29 ** vc.n) ** 0.73
    e_hue = 0.25 * (math.cos(hue_radians + 2.0) + 3.8)
    p1 = e_hue * (50000.0 / 13.0) * vc.nc * vc.ncb
    h_sin = math.sin(hue_radians)
    h_cos = math.cos(hue_radians)
    for iteration_round in range(5):
        j_normalized = j / 100.0
        alpha = 0.0 if chroma == 0.0 or j == 0.0 else chroma / math.sqrt(j_normalized)
        t = (alpha * t_inner_coeff) ** (1.0 / 0.9)
        ac = vc.aw * j_normalized ** (1.0 / vc.c / vc.z)
        p2 = ac / vc.nbb
        gamma = (
            23.0 * (p2 + 0.305) * t
            / (23.0 * p1 + 11.0 * t * h_cos + 108.0 * t * h_sin)
        )
        a = gamma * h_cos
        b = gamma * h_sin
        r_a = (460.0 * p2 + 451.0 * a + 288.0 * b) / 1403.0
        g_a = (460.0 * p2 - 891.0 * a - 261.0 * b) / 1403.0
        b_a = (460.0 * p2 - 220.0 * a - 6300.0 * b) / 1403.0
        r_c_scaled = _inverse_chromatic_adaptation(r_a)
        g_c_scaled = _inverse_chromatic_adaptation(g_a)
        b_c_scaled = _inverse_chromatic_adaptation(b_a)
        linrgb = _matrix_multiply(
            [r_c_scaled, g_c_scaled, b_c_scaled], _LINRGB_FROM_SCALED_DISCOUNT
        )
        if linrgb[0] < 0 or linrgb[1] < 0 or linrgb[2] < 0:
            return 0
        fnj = (
            _Y_FROM_LINRGB[0] * linrgb[0]
            + _Y_FROM_LINRGB[1] * linrgb[1]
            + _Y_FROM_LINRGB[2] * linrgb[2]
        )
        if fnj <= 0:
            return 0
        if iteration_round == 4 or abs(fnj - y) < 0.002:
            if linrgb[0] > 100.01 or linrgb[1] > 100.01 or linrgb[2] > 100.01:
                return 0
            return _argb_from_linrgb(linrgb)
        j = j - (fnj - y) * j / (2 * fnj)
    return 0


def _solve_to_int(hue_degrees: float, chroma: float, lstar: float) -> int:
    if chroma < 0.0001 or lstar < 0.0001 or lstar > 99.9999:
        return _argb_from_lstar(lstar)
    hue_degrees = _sanitize_degrees(hue_degrees)
    hue_radians = hue_degrees / 180 * math.pi
    y = y_from_lstar(lstar)
    exact = _find_result_by_j(hue_radians, chroma, y)
    if exact != 0:
        return exact
    linrgb = _bisect_to_limit(y, hue_radians)
    return _argb_from_linrgb(linrgb)


# ---------------------------------------------------------------------------
# Hct(hct.ts):仅需 (hue, chroma, tone) → ARGB 与 ARGB → (hue, chroma, tone)
# ---------------------------------------------------------------------------


def hct_from_int(argb: int) -> tuple[float, float, float]:
    hue, chroma, _j = _cam16_from_argb(argb)
    return hue, chroma, _lstar_from_argb(argb)


def argb_from_hct(hue: float, chroma: float, tone: float) -> int:
    return _solve_to_int(hue, chroma, tone)


# ---------------------------------------------------------------------------
# TonalPalette(tonal_palette.ts):含 KeyColor 二分与黄色 T99 特例
# ---------------------------------------------------------------------------


class TonalPalette:
    """定 hue/chroma 的色调梯度;tone() 带 memo 缓存。"""

    def __init__(self, hue: float, chroma: float) -> None:
        self.hue = hue
        self.chroma = chroma
        self._cache: dict[float, int] = {}
        self.key_color_tone = self._key_color_tone()

    @classmethod
    def of(cls, hue: float, chroma: float) -> "TonalPalette":
        return cls(hue, chroma)

    def tone(self, tone: float) -> int:
        cached = self._cache.get(tone)
        if cached is not None:
            return cached
        if tone == 99 and 105 <= self.hue < 125:
            argb = _average_argb(self.tone(98), self.tone(100))
        else:
            argb = argb_from_hct(self.hue, self.chroma, tone)
        self._cache[tone] = argb
        return argb

    def hex(self, tone: float) -> str:
        return f"#{self.tone(tone) & 0xFFFFFF:06X}"

    # KeyColor:自 T50 起二分找能容纳请求 chroma 的代表色调(上游 2023 版行为)

    def _key_color_tone(self) -> float:
        pivot = 50
        step = 1
        epsilon = 0.01
        max_chroma_value = 200.0
        chroma_cache: dict[int, float] = {}

        def max_chroma(tone: int) -> float:
            if tone not in chroma_cache:
                chroma_cache[tone] = hct_from_int(
                    argb_from_hct(self.hue, max_chroma_value, tone)
                )[1]
            return chroma_cache[tone]

        lower, upper = 0, 100
        while lower < upper:
            mid = (lower + upper) // 2
            ascending = max_chroma(mid) < max_chroma(mid + step)
            sufficient = max_chroma(mid) >= self.chroma - epsilon
            if sufficient:
                if abs(lower - pivot) < abs(upper - pivot):
                    upper = mid
                else:
                    if lower == mid:
                        return float(lower)
                    lower = mid
            elif ascending:
                lower = mid + step
            else:
                upper = mid
        return float(lower)


def _average_argb(argb1: int, argb2: int) -> int:
    red = round((red_from_argb(argb1) + red_from_argb(argb2)) / 2)
    green = round((green_from_argb(argb1) + green_from_argb(argb2)) / 2)
    blue = round((blue_from_argb(argb1) + blue_from_argb(argb2)) / 2)
    return argb_from_rgb(red, green, blue)


# ---------------------------------------------------------------------------
# SchemeTonalSpot(SPEC_2021, contrast 0)→ 本项目 theme.py 色板角色
# 色度参数与角色 tone 表对照上游 scheme/SchemeTonalSpot 与
# dynamiccolor/color_spec_2021.ts(contrast 0 时 ToneDeltaPair 不改基调)。
# ---------------------------------------------------------------------------

# 角色 → (调色板, dark tone, light tone)
_TONAL_SPOT_ROLES: dict[str, tuple[str, float, float]] = {
    "primary": ("primary", 80, 40),
    "onPrimary": ("primary", 20, 100),
    "primaryContainer": ("primary", 30, 90),
    "onPrimaryContainer": ("primary", 90, 30),
    "secondary": ("secondary", 80, 40),
    "secondaryContainer": ("secondary", 30, 90),
    "onSecondaryContainer": ("secondary", 90, 30),
    "tertiary": ("tertiary", 80, 40),
    "tertiaryContainer": ("tertiary", 30, 90),
    "onTertiaryContainer": ("tertiary", 90, 30),
    "error": ("error", 80, 40),
    "errorContainer": ("error", 30, 90),
    "background": ("neutral", 6, 98),
    "onBackground": ("neutral", 90, 10),
    "surface": ("neutral", 6, 98),
    "onSurface": ("neutral", 90, 10),
    "surfaceVariant": ("neutral_variant", 30, 90),
    "onSurfaceVariant": ("neutral_variant", 80, 30),
    "outline": ("neutral_variant", 60, 50),
    "outlineVariant": ("neutral_variant", 30, 80),
    "surfaceLowest": ("neutral", 4, 100),
    "surfaceLow": ("neutral", 10, 96),
    "surfaceContainer": ("neutral", 12, 94),
    "surfaceHigh": ("neutral", 17, 92),
    "surfaceHighest": ("neutral", 22, 90),
}

_ERROR_PALETTE_SEED_ARGb = 0xFFB3261E  # 上游 error palette: hue 25, chroma 84


def tonal_spot_palette(seed_argb: int, is_dark: bool) -> dict[str, str]:
    """种子色 → TonalSpot(2021)全角色色板(theme.py 的角色命名)。

    与 Android 端 rememberDynamicColorScheme(seed, isDark, TonalSpot)
    的默认参数一致(现行 material-color-utilities 的 2021 palette
delegate:primary C36 / secondary C16 / tertiary 色相+60°C24 /
neutral C6 / neutralVariant C8);输出可直接喂给 theme.build_qss。
    """
    hue, chroma, _tone = hct_from_int(seed_argb & 0xFFFFFFFF)
    palettes = {
        "primary": TonalPalette.of(hue, 36.0),
        "secondary": TonalPalette.of(hue, 16.0),
        "tertiary": TonalPalette.of(_sanitize_degrees(hue + 60.0), 24.0),
        "neutral": TonalPalette.of(hue, 6.0),
        "neutral_variant": TonalPalette.of(hue, 8.0),
        "error": TonalPalette.of(25.0, 84.0),
    }
    result: dict[str, str] = {}
    for role, (palette_name, dark_tone, light_tone) in _TONAL_SPOT_ROLES.items():
        tone = dark_tone if is_dark else light_tone
        result[role] = palettes[palette_name].hex(tone)
    return result


def seed_from_hex(hex_color: str) -> int:
    """'#RRGGBB' / 'RRGGBB' → ARGB;非法输入回落默认种子 #0061A4。"""
    text = hex_color.strip().lstrip("#")
    if len(text) != 6:
        text = "0061A4"
    try:
        return (0xFF << 24 | int(text, 16)) & 0xFFFFFFFF
    except ValueError:
        return (0xFF << 24 | 0x0061A4) & 0xFFFFFFFF
