"""stealth 子包:反检测层(设备档案 / 指纹注入 / 行为模拟 / 网络守卫)。"""

from .behavior import BehaviorSimulator, TypingPlan, sample_paths
from .inject import apply_profile, build_init_script, harden_page
from .netguard import NetGuard, hosts_of
from .profiles import (
    DEVICE_SPECS,
    PLATFORM_FONTS,
    PLATFORM_GPU,
    REGIONS,
    DeviceLibrary,
    DeviceSpec,
)

__all__ = [
    "DEVICE_SPECS",
    "PLATFORM_FONTS",
    "PLATFORM_GPU",
    "REGIONS",
    "BehaviorSimulator",
    "DeviceLibrary",
    "DeviceSpec",
    "NetGuard",
    "TypingPlan",
    "apply_profile",
    "build_init_script",
    "harden_page",
    "hosts_of",
    "sample_paths",
]
