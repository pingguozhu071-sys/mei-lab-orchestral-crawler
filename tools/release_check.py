"""发版门禁:一条命令跑 6 项检查,任一失败即阻止发布。

纪律(来自 kiana-crawler 的经验,写在这里当常量):
* **「没有告警」不等于「通过」** —— 每项检查必须给出明确状态;
* **无法得出结论的检查标记为「跳过」,不冒充「通过」**;
* **静态基线只允许下降,不允许回升**。

检查项:
1. 版本一致性(pyproject / `__version__` / README 声明一致);
2. Git 工作区干净;
3. 全量测试通过;
4. ruff 静态检查(基线只降不升);
5. 依赖漏洞审计(pip-audit,没装则跳过);
6. 密钥泄漏扫描(常见密钥形态的正则扫描)。

用法::

    python tools/release_check.py            # 全跑
    python tools/release_check.py --json     # 机器可读输出
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 项目根目录
ROOT = Path(__file__).resolve().parent.parent

#: 允许出现的密钥形态(用于泄漏扫描)
SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),                       # AWS Access Key
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                     # GitHub PAT
    re.compile(r"sk-[A-Za-z0-9]{32,}"),                     # OpenAI 风格 key
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),           # Slack token
)

#: 扫描时跳过的目录
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".pytest_tmp", "data", ".mimosa",
}

#: 允许 ruff 报告的错误数基线(只允许下降)
RUFF_BASELINE_FILE = ROOT / ".ruff_baseline.json"


@dataclass
class CheckResult:
    """一项检查的结果。"""

    name: str
    status: str = "pass"  # pass / fail / skip
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """是否失败(SKIP 不算失败)。"""
        return self.status == "fail"


def _run(cmd: list[str], cwd: Path = ROOT, timeout: float = 600.0) -> tuple[int, str, str]:
    """执行一条命令,返回 ``(返回码, stdout, stderr)``。"""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时({timeout}s): {cmd[0]}"


# ------------------------------------------------------------------ 各检查项

def check_version_consistency() -> CheckResult:
    """版本一致性:pyproject / `meilab.__version__` / README(若存在)必须一致。

    README 缺失时**不视为通过**,而是在 detail 里明确写出"文档侧校验已跳过",
    避免"没检查"被当成"检查过且通过"。
    """
    result = CheckResult("版本一致性")
    try:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if not match:
            result.status = "fail"
            result.detail = "pyproject.toml 里找不到 version"
            return result
        pyproject_version = match.group(1)
        sys.path.insert(0, str(ROOT))
        from meilab import __version__ as pkg_version

        if pyproject_version != pkg_version:
            result.status = "fail"
            result.detail = (
                f"版本不一致:pyproject={pyproject_version} 包内={pkg_version}"
            )
            return result

        readme_path = ROOT / "README.md"
        if readme_path.is_file():
            readme = readme_path.read_text(encoding="utf-8")
            if pyproject_version not in readme:
                result.status = "fail"
                result.detail = f"README 里没有出现版本 {pyproject_version}"
                return result
            result.detail = f"版本一致:{pyproject_version}"
        else:
            result.detail = (
                f"版本一致:{pyproject_version}(README 暂缺,文档侧校验已跳过)"
            )
        result.data = {"version": pyproject_version}
        return result
    except Exception as exc:
        result.status = "fail"
        result.detail = f"检查异常:{exc!r}"
        return result


def check_git_clean() -> CheckResult:
    """Git 工作区是否干净。"""
    result = CheckResult("Git 工作区干净")
    code, out, err = _run(["git", "status", "--porcelain"])
    if code != 0:
        result.status = "skip"
        result.detail = f"不是 Git 仓库或 git 不可用({err.strip()[:80]})"
        return result
    changed = [line for line in out.splitlines() if line.strip()]
    if changed:
        result.status = "fail"
        result.detail = f"有 {len(changed)} 个未提交改动"
        result.data = {"files": changed[:20]}
        return result
    result.detail = "工作区干净"
    return result


def check_tests() -> CheckResult:
    """全量测试是否通过。"""
    result = CheckResult("全量测试")
    code, out, err = _run(
        [sys.executable, "-m", "pytest", "tests", "-q"], timeout=1800.0
    )
    text = (out + err).strip()
    result.detail = text.splitlines()[-1] if text else ""
    if code != 0:
        result.status = "fail"
        result.data = {"output": text[-2000:]}
        return result
    result.data = {"output": text[-500:]}
    return result


def check_ruff_baseline() -> CheckResult:
    """ruff 静态检查:错误数不得超过基线(只允许下降)。"""
    result = CheckResult("ruff 静态基线")
    code, out, err = _run([sys.executable, "-m", "ruff", "check", "--output-format", "concise",
                           "meilab", "tests", "tools"])
    combined = out + err
    if "No module named ruff" in combined:
        result.status = "skip"
        result.detail = "未安装 ruff(可执行 pip install ruff)"
        return result
    # concise 格式每条诊断一行:``path:line:col: CODE message``;
    # 此前的解析器找 ": error"/行首 "E",与该格式永不匹配,错误数恒为 0(检查形同虚设)
    pattern = re.compile(r": [A-Z]+[0-9]+ ")
    errors = len([line for line in combined.splitlines() if pattern.search(line)])
    baseline = 0
    if RUFF_BASELINE_FILE.is_file():
        try:
            baseline = int(json.loads(RUFF_BASELINE_FILE.read_text(encoding="utf-8")).get("errors", 0))
        except Exception:
            baseline = 0
    if errors > baseline:
        result.status = "fail"
        result.detail = f"ruff 错误 {errors} 个,超过基线 {baseline}"
        result.data = {"errors": errors, "baseline": baseline}
        return result
    result.detail = f"ruff 错误 {errors} 个(基线 {baseline},只降不升)"
    result.data = {"errors": errors, "baseline": baseline}
    return result


def check_dependency_audit() -> CheckResult:
    """依赖漏洞审计(pip-audit)。"""
    result = CheckResult("依赖漏洞审计")
    code, out, err = _run([sys.executable, "-m", "pip_audit", "--format", "json"])
    if code == 127:
        result.status = "skip"
        result.detail = "未安装 pip-audit(可执行 pip install pip-audit)"
        return result
    if code != 0:
        # 有漏洞时 pip-audit 返回非 0
        try:
            report = json.loads(out or "[]")
            vulnerabilities = [v for v in report if v.get("vulns")]
        except Exception:
            vulnerabilities = []
        if vulnerabilities:
            result.status = "fail"
            result.detail = f"发现 {len(vulnerabilities)} 个有漏洞的依赖"
            result.data = {"vulnerable": vulnerabilities[:10]}
            return result
        result.status = "fail"
        result.detail = "pip-audit 执行失败:" + (err or out).strip()[:200]
        return result
    result.detail = "未发现已知漏洞"
    return result


def check_secret_leak() -> CheckResult:
    """密钥泄漏扫描。"""
    result = CheckResult("密钥泄漏扫描")
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in (".pyc", ".png", ".jpg", ".mp4", ".sqlite", ".db", ".exe"):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                hits.append(f"{path.relative_to(ROOT)}:{pattern.pattern[:24]}")
                break
    if hits:
        result.status = "fail"
        result.detail = f"发现 {len(hits)} 处疑似密钥"
        result.data = {"files": hits[:20]}
        return result
    result.detail = "未发现疑似密钥"
    return result


#: 全部检查
CHECKS = (
    check_version_consistency,
    check_git_clean,
    check_tests,
    check_ruff_baseline,
    check_dependency_audit,
    check_secret_leak,
)


def run_all() -> list[CheckResult]:
    """跑全部检查。

    :return: 检查结果列表。
    """
    return [check() for check in CHECKS]


def main() -> int:
    """命令行入口。

    :return: 退出码(有失败返回 1)。
    """
    import argparse

    parser = argparse.ArgumentParser(description="发版门禁")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    results = run_all()
    payload = [
        {"name": r.name, "status": r.status, "detail": r.detail, "data": r.data}
        for r in results
    ]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in payload:
            mark = {"pass": "[OK]", "fail": "[FAIL]", "skip": "[SKIP]"}.get(
                item["status"], "[?]"
            )
            print(f"{mark:8} {item['name']}: {item['detail']}")
        failed = [i for i in payload if i["status"] == "fail"]
        skipped = [i for i in payload if i["status"] == "skip"]
        print()
        print(
            f"总计 {len(payload)} 项:通过 {len(payload) - len(failed) - len(skipped)} · "
            f"失败 {len(failed)} · 跳过 {len(skipped)}"
        )
        if skipped:
            print("注意:「跳过」不等于「通过」——这些检查本次没能得出结论。")
    return 1 if any(r.failed for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
