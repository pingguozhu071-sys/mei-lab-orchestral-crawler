"""声明式站点规则:加站点不改代码。

规则文件放在 ``rules/sites/<域名>.yaml``,结构(与 kiana-crawler 同构):

.. code-block:: yaml

    name: weibo-post
    match: [weibo.com]
    item_type: post
    detail:
      fields:
        author:   {sel: "div[class*='_nick_']", text: true}
        published: {sel: "a[class*='_time_']", text: true, transform: date_parse}
        content:  {sel: "div.wbpro-feed-content", text: true}
      media:
        images:   {sel: "article img", attr: src}

字段值支持三种写法:
* ``{sel: "CSS 选择器", text: true}`` —— 取可见文本;
* ``{sel: "CSS 选择器", attr: src}`` —— 取属性;
* ``{sel: "...", text: true, transform: date_parse}`` —— 取文本后做转换。

支持的选择器语法是 CSS 选择器(不是 XPath)。规则不匹配时静默跳过,不报错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 规则目录(相对项目根)
RULES_DIR = Path("rules") / "sites"

#: 内置转换函数
TRANSFORMS: dict[str, Any] = {}


def _register_transform(name: str):
    """把一个函数注册成规则里的 transform。"""
    def wrapper(func):
        TRANSFORMS[name] = func
        return func
    return wrapper


@_register_transform("date_parse")
def date_parse(value: str) -> str:
    """把常见中文/英文日期解析成 ISO 日期字符串(解析不了就原样返回)。"""
    text = (value or "").strip()
    if not text:
        return text
    patterns = (
        (r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", "%Y-%m-%d"),
        (r"(\d{4})[-/年](\d{1,2})月?(\d{1,2})?", None),
        (r"(\d{4})-(\d{2})-(\d{2})", "%Y-%m-%d"),
    )
    for pattern, fmt in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        groups = [g for g in match.groups() if g]
        if fmt is None:
            year, month = groups[0], groups[1]
            day = groups[2] if len(groups) > 2 else "1"
            fmt = "%Y-%m-%d"
        else:
            year, month, day = groups[0], groups[1], groups[2]
        try:
            import datetime as _dt

            return _dt.datetime.strptime(
                f"{year}-{int(month):02d}-{int(day):02d}", "%Y-%m-%d"
            ).date().isoformat()
        except ValueError:
            continue
    return text


@_register_transform("strip")
def strip_transform(value: str) -> str:
    """去掉首尾空白。"""
    return (value or "").strip()


@_register_transform("int")
def int_transform(value: str) -> str:
    """提取其中的第一个整数(没有就原样返回)。"""
    match = re.search(r"-?\d+", value or "")
    return match.group(0) if match else (value or "")


@dataclass
class FieldSpec:
    """一个字段的抽取规则。"""

    sel: str = ""
    text: bool = False
    attr: str = ""
    transform: str = ""

    @property
    def is_empty(self) -> bool:
        """是否什么都没配(等价于不抽这个字段)。"""
        return not self.sel and not self.attr and not self.text


@dataclass
class SiteRule:
    """一个站点的规则。"""

    name: str = ""
    match: list[str] = field(default_factory=list)
    item_type: str = "post"
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    media: dict[str, FieldSpec] = field(default_factory=dict)
    source_path: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def matches(self, url: str) -> bool:
        """判断 URL 是否命中本规则。

        :param url: 目标 URL。
        :return: 是否命中。
        """
        lowered = (url or "").lower()
        for domain in self.match:
            needle = domain.lower().lstrip(".")
            if needle in lowered:
                return True
        return False

    def extract(self, html: str, base_url: str = "") -> dict[str, Any]:
        """按规则抽取字段。

        :param html: 页面 HTML。
        :param base_url: 用于把相对链接补成绝对链接。
        :return: ``{"fields": {...}, "media": {...}}``;命中的字段才出现。
        """
        from bs4 import BeautifulSoup

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        out: dict[str, Any] = {"fields": {}, "media": {}}
        for name, spec in self.fields.items():
            value = _extract_field(soup, spec, base_url)
            if value is not None:
                out["fields"][name] = value
        for name, spec in self.media.items():
            value = _extract_field(soup, spec, base_url)
            if value is not None:
                out["media"][name] = value
        return out


def _extract_field(soup: Any, spec: FieldSpec, base_url: str = "") -> Any:
    """按字段规则抽一个值。"""
    if spec.is_empty:
        return None
    try:
        element = soup.select_one(spec.sel)
    except Exception:
        return None
    if element is None:
        return None
    if spec.attr:
        value = element.get(spec.attr)
        value = value if isinstance(value, str) else (" ".join(value) if value else "")
    else:
        value = element.get_text(" ", strip=True)
    if spec.transform:
        func = TRANSFORMS.get(spec.transform)
        if func is not None:
            try:
                value = func(value)
            except Exception:
                pass
    # URL 类型的属性补成绝对路径
    if spec.attr in ("src", "content", "href", "data-src", "data-original") \
            and base_url and isinstance(value, str) and value:
        from urllib.parse import urljoin

        value = urljoin(base_url, value)
    return value


def parse_rule(data: dict[str, Any], source_path: str = "") -> SiteRule:
    """把 YAML 字典转成 :class:`SiteRule`。

    :param data: 规则字典。
    :param source_path: 规则文件路径(写进记录便于回溯)。
    :return: 规则对象。
    """
    rule = SiteRule(
        name=str(data.get("name") or ""),
        match=[str(m) for m in (data.get("match") or [])],
        item_type=str(data.get("item_type") or "post"),
        source_path=source_path,
        raw=dict(data),
    )
    detail = data.get("detail") or {}
    rule.fields = _parse_fields(detail.get("fields") or {})
    rule.media = _parse_fields(detail.get("media") or {})
    return rule


def _parse_fields(block: dict[str, Any]) -> dict[str, FieldSpec]:
    """解析字段块。"""
    out: dict[str, FieldSpec] = {}
    for name, value in block.items():
        if isinstance(value, str):
            out[name] = FieldSpec(sel=value, text=True)
            continue
        if not isinstance(value, dict):
            continue
        out[name] = FieldSpec(
            sel=str(value.get("sel") or ""),
            text=bool(value.get("text")),
            attr=str(value.get("attr") or ""),
            transform=str(value.get("transform") or ""),
        )
    return out


def validate_rule(data: dict[str, Any]) -> list[str]:
    """校验一条规则(返回问题清单)。

    :param data: 规则字典。
    :return: 问题描述;为空表示合法。
    """
    issues: list[str] = []
    if not data.get("name"):
        issues.append("缺少 name")
    match = data.get("match")
    if not match or not isinstance(match, list):
        issues.append("缺少 match(应为域名数组)")
    detail = data.get("detail") or {}
    if not isinstance(detail, dict):
        issues.append("detail 必须是字典")
        return issues
    for section in ("fields", "media"):
        block = detail.get(section) or {}
        if not isinstance(block, dict):
            issues.append(f"detail.{section} 必须是字典")
            continue
        for name, spec in block.items():
            if isinstance(spec, str):
                continue
            if not isinstance(spec, dict):
                issues.append(f"{section}.{name} 必须是字典或字符串")
                continue
            if not spec.get("sel"):
                issues.append(f"{section}.{name} 缺少 sel")
            transform = str(spec.get("transform") or "")
            if transform and transform not in TRANSFORMS:
                issues.append(f"{section}.{name} 用了未知 transform:{transform}")
            if spec.get("attr") and spec.get("text"):
                issues.append(f"{section}.{name} 同时写了 attr 与 text")
    return issues


def load_rule_file(path: str | Path) -> tuple[SiteRule | None, list[str]]:
    """从 YAML 文件加载一条规则。

    :param path: 规则文件路径。
    :return: ``(规则, 问题清单)``;解析失败时规则为 ``None``。
    """
    try:
        import yaml  # 第三方库:YAML 解析
    except Exception:
        return None, ["未安装 PyYAML,请执行 pip install pyyaml"]

    target = Path(path)
    if not target.is_file():
        return None, [f"规则文件不存在:{target}"]
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, [f"YAML 解析失败:{exc!r}"]
    if not isinstance(data, dict):
        return None, ["规则文件必须是字典"]
    issues = validate_rule(data)
    return parse_rule(data, str(target)), issues


def load_rules(rules_dir: str | Path | None = None) -> list[SiteRule]:
    """加载目录下的全部规则。

    :param rules_dir: 规则目录;为空用默认的 ``rules/sites``。
    :return: 合法规则列表(有问题的规则跳过)。
    """
    directory = Path(rules_dir) if rules_dir else RULES_DIR
    if not directory.is_dir():
        return []
    rules: list[SiteRule] = []
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        rule, issues = load_rule_file(path)
        if rule is not None and not issues:
            rules.append(rule)
    return rules


def match_rule(rules: list[SiteRule], url: str) -> SiteRule | None:
    """给 URL 找一条匹配的规则。

    :param rules: 规则列表。
    :param url: 目标 URL。
    :return: 命中的规则;没有返回 ``None``。
    """
    for rule in rules:
        if rule.matches(url):
            return rule
    return None
