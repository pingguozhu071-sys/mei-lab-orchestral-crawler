"""文档数字同源:把 README / 台账里的规模数字改成脚本生成。

纪律(来自 kiana-crawler 的旧版整改记录 2.4.5):
> 文档中的引擎行数、标签数、覆盖率互相矛盾——数字依赖手工抄录。

做法:文档里用 ``<!-- AUTO:NUMBERS -->`` 标记块,本脚本在生成文档时把标记块
替换成实时统计的结果;``--check`` 模式只比对"文档里的数字是否与现状一致",
不一致就返回非 0(可挂 CI)。

用法::

    python tools/doc_numbers.py --check     # 校验文档数字是否过期
    python tools/doc_numbers.py --print     # 打印当前统计
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

#: 项目根目录
ROOT = Path(__file__).resolve().parent.parent

#: 文档里标记自动数字块的开始/结束标记
BLOCK_START = "<!-- AUTO:NUMBERS -->"
BLOCK_END = "<!-- /AUTO:NUMBERS -->"


def count_python_lines(directory: Path, exclude_tests: bool = False) -> tuple[int, int]:
    """统计 Python 文件数与行数。

    :param directory: 要统计的目录。
    :param exclude_tests: 是否排除 tests 目录。
    :return: ``(文件数, 总行数)``。
    """
    files = 0
    lines = 0
    for path in directory.rglob("*.py"):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        if exclude_tests and "tests" in path.relative_to(ROOT).parts:
            continue
        files += 1
        try:
            lines += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return files, lines


def count_test_functions() -> int:
    """统计测试函数数量(``def test_*``)。"""
    total = 0
    tests_dir = ROOT / "tests"
    if not tests_dir.is_dir():
        return 0
    pattern = re.compile(r"^\s*def\s+test_", re.MULTILINE)
    for path in tests_dir.rglob("test_*.py"):
        try:
            total += len(pattern.findall(path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return total


def collect() -> dict[str, Any]:
    """采集当前工程的关键数字。

    :return: 数字字典。
    """
    pkg_files, pkg_lines = count_python_lines(ROOT / "meilab")
    test_files, test_lines = count_python_lines(ROOT / "tests")
    modules = [
        path for path in (ROOT / "meilab").rglob("*.py")
        if path.name != "__init__.py" and "__pycache__" not in path.parts
    ]
    rules = list((ROOT / "rules" / "sites").glob("*.yaml"))
    return {
        "包内模块数": len(modules),
        "包内代码行": pkg_lines,
        "测试文件数": test_files,
        "测试代码行": test_lines,
        "测试函数数": count_test_functions(),
        "站点规则数": len(rules),
    }


def render_block(numbers: dict[str, Any]) -> str:
    """把数字字典渲染成 markdown 表格块。

    :param numbers: 数字字典。
    :return: markdown 文本。
    """
    lines = [BLOCK_START, "", "| 指标 | 数值 |", "|---|---|"]
    for key, value in numbers.items():
        lines.append(f"| {key} | {value} |")
    lines.extend(["", BLOCK_END])
    return "\n".join(lines)


def check_document(path: Path) -> tuple[bool, list[str]]:
    """校验文档里的自动数字块是否与现状一致。

    :param path: 文档路径。
    :return: ``(是否一致, 问题清单)``。
    """
    if not path.is_file():
        return True, []
    text = path.read_text(encoding="utf-8")
    if BLOCK_START not in text:
        return True, []  # 没有自动块,不管
    problems: list[str] = []
    numbers = collect()
    for key, value in numbers.items():
        # 在自动块范围内找形如 "| 指标 | 数字 |" 的行
        pattern = re.compile(rf"\|\s*{re.escape(key)}\s*\|\s*(\d+)\s*\|")
        for match in pattern.finditer(text):
            if int(match.group(1)) != int(value):
                problems.append(
                    f"{path.name}: 「{key}」文档写 {match.group(1)},实际 {value}"
                )
    return (not problems), problems


def write_document(path: Path, numbers: dict[str, Any]) -> bool:
    """把文档里的自动数字块替换成实时统计(有改动才写盘)。

    :param path: 文档路径。
    :param numbers: 数字字典。
    :return: 是否发生了改动。
    """
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if BLOCK_START not in text or BLOCK_END not in text:
        return False
    start = text.index(BLOCK_START)
    end = text.index(BLOCK_END) + len(BLOCK_END)
    updated = text[:start] + render_block(numbers) + text[end:]
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    """命令行入口。

    :return: 退出码(校验不一致返回 1)。
    """
    parser = argparse.ArgumentParser(description="文档数字同源工具")
    parser.add_argument("--check", action="store_true", help="校验文档数字是否过期")
    parser.add_argument("--print", action="store_true", help="打印当前统计")
    parser.add_argument("--write", action="store_true", help="把实时数字写回文档的自动块")
    args = parser.parse_args()

    numbers = collect()
    if args.print:
        print(json.dumps(numbers, ensure_ascii=False, indent=2))
        return 0
    if args.write:
        changed = [
            name for name in ("README.md", "PROJECT_STATE.md")
            if write_document(ROOT / name, numbers)
        ]
        if changed:
            print(f"[OK] 已更新:{', '.join(changed)}")
        else:
            print("[OK] 文档数字本来就是最新的")
        return 0
    if args.check:
        problems: list[str] = []
        for name in ("README.md", "PROJECT_STATE.md"):
            ok, issues = check_document(ROOT / name)
            if not ok:
                problems.extend(issues)
        if problems:
            for problem in problems:
                print(f"[FAIL] {problem}")
            return 1
        print("[OK] 文档里的数字与代码现状一致")
        return 0
    print(render_block(numbers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
