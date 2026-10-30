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
    if payload.get("urls"):
        console.print("\n[cyan]已抓取的页面:[/cyan]")
        for item in payload["urls"]:
            console.print(f"  - {item}")
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


#: 交互式会话的内置命令
SHELL_COMMANDS = {
    ":help": "显示帮助",
    ":stats": "查看任务/仓库统计",
    ":export [格式]": "导出内容(jsonl/csv/markdown/xlsx)",
    ":latest": "显示上一次抓取的正文摘要",
    ":quit": "退出会话",
}


def _normalize_pasted(text: str) -> str:
    """把粘贴的文本整理成 URL(去空白、引号、句末标点)。

    :param text: 原始输入。
    :return: 清理后的字符串。
    """
    cleaned = (text or "").strip().strip("'\"`<>")
    cleaned = cleaned.rstrip(".,;!?，。；！？")
    return cleaned.strip()


def _run_shell(engine: Engine) -> None:
    """交互式会话:粘贴链接按 Enter 立刻爬。

    :param engine: 已构造好的引擎(生命周期由调用方负责关闭)。
    """
    from .workers.base import check_url_safe

    console.print("[bold cyan]mei-lab-orchestral-crawler 交互窗口[/bold cyan]")
    console.print("粘贴一条链接按 [bold]Enter[/bold] 即可抓取;输入 [bold]:help[/bold] 看命令,"
                  "[bold]:quit[/bold] 退出。\n")

    last_url = ""
    while True:
        try:
            raw = console.input("[bold green]url>[/bold green] ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]会话结束。[/yellow]")
            return

        text = _normalize_pasted(raw)
        if not text:
            continue

        # 内置命令
        if text.startswith(":"):
            command = text.lower()
            if command in (":q", ":quit", ":exit"):
                console.print("[yellow]会话结束。[/yellow]")
                return
            if command == ":help":
                for key, desc in SHELL_COMMANDS.items():
                    console.print(f"  [cyan]{key:<16}[/cyan] {desc}")
                continue
            if command == ":stats":
                payload = engine.stats()
                console.print(f"任务: {json.dumps(payload.get('tasks', {}), ensure_ascii=False)}")
                console.print(f"仓库: {json.dumps(payload.get('vault', {}), ensure_ascii=False)}")
                console.print(f"通道: {json.dumps(payload.get('channels', {}), ensure_ascii=False)}")
                continue
            if command.startswith(":export"):
                parts = text.split()
                fmt = parts[1] if len(parts) > 1 else "markdown"
                result = engine.export(fmt)
                if result.get("error"):
                    console.print(f"[red]导出失败:[/red] {result['error']}")
                else:
                    console.print(f"[green]已导出[/green] {result['count']} 条 → {len(result['files'])} 个文件")
                continue
            if command == ":latest":
                episode = engine.latest_episode(last_url) if last_url else None
                if episode is None:
                    console.print("[yellow]还没有抓取记录。[/yellow]")
                else:
                    console.print(f"[cyan]{episode.title or episode.url}[/cyan] "
                                  f"({len(episode.content)} 字符 / {episode.channel})")
                    console.print(episode.content[:500] or "(无正文)")
                continue
            console.print(f"[red]未知命令[/red] {text}(输入 :help 查看)")
            continue

        # 粘贴的可能是整段文本,取出其中第一个看起来像 URL 的片段
        candidate = text.split()[0] if text.split() else text
        if "://" not in candidate:
            candidate = text

        # 安全校验:只允许 http/https,拒绝 localhost/环回/私网/保留地址
        safe, why = check_url_safe(candidate)
        if not safe:
            console.print(f"[red]拒绝抓取[/red] {candidate}\n  原因: {why}")
            continue

        console.print(f"[dim]抓取中…[/dim] {candidate}")
        outcome = engine.crawl_now(candidate)
        last_url = str(outcome.get("url") or candidate)
        if outcome.get("ok"):
            episode = engine.latest_episode(last_url)
            title = episode.title if episode else ""
            length = len(episode.content) if episode else 0
            console.print(
                f"[green]✓[/green] {outcome.get('channel')} 通道 | {title or '(无标题)'} | "
                f"{length} 字符 | {outcome.get('elapsed', 0):.1f}s"
            )
        else:
            console.print(f"[red]✗[/red] {outcome.get('error') or '抓取失败'}")


@cli.command()
@click.option("--db", default=None, help="队列数据库路径")
@click.option("--output", "-o", default="data", help="输出目录")
@click.option("--http-workers", default=2, type=int, help="HTTP Worker 数量")
@click.option("--browser-workers", default=1, type=int, help="浏览器 Worker 数量")
@click.option("--delay", default=1.5, type=float, help="请求最小间隔(秒,下限 1.5)")
@click.option("--timeout", default=20.0, type=float, help="单次抓取超时(秒)")
@click.option("--headless/--headed", default=True, help="浏览器是否无头运行")
@click.option("--ignore-robots/--respect-robots", default=False, help="是否忽略 robots.txt")
def shell(
    db: str | None,
    output: str,
    http_workers: int,
    browser_workers: int,
    delay: float,
    timeout: float,
    headless: bool,
    ignore_robots: bool,
) -> None:
    """交互窗口:粘贴链接按 Enter 直接爬。"""
    engine = _engine(db, output, http_workers, browser_workers, delay, timeout,
                     headless, not ignore_robots, False)
    try:
        _run_shell(engine)
    finally:
        engine.close()


def main() -> None:
    """CLI 入口(被 pyproject 的 ``mel`` 脚本调用)。"""
    try:
        cli()
    except KeyboardInterrupt:
        console.print("[yellow]已中断[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
