"""设备档案库:20+ 台**自洽**的虚拟设备。

"自洽"是指档案内部不出现自相矛盾的组合——这是 bot 检测最容易抓的点:
移动 UA 配桌面 viewport、locale 与时区不在同一地区、Android 设备却没有触摸事件,
都会在一两个请求内暴露。

实现上不靠"逐条手写并祈祷不写错",而是**按构造保证一致**:
每个设备只声明 `sale_region`,locale 与 timezone 由 ``REGIONS`` 表推导;
平台相关的 UA、viewport、字体、GPU 由平台模板统一生成。

一致性校验仍保留在 :meth:`DeviceLibrary.validate`,供单测遍历全库断言。
"""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass, field
from typing import Iterator

from ..core.types import DeviceProfile

#: 销售地区 → (locale, IANA 时区)。档案的 locale/timezone 一律由此推导。
REGIONS: dict[str, tuple[str, str]] = {
    "US": ("en-US", "America/New_York"),
    "GB": ("en-GB", "Europe/London"),
    "DE": ("de-DE", "Europe/Berlin"),
    "FR": ("fr-FR", "Europe/Paris"),
    "JP": ("ja-JP", "Asia/Tokyo"),
    "CN": ("zh-CN", "Asia/Shanghai"),
    "KR": ("ko-KR", "Asia/Seoul"),
    "TW": ("zh-TW", "Asia/Taipei"),
    "SG": ("en-SG", "Asia/Singapore"),
    "AU": ("en-AU", "Australia/Sydney"),
    "BR": ("pt-BR", "America/Sao_Paulo"),
    "IN": ("en-IN", "Asia/Kolkata"),
    "RU": ("ru-RU", "Europe/Moscow"),
    "ES": ("es-ES", "Europe/Madrid"),
    "IT": ("it-IT", "Europe/Rome"),
    "CA": ("en-CA", "America/Toronto"),
    "MX": ("es-MX", "America/Mexico_City"),
    "NL": ("nl-NL", "Europe/Amsterdam"),
    "SE": ("sv-SE", "Europe/Stockholm"),
    "PL": ("pl-PL", "Europe/Warsaw"),
    "TR": ("tr-TR", "Europe/Istanbul"),
}

#: 平台 → 字体清单
PLATFORM_FONTS: dict[str, list[str]] = {
    "windows": ["Arial", "Calibri", "Cambria", "Consolas", "Courier New", "Georgia",
                "MS Gothic", "Microsoft YaHei", "Segoe UI", "SimSun", "Tahoma",
                "Times New Roman", "Trebuchet MS", "Verdana"],
    "macos": ["American Typewriter", "Arial", "Avenir", "Arial Unicode MS", "Courier",
              "Geneva", "Georgia", "Helvetica", "Helvetica Neue", "Menlo", "Monaco",
              "PingFang SC", "Songti SC", "Times New Roman"],
    "linux": ["DejaVu Sans", "DejaVu Serif", "Liberation Mono", "Liberation Sans",
              "Noto Sans CJK SC", "Ubuntu", "Ubuntu Mono", "WenQuanYi Micro Hei",
              "FreeMono", "FreeSerif"],
    "android": ["Roboto", "Noto Sans", "Noto Serif", "Droid Sans Mono", "Cutive Mono",
                "Coming Soon", "Dancing Script", "Carrois Gothic SC"],
    "ios": ["American Typewriter", "Avenir", "Arial", "Chalkboard SE", "Cochin",
            "Courier", "Georgia", "Helvetica", "Marker Felt", "Menlo", "Palatino",
            "PingFang SC", "Times New Roman"],
}

#: 平台 → GPU 厂商/渲染器
PLATFORM_GPU: dict[str, tuple[str, str]] = {
    "windows": ("Google Inc. (NVIDIA)",
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    "macos": ("Google Inc. (Apple)",
              "ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Pro, Unspecified Version)"),
    "linux": ("Google Inc. (Intel)",
              "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6)"),
    "android": ("Qualcomm", "Adreno (TM) 740"),
    "ios": ("Apple Inc.", "Apple GPU"),
}


@dataclass(frozen=True)
class DeviceSpec:
    """一台设备的声明式描述(只写差异项,其余由平台模板补齐)。"""

    name: str
    family: str  # windows / macos / linux / android / ios
    ua: str
    viewport: tuple[int, int]
    sale_region: str
    platform: str
    mobile: bool = False
    has_touch: bool = False
    device_scale_factor: float = 1.0
    gpu_vendor: str | None = None
    gpu_renderer: str | None = None
    fonts: list[str] = field(default_factory=list)


def _win_ua() -> str:
    """Windows Chrome UA。"""
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def _win11_ua() -> str:
    """Windows 11 Chrome UA。"""
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0")


def _mac_chrome_ua() -> str:
    """macOS Chrome UA。"""
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def _mac_safari_ua() -> str:
    """macOS Safari UA。"""
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")


def _linux_chrome_ua() -> str:
    """Linux Chrome UA。"""
    return ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def _android_chrome_ua(model: str, version: str) -> str:
    """Android Chrome UA。"""
    return (f"Mozilla/5.0 (Linux; Android {version}; {model}) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36")


def _ios_safari_ua(model: str, os_version: str) -> str:
    """iOS Safari UA。"""
    return (f"Mozilla/5.0 ({model}; CPU iPhone OS {os_version} like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
            "Mobile/15E148 Safari/604.1")


#: 全部设备声明(21 台,覆盖 Windows / macOS / Linux / Android / iOS)
DEVICE_SPECS: list[DeviceSpec] = [
    # --- Windows(5) ---
    DeviceSpec("win10_chrome_1920", "windows", _win_ua(), (1920, 1080), "US", "Win32"),
    DeviceSpec("win11_edge_1536", "windows", _win11_ua(), (1536, 864), "GB", "Win32"),
    DeviceSpec("win10_chrome_1366", "windows", _win_ua(), (1366, 768), "DE", "Win32"),
    DeviceSpec("win10_chrome_1440", "windows", _win_ua(), (2560, 1440), "FR", "Win32"),
    DeviceSpec("win10_chrome_1280", "windows", _win_ua(), (1280, 720), "JP", "Win32"),
    # --- macOS(4) ---
    DeviceSpec("mac_chrome_1440", "macos", _mac_chrome_ua(), (1440, 900), "US", "MacIntel"),
    DeviceSpec("mac_safari_1512", "macos", _mac_safari_ua(), (1512, 982), "JP", "MacIntel"),
    DeviceSpec("mac_chrome_1728", "macos", _mac_chrome_ua(), (1728, 1117), "DE", "MacIntel"),
    DeviceSpec("mac_safari_1680", "macos", _mac_safari_ua(), (1680, 1050), "AU", "MacIntel"),
    # --- Linux(2) ---
    DeviceSpec("linux_chrome_1920", "linux", _linux_chrome_ua(), (1920, 1080), "CN", "Linux x86_64"),
    DeviceSpec("linux_chrome_1366", "linux", _linux_chrome_ua(), (1366, 768), "IN", "Linux x86_64"),
    # --- Android(6) ---
    DeviceSpec("android_pixel7", "android", _android_chrome_ua("Pixel 7", "13"),
               (412, 915), "JP", "Linux armv8l", mobile=True, has_touch=True,
               device_scale_factor=2.625),
    DeviceSpec("android_pixel8", "android", _android_chrome_ua("Pixel 8", "14"),
               (412, 915), "US", "Linux armv8l", mobile=True, has_touch=True,
               device_scale_factor=2.625),
    DeviceSpec("android_galaxys23", "android", _android_chrome_ua("SM-S911B", "13"),
               (360, 780), "KR", "Linux armv8l", mobile=True, has_touch=True,
               device_scale_factor=3.0),
    DeviceSpec("android_xiaomi13", "android", _android_chrome_ua("2201123G", "13"),
               (393, 873), "CN", "Linux armv8l", mobile=True, has_touch=True,
               device_scale_factor=2.75),
    DeviceSpec("android_oneplus11", "android", _android_chrome_ua("CPH2449", "14"),
               (412, 915), "IN", "Linux armv8l", mobile=True, has_touch=True,
               device_scale_factor=2.625),
    DeviceSpec("android_tablet_s9", "android", _android_chrome_ua("SM-X710", "13"),
               (800, 1280), "BR", "Linux armv8l", mobile=True, has_touch=True,
               device_scale_factor=2.0),
    # --- iOS(4) ---
    DeviceSpec("ios_iphone15", "ios", _ios_safari_ua("iPhone", "17_4"),
               (393, 852), "JP", "iPhone", mobile=True, has_touch=True,
               device_scale_factor=3.0),
    DeviceSpec("ios_iphone14", "ios", _ios_safari_ua("iPhone", "17_2"),
               (390, 844), "US", "iPhone", mobile=True, has_touch=True,
               device_scale_factor=3.0),
    DeviceSpec("ios_iphonese", "ios", _ios_safari_ua("iPhone", "16_6"),
               (375, 667), "FR", "iPhone", mobile=True, has_touch=True,
               device_scale_factor=2.0),
    DeviceSpec("ios_ipad_air", "ios", _ios_safari_ua("iPad", "17_4"),
               (820, 1180), "AU", "iPad", mobile=True, has_touch=True,
               device_scale_factor=2.0),
]


class DeviceLibrary:
    """设备档案库:按构造保证一致性,并提供选择策略。"""

    def __init__(self, specs: list[DeviceSpec] | None = None) -> None:
        """初始化档案库。

        :param specs: 设备声明列表;为空则使用内置 :data:`DEVICE_SPECS`。
        """
        self._specs = list(specs) if specs is not None else list(DEVICE_SPECS)
        self._profiles: list[DeviceProfile] = [self._build(spec) for spec in self._specs]
        self._by_name = {p.name: p for p in self._profiles}

    # ------------------------------------------------------------------ 构建

    @staticmethod
    def _build(spec: DeviceSpec) -> DeviceProfile:
        """把一条声明展开成完整档案(locale/timezone 由地区推导)。"""
        locale, timezone = REGIONS[spec.sale_region]
        vendor, renderer = PLATFORM_GPU[spec.family]
        fonts = spec.fonts or PLATFORM_FONTS[spec.family]
        return DeviceProfile(
            name=spec.name,
            ua=spec.ua,
            viewport=spec.viewport,
            timezone=timezone,
            locale=locale,
            gpu_vendor=spec.gpu_vendor or vendor,
            gpu_renderer=spec.gpu_renderer or renderer,
            fonts=list(fonts),
            has_touch=spec.has_touch,
            platform=spec.platform,
            sale_region=spec.sale_region,
            mobile=spec.mobile,
            device_scale_factor=spec.device_scale_factor,
            is_mobile=spec.mobile,
        )

    # ------------------------------------------------------------------ 查询

    def __len__(self) -> int:
        """档案数量。"""
        return len(self._profiles)

    def __iter__(self) -> Iterator[DeviceProfile]:
        """遍历全部档案。"""
        return iter(self._profiles)

    def all(self) -> list[DeviceProfile]:
        """返回全部档案的副本列表。"""
        return list(self._profiles)

    def by_name(self, name: str) -> DeviceProfile | None:
        """按名称取档案。

        :param name: 档案名。
        :return: 档案;不存在返回 ``None``。
        """
        return self._by_name.get(name)

    def pick(
        self,
        sale_region: str | None = None,
        mobile: bool | None = None,
        seed: int | None = None,
    ) -> DeviceProfile:
        """挑一台设备档案。

        :param sale_region: 限定销售地区(如 ``"JP"``);为空则不限。
        :param mobile: 限定是否移动端;为空则不限。
        :param seed: 随机种子;给定则结果可复现。
        :return: 选中的档案。
        """
        candidates = [
            p for p in self._profiles
            if (sale_region is None or p.sale_region == sale_region)
            and (mobile is None or p.mobile is mobile)
        ]
        if not candidates:
            candidates = list(self._profiles)
        if seed is not None:
            # 显式给种子时用可复现的随机源(便于测试与复现)
            return random.Random(seed).choice(candidates)
        # 默认用密码学安全随机源,避免设备选择序列可被预测
        return secrets.choice(candidates)

    # ------------------------------------------------------------------ 校验

    def validate(self, profile: DeviceProfile) -> list[str]:
        """校验单个档案的一致性。

        :param profile: 待校验档案。
        :return: 违规描述列表;为空表示一致。
        """
        issues: list[str] = []
        ua = profile.ua.lower()

        # 1) 移动设备必须有触摸,且 UA 必须含 Mobile
        if profile.mobile:
            if not profile.has_touch:
                issues.append(f"{profile.name}: 移动设备缺少触摸支持")
            if "mobile" not in ua:
                issues.append(f"{profile.name}: 移动设备 UA 缺少 'Mobile' 段")
            if profile.viewport[0] > 1024:
                issues.append(f"{profile.name}: 移动设备 viewport 过宽 {profile.viewport}")
        else:
            if profile.has_touch:
                issues.append(f"{profile.name}: 桌面设备不应有触摸支持")
            if "mobile" in ua:
                issues.append(f"{profile.name}: 桌面设备 UA 含 'Mobile' 段")

        # 2) locale / timezone 必须与地区匹配
        expected = REGIONS.get(profile.sale_region)
        if expected is None:
            issues.append(f"{profile.name}: 未知销售地区 {profile.sale_region!r}")
        else:
            locale, timezone = expected
            if profile.locale != locale:
                issues.append(
                    f"{profile.name}: locale {profile.locale!r} 与地区 "
                    f"{profile.sale_region} 期望的 {locale!r} 不一致")
            if profile.timezone != timezone:
                issues.append(
                    f"{profile.name}: timezone {profile.timezone!r} 与地区 "
                    f"{profile.sale_region} 期望的 {timezone!r} 不一致")

        # 3) 平台与 UA 必须匹配
        if "android" in ua and "Linux armv8l" not in profile.platform:
            issues.append(f"{profile.name}: Android UA 的 platform 应为 'Linux armv8l'")
        if "iphone" in ua or "ipad" in ua:
            if profile.platform not in ("iPhone", "iPad"):
                issues.append(f"{profile.name}: iOS UA 的 platform 应为 'iPhone'/'iPad'")
        if "windows nt" in ua and profile.platform != "Win32":
            issues.append(f"{profile.name}: Windows UA 的 platform 应为 'Win32'")
        if "macintosh" in ua and profile.platform != "MacIntel":
            issues.append(f"{profile.name}: macOS UA 的 platform 应为 'MacIntel'")
        if "x11; linux" in ua and profile.platform != "Linux x86_64":
            issues.append(f"{profile.name}: Linux UA 的 platform 应为 'Linux x86_64'")

        # 4) viewport 必须是正数
        if profile.viewport[0] <= 0 or profile.viewport[1] <= 0:
            issues.append(f"{profile.name}: viewport 非法 {profile.viewport}")

        return issues

    def validate_all(self) -> list[str]:
        """校验全部档案,返回所有违规描述。"""
        issues: list[str] = []
        for profile in self._profiles:
            issues.extend(self.validate(profile))
        return issues

    def regions(self) -> list[str]:
        """返回覆盖到的销售地区列表(去重)。"""
        seen: list[str] = []
        for p in self._profiles:
            if p.sale_region not in seen:
                seen.append(p.sale_region)
        return seen
