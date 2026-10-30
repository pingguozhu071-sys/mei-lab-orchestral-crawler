"""JSON 数据提取:JSON API 响应 / JSON-LD / 前端 hydration state。

三类来源:

* **JSON API**:响应体本身就是 JSON,直接解析;
* **JSON-LD**:HTML 里 ``<script type="application/ld+json">`` 的结构化数据;
* **hydration state**:前端框架把首屏数据内嵌进 HTML,常见两种形态——
  Next.js 的 ``<script id="__NEXT_DATA__">`` 与 Nuxt 的 ``window.__NUXT__``。

统一输出 :class:`JsonExtraction`:原始 ``data`` + 归一化后的 ``records``
(尽力找出"记录列表",方便下游按条目入库)。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any

#: 常见的"记录列表"键名(优先匹配)
RECORD_KEYS = (
    "items", "results", "records", "data", "list", "rows", "entries",
    "products", "posts", "articles", "users", "nodes", "edges",
)

#: Nuxt hydration state 的赋值起点(后续内容用 raw_decode 解析,不用正则匹配大括号)
_NUXT_RE = re.compile(r"window\.__NUXT__\s*=\s*")


@dataclass
class JsonExtraction:
    """一次 JSON 数据提取的结果。"""

    kind: str = "none"  # api / jsonld / next_data / nuxt / none
    data: Any = None
    records: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否拿到了数据。"""
        return self.data is not None

    @property
    def count(self) -> int:
        """归一化后的记录条数。"""
        return len(self.records)


def _loads(text: str) -> Any:
    """容错解析 JSON(失败返回 ``None``)。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def parse_json_response(body: str | bytes | None) -> JsonExtraction:
    """解析一个 JSON API 响应体。

    :param body: 响应体(文本或字节)。
    :return: 提取结果。
    """
    result = JsonExtraction(kind="api")
    if body is None:
        result.notes.append("空响应体")
        return result
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            body = ""
    data = _loads(body)
    if data is None:
        result.kind = "none"
        result.notes.append("响应体不是合法 JSON")
        return result
    result.data = data
    result.records = normalize_records(data)
    result.title = guess_title(data)
    return result


def extract_jsonld(html: str) -> list[dict[str, Any]]:
    """抽取 HTML 中的 JSON-LD 结构化数据。

    :param html: HTML 文本。
    :return: JSON-LD 对象列表(解析失败的条目会被跳过)。
    """
    out: list[dict[str, Any]] = []
    if not html:
        return out
    try:
        from bs4 import BeautifulSoup  # 第三方库:HTML 解析

        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return out

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        data = _loads(raw)
        if isinstance(data, dict):
            out.append(data)
        elif isinstance(data, list):
            out.extend(item for item in data if isinstance(item, dict))
    return out


def extract_next_data(html: str) -> dict[str, Any] | None:
    """抽取 Next.js 的 ``__NEXT_DATA__`` hydration state。

    :param html: HTML 文本。
    :return: 解析后的字典;没有或解析失败返回 ``None``。
    """
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        node = soup.find("script", id="__NEXT_DATA__")
    except Exception:
        return None
    if node is None:
        return None
    data = _loads(node.string or node.get_text() or "")
    return data if isinstance(data, dict) else None


def _read_js_string_literal(text: str) -> str | None:
    """读取一个 JS 字符串字面量(正确处理反斜杠转义)。

    :param text: 以引号开头的文本。
    :return: 字面量内容;不是合法字面量时返回 ``None``。
    """
    if not text or text[0] not in ("'", '"'):
        return None
    quote = text[0]
    out: list[str] = []
    index = 1
    escapes = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "\\": "\\",
               "'": "'", '"': '"', "/": "/"}
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            out.append(escapes.get(nxt, nxt))
            index += 2
            continue
        if char == quote:
            return "".join(out)
        out.append(char)
        index += 1
    return None


def extract_nuxt(html: str) -> dict[str, Any] | None:
    """抽取 Nuxt 的 ``window.__NUXT__`` hydration state。

    不用正则去"匹配大括号"(嵌套 JSON 会截断),而是用
    :meth:`json.JSONDecoder.raw_decode` 做括号感知解析。

    :param html: HTML 文本。
    :return: 解析后的字典;没有或解析失败返回 ``None``。
    """
    if not html:
        return None
    match = _NUXT_RE.search(html)
    if match is None:
        return None

    rest = html[match.end() :].lstrip()
    if rest.startswith("JSON.parse"):
        # 形如 window.__NUXT__=JSON.parse("{\"a\":1}") —— 先取出字符串字面量再解析
        literal = _read_js_string_literal(rest[rest.find("(") + 1 :].lstrip())
        if literal is None:
            return None
        data = _loads(literal)
    else:
        try:
            data, _end = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            data = None
    return data if isinstance(data, dict) else None


def extract_from_html(html: str) -> JsonExtraction:
    """从 HTML 里找结构化数据(JSON-LD → __NEXT_DATA__ → __NUXT__,按此顺序)。

    :param html: HTML 文本。
    :return: 提取结果;都没找到时 ``kind="none"``。
    """
    result = JsonExtraction(kind="none")

    jsonld = extract_jsonld(html)
    if jsonld:
        result.kind = "jsonld"
        result.data = jsonld
        result.records = [item for item in jsonld if isinstance(item, dict)]
        result.title = guess_title(jsonld)
        result.notes.append(f"命中 JSON-LD ×{len(jsonld)}")
        return result

    next_data = extract_next_data(html)
    if next_data is not None:
        result.kind = "next_data"
        result.data = next_data
        result.records = normalize_records(next_data)
        result.title = guess_title(next_data)
        result.notes.append("命中 __NEXT_DATA__")
        return result

    nuxt = extract_nuxt(html)
    if nuxt is not None:
        result.kind = "nuxt"
        result.data = nuxt
        result.records = normalize_records(nuxt)
        result.title = guess_title(nuxt)
        result.notes.append("命中 window.__NUXT__")
        return result

    result.notes.append("未发现 JSON-LD / __NEXT_DATA__ / __NUXT__")
    return result


def normalize_records(data: Any, max_depth: int = 6) -> list[dict[str, Any]]:
    """从任意嵌套结构里找出"记录列表"。

    策略:先按常见键名(``items`` / ``results`` / ``data`` …)找,
    找不到再深度优先找第一个"字典元素列表"。

    :param data: 待处理的数据。
    :param max_depth: 最大递归深度。
    :return: 记录字典列表(找不到时为空)。
    """
    if isinstance(data, list):
        dicts = [item for item in data if isinstance(item, dict)]
        return dicts if dicts else []
    if not isinstance(data, dict):
        return []

    for key in RECORD_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            dicts = [item for item in value if isinstance(item, dict)]
            if dicts:
                return dicts
        if isinstance(value, dict):
            nested = normalize_records(value, max_depth - 1)
            if nested:
                return nested

    if max_depth <= 0:
        return []
    for value in data.values():
        if isinstance(value, (dict, list)):
            nested = normalize_records(value, max_depth - 1)
            if nested:
                return nested
    return []


def guess_title(data: Any) -> str:
    """从结构化数据里猜一个标题。

    :param data: 结构化数据。
    :return: 标题字符串;找不到返回空串。
    """
    if isinstance(data, dict):
        for key in ("title", "name", "headline", "headTitle"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("props", "data", "pageProps"):
            nested = data.get(key)
            if isinstance(nested, dict):
                title = guess_title(nested)
                if title:
                    return title
        # 记录列表里的第一条通常带标题
        records = normalize_records(data)
        if records:
            title = guess_title(records[0])
            if title:
                return title
    elif isinstance(data, list):
        for item in data:
            title = guess_title(item)
            if title:
                return title
    return ""


def to_markdown(data: Any, max_items: int = 200) -> str:
    """把结构化数据渲染成便于阅读的 markdown。

    :param data: 结构化数据。
    :param max_items: 最多渲染多少条记录。
    :return: markdown 文本。
    """
    records = normalize_records(data)
    if not records:
        if isinstance(data, (dict, list)):
            try:
                return "```json\n" + json.dumps(data, ensure_ascii=False, indent=2)[:20000] + "\n```"
            except (TypeError, ValueError):
                return str(data)
        return ""

    lines: list[str] = []
    for index, record in enumerate(records[:max_items], start=1):
        title = guess_title(record) or f"记录 {index}"
        lines.append(f"## {title}")
        for key, value in record.items():
            if isinstance(value, (dict, list)):
                try:
                    rendered = json.dumps(value, ensure_ascii=False)[:500]
                except (TypeError, ValueError):
                    rendered = str(value)[:500]
            else:
                rendered = str(value)[:500]
            lines.append(f"- **{key}**: {rendered}")
        lines.append("")
    return "\n".join(lines).strip()
