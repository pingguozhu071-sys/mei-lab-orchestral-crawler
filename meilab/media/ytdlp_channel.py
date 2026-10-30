"""通用 yt-dlp 通道:一条命令打通 yt-dlp 支持的所有站点。

这是媒体通道的**通用兜底**:五平台专用解析器没命中、或者解析失败时,一律走这里
(YouTube 等任意公开视频站也在其覆盖范围内)。

实现要点:
* 用 ``sys.executable -m yt_dlp`` 调用,保证与当前虚拟环境同一份依赖;
* ``--continue`` 断点续传、``.part`` 临时文件由 yt-dlp 自己管理;
* **产物认定用"目录快照差集"**:下载前记录目录内容,下载后只认新增文件,
  避免把"目录里本来就有的旧文件"冒充本次成果;
* JS 运行时:优先 deno,其次 node,都没有就不加参数(yt-dlp 会自行降级);
* 任何失败都返回可读原因,**不冒充成功**。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..workers.http import ensure_ascii_ca_bundle


@dataclass
class YtdlpResult:
    """yt-dlp 调用的结果。"""

    ok: bool = False
    info: dict[str, Any] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    error: str = ""
    command: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    stderr_tail: str = ""


def ytdlp_version() -> str | None:
    """返回 yt-dlp 版本(未安装返回 ``None``)。

    模块本身没有 ``__version__`` 属性,只能用包元数据查。
    """
    try:
        from importlib.metadata import version

        return version("yt-dlp")
    except Exception:
        return None


def ytdlp_available() -> bool:
    """yt-dlp 是否可用。"""
    return ytdlp_version() is not None


def js_runtime_args() -> list[str]:
    """探测可用的 JS 运行时,给出 yt-dlp 参数。

    :return: 参数列表(没有可用运行时时为空)。
    """
    for runtime in ("deno", "node"):
        if shutil.which(runtime):
            return ["--js-runtimes", runtime]
    return []


def _base_env() -> dict[str, str]:
    """构造子进程环境变量(含 CA bundle 修复)。"""
    env = dict(os.environ)
    ca = ensure_ascii_ca_bundle()
    if ca:
        env["CURL_CA_BUNDLE"] = ca
    return env


def _run(args: list[str], timeout: float) -> tuple[int, str, str]:
    """执行 yt-dlp 子进程。

    :param args: 参数列表。
    :param timeout: 超时(秒)。
    :return: ``(返回码, stdout, stderr)``。
    """
    command = [sys.executable, "-m", "yt_dlp", *args]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_base_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"yt-dlp 超时({timeout}s)"
    except FileNotFoundError as exc:
        return 127, "", f"无法启动 yt-dlp: {exc!r}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def resolve(
    url: str,
    *,
    cookies: str | None = None,
    timeout: float = 120.0,
) -> YtdlpResult:
    """只解析元数据,不下载。

    :param url: 媒体页面 URL 或直链。
    :param cookies: 用户自备 cookie 文件路径。
    :param timeout: 超时(秒)。
    :return: 解析结果(``info`` 为 yt-dlp 的 JSON 元数据)。
    """
    result = YtdlpResult()
    args = ["--dump-json", "--no-playlist", "--no-warnings", *js_runtime_args()]
    if cookies:
        args += ["--cookies", cookies]
    args.append(url)
    result.command = args

    started = time.time()
    code, out, err = _run(args, timeout)
    result.elapsed = time.time() - started
    result.stderr_tail = err[-800:]
    if code != 0:
        result.error = err.strip().splitlines()[-1] if err.strip() else f"yt-dlp 退出码 {code}"
        return result
    try:
        payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except (ValueError, IndexError) as exc:
        result.error = f"yt-dlp 输出不是合法 JSON: {exc!r}"
        return result
    result.info = payload if isinstance(payload, dict) else {}
    result.ok = bool(result.info)
    if not result.ok:
        result.error = "yt-dlp 未返回元数据"
    return result


def download(
    url: str,
    output_dir: str | os.PathLike[str],
    *,
    cookies: str | None = None,
    audio_only: bool = False,
    filename_template: str = "%(title).60s [%(id)s].%(ext)s",
    timeout: float = 3600.0,
    extra_args: list[str] | None = None,
    progress_cb: Callable[[str], None] | None = None,
    test_only: bool = False,
) -> YtdlpResult:
    """下载媒体(通用通道)。

    :param url: 媒体页面 URL 或直链。
    :param output_dir: 输出目录。
    :param cookies: 用户自备 cookie 文件路径。
    :param audio_only: 只要音频(转 m4a/mp3)。
    :param filename_template: 输出文件名模板。
    :param timeout: 超时(秒)。
    :param extra_args: 额外参数。
    :param progress_cb: 进度回调(收到 yt-dlp 的逐行输出)。
    :param test_only: 只下载前 ~10KiB 做链路验证(yt-dlp 的 ``--test``)。
    :return: 下载结果,``files`` 为本次新增的产物路径。
    """
    result = YtdlpResult()
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    args = [
        "--no-playlist",
        "--continue",
        "--newline",
        "--no-warnings",
        "--retries", "3",
        "--fragment-retries", "10",
        "-o", str(out_dir / filename_template),
        *js_runtime_args(),
    ]
    if test_only:
        args.append("--test")
    if cookies:
        args += ["--cookies", cookies]
    if audio_only:
        args += ["-x", "--audio-format", "m4a"]
    if extra_args:
        args += list(extra_args)
    args.append(url)
    result.command = args

    before = {p for p in out_dir.iterdir()}
    started = time.time()
    code, out, err = _run(args, timeout)
    result.elapsed = time.time() - started
    result.stderr_tail = err[-800:]
    if progress_cb:
        for line in (out or "").splitlines()[-20:]:
            progress_cb(line)

    after = {p for p in out_dir.iterdir()}
    # 只认"完整产物":yt-dlp 的中间态(.part / .ytdl)不算成果
    produced = sorted(
        p for p in (after - before)
        if p.is_file() and not p.name.endswith(".part") and not p.name.endswith(".ytdl")
    )
    result.files = [str(p) for p in produced]
    result.partial = sorted(
        str(p) for p in (after - before)
        if p.is_file() and (p.name.endswith(".part") or p.name.endswith(".ytdl"))
    )

    if code != 0:
        result.error = err.strip().splitlines()[-1] if err.strip() else f"yt-dlp 退出码 {code}"
        if result.partial:
            result.error += f"(已保留半成品 {len(result.partial)} 个,重跑可续传)"
        return result
    if not result.files:
        result.error = "yt-dlp 未产出任何文件(可能该链接不支持或需要 cookie)"
        if result.partial:
            result.error += f"(已保留半成品 {len(result.partial)} 个,重跑可续传)"
        return result
    result.ok = True
    return result


def probe_supported(url: str, *, timeout: float = 60.0) -> tuple[bool, str]:
    """探测某个 URL 是否被 yt-dlp 支持(不下载)。

    :param url: 待探测 URL。
    :param timeout: 超时(秒)。
    :return: ``(是否支持, 原因)``。
    """
    result = resolve(url, timeout=timeout)
    if result.ok:
        return True, "支持"
    return False, result.error or "不支持"
