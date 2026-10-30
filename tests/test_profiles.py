"""设备档案库单测:遍历全库断言一致性。

一致性硬约束:
* Android/iOS 必带触摸,移动 UA 必配移动 viewport;
* 桌面设备必不带触摸、UA 不得含 "Mobile";
* locale 与 timezone 必须与 sale_region 匹配(ja-JP ↔ Asia/Tokyo);
* 平台属性必须与 UA 家族匹配。
"""

from __future__ import annotations

import pytest

from meilab.core.types import DeviceProfile
from meilab.stealth.profiles import DEVICE_SPECS, REGIONS, DeviceLibrary


@pytest.fixture(scope="module")
def library() -> DeviceLibrary:
    """全库档案库(模块级,避免重复构造)。"""
    return DeviceLibrary()


def test_library_has_enough_devices(library: DeviceLibrary) -> None:
    """至少 20 台设备。"""
    assert len(library) >= 20


def test_all_profiles_are_consistent(library: DeviceLibrary) -> None:
    """遍历全库,任何不一致都必须暴露出来。"""
    issues = library.validate_all()
    assert issues == [], "设备档案存在一致性问题:\n" + "\n".join(issues)


def test_names_unique(library: DeviceLibrary) -> None:
    """档案名唯一。"""
    names = [p.name for p in library]
    assert len(names) == len(set(names))


def test_mobile_devices_have_touch_and_mobile_viewport(library: DeviceLibrary) -> None:
    """移动设备:触摸 + 窄 viewport + UA 含 Mobile。"""
    mobiles = [p for p in library if p.mobile]
    assert len(mobiles) >= 8, "移动设备数量不足"
    for p in mobiles:
        assert p.has_touch is True
        assert "mobile" in p.ua.lower()
        assert p.viewport[0] <= 1024
        assert p.is_mobile is True


def test_desktop_devices_have_no_touch(library: DeviceLibrary) -> None:
    """桌面设备:无触摸,UA 不含 Mobile。"""
    desktops = [p for p in library if not p.mobile]
    assert len(desktops) >= 8
    for p in desktops:
        assert p.has_touch is False
        assert "mobile" not in p.ua.lower()
        assert p.is_mobile is False


def test_locale_timezone_match_sale_region(library: DeviceLibrary) -> None:
    """locale 与 timezone 必须与销售地区匹配。"""
    for p in library:
        locale, tz = REGIONS[p.sale_region]
        assert p.locale == locale
        assert p.timezone == tz


def test_ja_jp_maps_to_tokyo(library: DeviceLibrary) -> None:
    """工程书点名的硬约束:ja-JP ↔ Asia/Tokyo。"""
    jp = [p for p in library if p.sale_region == "JP"]
    assert jp, "档案库应包含日本区设备"
    for p in jp:
        assert p.locale == "ja-JP"
        assert p.timezone == "Asia/Tokyo"


def test_platform_matches_ua_family(library: DeviceLibrary) -> None:
    """平台属性与 UA 家族匹配。"""
    for p in library:
        ua = p.ua.lower()
        if "android" in ua:
            assert p.platform == "Linux armv8l"
        elif "iphone" in ua or "ipad" in ua:
            assert p.platform in ("iPhone", "iPad")
        elif "windows nt" in ua:
            assert p.platform == "Win32"
        elif "macintosh" in ua:
            assert p.platform == "MacIntel"
        elif "x11; linux" in ua:
            assert p.platform == "Linux x86_64"


def test_covers_all_platform_families(library: DeviceLibrary) -> None:
    """覆盖 Win / macOS / Linux / Android / iOS。"""
    families = {spec.family for spec in DEVICE_SPECS}
    assert {"windows", "macos", "linux", "android", "ios"} <= families


def test_pick_filters_by_region_and_mobile(library: DeviceLibrary) -> None:
    """选择策略支持按地区/移动端过滤。"""
    p = library.pick(sale_region="JP", mobile=True, seed=1)
    assert p.sale_region == "JP"
    assert p.mobile is True

    p2 = library.pick(sale_region="JP", mobile=False, seed=1)
    assert p2.sale_region == "JP"
    assert p2.mobile is False


def test_pick_is_reproducible_with_seed(library: DeviceLibrary) -> None:
    """给定种子时选择可复现。"""
    a = library.pick(seed=42).name
    b = library.pick(seed=42).name
    assert a == b


def test_context_options_are_playwright_shaped(library: DeviceLibrary) -> None:
    """档案能直接转成 Playwright new_context 参数。"""
    p: DeviceProfile = library.pick(seed=7)
    opts = p.as_context_options()
    assert set(opts) >= {"user_agent", "viewport", "timezone_id", "locale", "has_touch"}
    assert opts["viewport"]["width"] == p.viewport[0]
    assert opts["timezone_id"] == p.timezone


def test_validate_detects_broken_profile(library: DeviceLibrary) -> None:
    """校验器能抓出人为制造的不一致档案。

    注:``DeviceProfile`` 模型层已经拦下"移动设备没有触摸"这种硬矛盾
    (构造时直接抛 ValidationError),因此这里构造的是**模型层放行、
    但语义层仍不自洽**的档案(桌面宽度 viewport + locale 与地区不符 + 平台与 UA 不符)。
    """
    broken = DeviceProfile(
        name="broken",
        ua="Mozilla/5.0 (Linux; Android 13; Pixel 7) Mobile Safari/537.36",
        viewport=(1920, 1080),
        timezone="Asia/Tokyo",
        locale="en-US",
        gpu_vendor="Qualcomm",
        gpu_renderer="Adreno (TM) 740",
        fonts=["Roboto"],
        has_touch=True,
        platform="Win32",
        sale_region="JP",
        mobile=True,
        is_mobile=True,
    )
    issues = library.validate(broken)
    assert any("viewport" in i for i in issues)
    assert any("locale" in i for i in issues)
    assert any("platform" in i for i in issues)


def test_model_rejects_mobile_without_touch() -> None:
    """模型层直接拒绝"移动设备没有触摸"。"""
    with pytest.raises(ValueError):
        DeviceProfile(
            name="no_touch",
            ua="Mozilla/5.0 (Linux; Android 13; Pixel 7) Mobile Safari/537.36",
            viewport=(412, 915),
            timezone="Asia/Tokyo",
            locale="ja-JP",
            gpu_vendor="Qualcomm",
            gpu_renderer="Adreno (TM) 740",
            fonts=["Roboto"],
            has_touch=False,
            platform="Linux armv8l",
            sale_region="JP",
            mobile=True,
            is_mobile=True,
        )
