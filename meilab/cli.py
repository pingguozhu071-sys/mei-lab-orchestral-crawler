"""命令行入口:``mel crawl`` / ``mel resume`` / ``mel stats`` / ``mel export``。

用 click 实现,输出用 rich 渲染。
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from .engine import Engine, EngineConfig

console = Console()


def _engine(
    db: str | None,
    output: str,
    http_workers: int,
    browser_workers: int,
    delay: float,
    timeout: float,
    headless: bool,
    obey_robots: bool,
    verbose: bool,
) -> Engine:
    """按命令行参数构造引擎。"""
    config = EngineConfig(
        db_path=db or "",
        output_dir=output,
        http_workers=http_workers,
        browser_workers=browser_workers,
        delay=delay,
        timeout=timeout,
        headless=headless,
        obey_robots=obey_robots,
        verbose=verbose,
    )
    return Engine(config)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="meilab-orchestral-crawler", prog_name="mel")
def cli() -> None:
    """mei-lab-orchestral-crawler:分层调度式智能爬虫框架。"""


@cli.command()
@click.option("--url", "-u", multiple=True, required=True, help="种子 URL(可重复)")
@click.option("--max", "max_tasks", default=0, type=int, help="最多处理多少任务,0 为不限")
@click.option("--db", default=None, help="队列数据库路径")
@click.option("--output", "-o", default="data", help="输出目录")
@click.option("--http-workers", default=2, type=int, help="HTTP Worker 数量")
@click.option("--browser-workers", default=1, type=int, help="浏览器 Worker 数量")
@click.option("--delay", default=1.5, type=float, help="请求最小间隔(秒,下限 1.5)")
@click.option("--timeout", default=20.0, type=float, help="单次抓取超时(秒)")
@click.option("--headless/--headed", default=True, help="浏览器是否无头运行")
@click.option("--ignore-robots/--respect-robots", default=False,
              help="是否忽略 robots.txt(默认遵守)")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出结果")
@click.option("--verbose", "-v", is_flag=True, help="输出更多细节")
def crawl(
    url: tuple[str, ...],
    max_tasks: int,
    db: str | None,
    output: str,
    http_workers: int,
    browser_workers: int,
    delay: float,
    timeout: float,
    headless: bool,
    ignore_robots: bool,
    as_json: bool,
    verbose: bool,
) -> None:
    """抓取给定 URL(分层调度 + 多格式提取 + 反检测)。"""
    engine = _engine(db, output, http_workers, browser_workers, delay, timeout,
                     headless, not ignore_robots, verbose)
    try:
        summary = engine.crawl(list(url), max_tasks=max_tasks)
        payload: dict[str, Any] = summary.as_dict()
    finally:
        engine.close()

    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    table = Table(title="抓取结果", show_lines=False)
    table.add_column("指标", style="cyan")
    table.add_column("值", style="white")
    for key in ("queued", "dispatched", "succeeded", "failed", "blocked", "unchanged", "elapsed"):
        table.add_row(key, str(payload.get(key)))
    console.print(table)
    if payload.get("channel_counts"):
        console.print("通道分布:", payload["channel_counts"])
    if payload.get("errors"):
        console.print(f"[yellow]前 {len(payload['errors'])} 条问题:[/yellow]")
        for item in payload["errors"]:
            console.print(f"  - {item}")


@cli.command()
@click.option("--max", "max_tasks", default=0, type=int, help="最多处理多少任务")
@click.option("--db", default=None, help="队列数据库路径")
@click.option("--output", "-o", default="data", help="输出目录")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出结果")
def resume(max_tasks: int, db: str | None, output: str, as_json: bool) -> None:
    """断点续传:继续处理队列里未完成的任务。"""
    engine = _engine(db, output, 2, 1, 1.5, 20.0, True, True, False)
    try:
        payload = engine.resume(max_tasks=max_tasks).as_dict()
    finally:
        engine.close()
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    console.print(f"续传完成:成功 {payload['succeeded']} / 失败 {payload['failed']} "
                  f"/ 共 {payload['queued']} 个任务,{payload['elapsed']} 秒")


@cli.command()
@click.option("--db", default=None, help="队列数据库路径")
@click.option("--output", "-o", default="data", help="输出目录")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出结果")
def stats(db: str | None, output: str, as_json: bool) -> None:
    """查看任务 / Episode / 仓库统计。"""
    engine = _engine(db, output, 0, 0, 1.5, 20.0, True, True, False)
    try:
        payload = engine.stats()
    finally:
        engine.close()

    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return

    table = Table(title="统计", show_lines=False)
    table.add_column("分类", style="cyan")
    table.add_column("明细", style="white")
    table.add_row("任务状态", json.dumps(payload.get("tasks", {}), ensure_ascii=False))
    table.add_row("Episode", json.dumps(payload.get("episodes", {}), ensure_ascii=False))
    table.add_row("仓库", json.dumps(payload.get("vault", {}), ensure_ascii=False))
    table.add_row("通道", json.dumps(payload.get("channels", {}), ensure_ascii=False))
    ratio = payload.get("channel_ratio", {})
    table.add_row("通道占比", f"轻量 {ratio.get('lightweight', 0):.2%} / 浏览器 {ratio.get('browser', 0):.2%}")
    console.print(table)
    if payload.get("domains"):
        console.print("域名分布:", ", ".join(payload["domains"][:10]))


@cli.command()
@click.option("--format", "fmt", default="markdown",
              type=click.Choice(["jsonl", "csv", "markdown", "xlsx"]), help="导出格式")
@click.option("--output", "-o", default="./data", help="输出目录")
@click.option("--db", default=None, help="队列数据库路径")
def export(fmt: str, output: str, db: str | None) -> None:
    """把仓库内容导出为 JSONL / CSV / Markdown / XLSX。"""
    engine = _engine(db, output, 0, 0, 1.5, 20.0, True, True, False)
    try:
        payload = engine.export(fmt, output)
    finally:
        engine.close()
    if payload.get("error"):
        console.print(f"[red]导出失败:[/red] {payload['error']}")
        raise SystemExit(1)
    console.print(f"[green]导出完成[/green]: {payload['count']} 条记录,{len(payload['files'])} 个文件")
    for path in payload["files"][:10]:
        console.print(f"  - {path}")


def main() -> None:
    """CLI 入口(被 pyproject 的 ``mel`` 脚本调用)。"""
    try:
        cli()
    except KeyboardInterrupt:
        console.print("[yellow]已中断[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
