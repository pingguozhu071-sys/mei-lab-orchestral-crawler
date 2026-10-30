"""人类行为模拟:打字节奏、停留时长、鼠标轨迹。

自动化的行为特征非常明显:等间隔按键、瞬移光标、页面停留时间恒定。
本模块用三种统计模型把这三处"机器味"去掉:

* **打字节奏**:按键间隔服从**泊松过程**(指数分布的到达间隔),
  而不是机械的固定间隔;真人打字速度还会随词边界与停顿波动。
* **停留时长**:用**对数正态分布**——真人阅读/浏览时长是重尾分布
  (大多数人几十秒,少数人几分钟),正态分布做不到这种长尾。
* **鼠标轨迹**:内置 5 条录制样本 + **贝塞尔曲线**生成,按 **7:3** 混合。
  纯程序生成的贝塞尔曲线规整得不像人;纯录制样本又无法适应任意起终点。

随机源说明:行为模拟**不是**密码学用途,使用 ``random.Random`` 实例;
未显式给种子时,种子来自 ``secrets.randbits``(不可预测),保证同一 Worker
的连续动作之间存在真实的随机性,又不破坏"给定种子可复现"的排障能力。
"""

from __future__ import annotations

import math
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

#: 真人平均打字速度(字符/秒)。200~400 WPM ≈ 7~14 字符/秒,取中值偏低更安全。
DEFAULT_TYPING_RATE = 8.0

#: 鼠标轨迹里"录制样本"与"贝塞尔生成"的混合比例
SAMPLE_RATIO = 0.7

#: 内置 5 条录制轨迹(归一化坐标,x/y ∈ [0,1],左上为原点)
RECORDED_PATHS: list[list[tuple[float, float]]] = [
    # 1) 近直线,带轻微抖动
    [(0.0, 0.0), (0.25, 0.06), (0.5, 0.03), (0.75, 0.08), (1.0, 0.0)],
    # 2) 明显上凸弧线
    [(0.0, 0.0), (0.2, 0.25), (0.5, 0.42), (0.8, 0.22), (1.0, 0.0)],
    # 3) 明显下凹弧线
    [(0.0, 0.0), (0.2, 0.10), (0.5, 0.30), (0.8, 0.18), (1.0, 0.0)],
    # 4) S 形曲线
    [(0.0, 0.0), (0.2, 0.30), (0.45, 0.05), (0.7, 0.35), (1.0, 0.0)],
    # 5) 折线抖动(人手不稳)
    [(0.0, 0.0), (0.15, 0.12), (0.3, 0.04), (0.5, 0.20), (0.7, 0.06), (0.85, 0.16), (1.0, 0.0)],
]


@dataclass
class TypingPlan:
    """一次打字的节奏计划。"""

    text: str = ""
    delays: list[float] = field(default_factory=list)
    word_pauses: list[float] = field(default_factory=list)

    @property
    def total_time(self) -> float:
        """打完这段文本需要的总时长(秒)。"""
        return sum(self.delays) + sum(self.word_pauses)

    def timeline(self) -> list[float]:
        """展开成"每个字符之后应等待的秒数"序列(含词间停顿)。"""
        out: list[float] = []
        for index, delay in enumerate(self.delays):
            out.append(delay)
            if index < len(self.word_pauses):
                out.append(self.word_pauses[index])
        return out


class BehaviorSimulator:
    """生成类人的行为序列。"""

    def __init__(
        self,
        seed: int | None = None,
        typing_rate: float = DEFAULT_TYPING_RATE,
        dwell_median: float = 2.5,
        dwell_sigma: float = 0.85,
        sample_ratio: float = SAMPLE_RATIO,
    ) -> None:
        """初始化模拟器。

        :param seed: 随机种子;为 ``None`` 时用 ``secrets`` 生成不可预测的种子。
        :param typing_rate: 平均打字速度(字符/秒)。
        :param dwell_median: 停留时长的中位数(秒)。
        :param dwell_sigma: 停留时长的对数正态 σ(越大长尾越重)。
        :param sample_ratio: 鼠标轨迹使用录制样本的比例(0~1)。
        """
        self.seed = seed if seed is not None else secrets.randbits(64)
        self.rng = random.Random(self.seed)
        self.typing_rate = max(1.0, float(typing_rate))
        self.dwell_median = max(0.1, float(dwell_median))
        self.dwell_sigma = max(0.05, float(dwell_sigma))
        self.sample_ratio = min(1.0, max(0.0, float(sample_ratio)))

    # ------------------------------------------------------------------ 打字

    def typing_plan(self, text: str) -> TypingPlan:
        """生成打字节奏计划(泊松到达间隔 + 词间停顿)。

        :param text: 要输入的文本。
        :return: 打字计划。
        """
        chars = list(text)
        if not chars:
            return TypingPlan(text=text)

        mean_interval = 1.0 / self.typing_rate
        delays: list[float] = []
        for index, char in enumerate(chars):
            # 泊松过程:到达间隔服从指数分布(均值 1/λ)
            interval = self.rng.expovariate(self.typing_rate)
            # 越往后越可能"卡壳",给一点长尾
            if self.rng.random() < 0.05:
                interval *= self.rng.uniform(1.8, 3.5)
            # 大写字母与标点需要额外按键时间
            if char.isupper() or char in "!@#$%^&*()_+{}:\"<>?":
                interval += mean_interval * self.rng.uniform(0.3, 0.9)
            delays.append(min(2.5, max(0.02, interval)))

        # 词间停顿:遇到空格且下一个字符是词首时,插入一次思考停顿
        word_pauses: list[float] = []
        for index in range(len(chars) - 1):
            pause = 0.0
            if chars[index] == " " and chars[index + 1] not in (" ", ""):
                pause = self.rng.lognormvariate(math.log(0.12), 0.6)
            elif chars[index] in ".,!?;:":
                pause = self.rng.lognormvariate(math.log(0.18), 0.7)
            word_pauses.append(min(1.5, max(0.0, pause)))

        return TypingPlan(text=text, delays=delays, word_pauses=word_pauses)

    def type_text(self, page: Any, text: str) -> float:
        """在页面上模拟逐字输入。

        :param page: Playwright ``Page``。
        :param text: 要输入的文本。
        :return: 实际耗时(秒)。
        """
        plan = self.typing_plan(text)
        started = time.time()
        delays = plan.timeline()
        for index, char in enumerate(text):
            try:
                page.keyboard.type(char)
            except Exception:
                break
            if index < len(delays):
                time.sleep(delays[index])
        return time.time() - started

    # ------------------------------------------------------------------ 停留

    def dwell_time(self, median: float | None = None, sigma: float | None = None) -> float:
        """采样一次"人在这页停留多久"。

        对数正态分布:``median * exp(sigma * Z)``,Z ~ N(0,1)。
        相比正态分布,它有真实的重尾(偶尔几分钟的停留)。

        :param median: 覆盖中位数(秒)。
        :param sigma: 覆盖对数标准差。
        :return: 停留时长(秒,上限 300 秒)。
        """
        med = self.dwell_median if median is None else max(0.1, float(median))
        sig = self.dwell_sigma if sigma is None else max(0.05, float(sigma))
        value = med * math.exp(sig * self.rng.gauss(0.0, 1.0))
        return min(300.0, max(0.2, value))

    def wait_dwell(self, median: float | None = None, sigma: float | None = None) -> float:
        """按采样到的停留时长真实等待(而不是固定 sleep)。

        :param median: 覆盖中位数(秒)。
        :param sigma: 覆盖对数标准差。
        :return: 实际等待秒数。
        """
        seconds = self.dwell_time(median, sigma)
        time.sleep(seconds)
        return seconds

    # ------------------------------------------------------------------ 鼠标

    def mouse_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        steps: int = 24,
    ) -> list[tuple[float, float]]:
        """生成一条"人手"鼠标轨迹。

        按 7:3 混合:70% 用内置录制样本(形变后平移到起终点),
        30% 用三次贝塞尔曲线生成。

        :param start: 起点(像素坐标)。
        :param end: 终点(像素坐标)。
        :param steps: 轨迹点数量(最少 2)。
        :return: 轨迹点列表。
        """
        steps = max(2, int(steps))
        sx, sy = float(start[0]), float(start[1])
        ex, ey = float(end[0]), float(end[1])

        if self.rng.random() < self.sample_ratio:
            points = self._sample_path(sx, sy, ex, ey, steps)
        else:
            points = self._bezier_path(sx, sy, ex, ey, steps)
        return points

    def move_mouse(self, page: Any, start: tuple[float, float], end: tuple[float, float]) -> int:
        """在页面上模拟一次鼠标移动。

        :param page: Playwright ``Page``。
        :param start: 起点。
        :param end: 终点。
        :return: 实际移动的步数。
        """
        points = self.mouse_path(start, end)
        previous = start
        try:
            for point in points:
                page.mouse.move(point[0], point[1])
                time.sleep(self.rng.uniform(0.004, 0.018))
                previous = point
        except Exception:
            return len(points)
        return len(points)

    def scroll(self, page: Any, delta: float = 400.0, steps: int = 6) -> int:
        """模拟一次滚轮滚动(带缓动)。

        :param page: Playwright ``Page``。
        :param delta: 滚动总距离(像素)。
        :param steps: 分几步滚完。
        :return: 实际滚动步数。
        """
        steps = max(1, int(steps))
        per_step = delta / steps
        done = 0
        for index in range(steps):
            # 缓动:两头慢、中间快
            factor = 0.5 - 0.5 * math.cos(math.pi * (index + 1) / steps)
            amount = per_step * (0.6 + 0.8 * factor)
            try:
                page.mouse.wheel(0, amount)
            except Exception:
                break
            done += 1
            time.sleep(self.rng.uniform(0.03, 0.12))
        return done

    # ------------------------------------------------------------------ 内部

    def _sample_path(
        self,
        sx: float,
        sy: float,
        ex: float,
        ey: float,
        steps: int,
    ) -> list[tuple[float, float]]:
        """把一条录制样本变形后映射到起终点之间。"""
        sample = self.rng.choice(RECORDED_PATHS)
        dx = ex - sx
        dy = ey - sy
        points: list[tuple[float, float]] = []
        for index in range(steps):
            t = index / (steps - 1)
            # 在录制样本上按比例取点(线性插值)
            pos = t * (len(sample) - 1)
            left = int(math.floor(pos))
            right = min(left + 1, len(sample) - 1)
            frac = pos - left
            nx = sample[left][0] * (1 - frac) + sample[right][0] * frac
            ny = sample[left][1] * (1 - frac) + sample[right][1] * frac
            # 加一点随机抖动(人手不稳)
            jitter_x = self.rng.gauss(0.0, 1.2)
            jitter_y = self.rng.gauss(0.0, 1.2)
            points.append((sx + dx * nx + jitter_x, sy + dy * ny + jitter_y))
        points[-1] = (ex, ey)
        return points

    def _bezier_path(
        self,
        sx: float,
        sy: float,
        ex: float,
        ey: float,
        steps: int,
    ) -> list[tuple[float, float]]:
        """用三次贝塞尔曲线生成轨迹(控制点带随机偏移,避免每次都一模一样)。"""
        dx = ex - sx
        dy = ey - sy
        dist = max(1.0, math.hypot(dx, dy))
        # 垂直于主方向的单位向量
        perp_x = -dy / dist
        perp_y = dx / dist
        # 控制点沿主方向落在 1/3 与 2/3 处,垂直偏移量级与距离相关
        off1 = self.rng.gauss(0.0, dist * 0.12)
        off2 = self.rng.gauss(0.0, dist * 0.12)
        c1 = (sx + dx * 0.33 + perp_x * off1, sy + dy * 0.33 + perp_y * off1)
        c2 = (sx + dx * 0.66 + perp_x * off2, sy + dy * 0.66 + perp_y * off2)

        points: list[tuple[float, float]] = []
        for index in range(steps):
            t = index / (steps - 1)
            inv = 1.0 - t
            x = inv**3 * sx + 3 * inv**2 * t * c1[0] + 3 * inv * t**2 * c2[0] + t**3 * ex
            y = inv**3 * sy + 3 * inv**2 * t * c1[1] + 3 * inv * t**2 * c2[1] + t**3 * ey
            points.append((x, y))
        points[-1] = (ex, ey)
        return points


def describe_plan(text: str, seed: int | None = 7) -> TypingPlan:
    """便捷函数:给一段文本生成打字计划(供 CLI/调试查看)。

    :param text: 文本。
    :param seed: 随机种子。
    :return: 打字计划。
    """
    return BehaviorSimulator(seed=seed).typing_plan(text)


def sample_paths() -> Sequence[list[tuple[float, float]]]:
    """返回内置录制轨迹(只读副本)。"""
    return [list(p) for p in RECORDED_PATHS]
