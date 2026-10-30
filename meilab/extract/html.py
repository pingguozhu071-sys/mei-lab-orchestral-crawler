"""HTML → markdown 提取:文本密度启发式 + 去噪 + 三层 residual。

**文本密度启发式**:正文区域的特征是"文字多、标签少"。对每个块级候选容器计算
``密度 = 子树文本长度 / (1 + 后代标签数)``,取密度最高的那一块;再在它周围保留
**连续**的高密度兄弟节点,避免把正文切成碎片。

**去噪**:先删掉结构性噪声标签(nav / footer / aside / script / style / form …),
再删掉 class / id 命中噪声词(``comment|share|sidebar|ad|recommend`` 等)的元素。

**三层 residual**:
* ``raw_html`` —— 原始 HTML,保留全部证据;
* ``cleaned_dom`` —— 去噪后的 DOM,保留结构;
* ``markdown`` —— 纯正文,便于阅读与入库。

密度算法失效(正文过短)时,回退到 ``trafilatura`` 作为兜底引擎。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Tag

#: 结构性噪声标签:整块删除
NOISE_TAGS = (
    "script", "style", "noscript", "nav", "footer", "aside", "header",
    "form", "iframe", "svg", "canvas", "template", "link", "meta",
)

#: 噪声 class / id 关键词(不区分大小写)
NOISE_PATTERN = re.compile(
    r"(comment|share|sidebar|advert|\bad\b|ads-|banner|promo|social|related|"
    r"recommend|popup|cookie|newsletter|subscribe|breadcrumb|pagination|"
    r"toolbar|navbar|footer|header|widget|modal|hidden)",
    re.IGNORECASE,
)

#: 参与密度计算的块级候选标签
BLOCK_TAGS = ("article", "main", "section", "div", "td", "body")

#: 判定"这块值得保留"的最低密度
MIN_DENSITY = 0.05

#: 正文的最低字符数,低于此值视为密度算法失效
MIN_CONTENT_LENGTH = 200

#: 兄弟节点保留阈值:密度达到最佳密度的这个比例就保留
SIBLING_KEEP_RATIO = 0.35


@dataclass
class HtmlExtraction:
    """一次 HTML 提取的完整结果(三层 residual)。"""

    raw_html: str = ""
    cleaned_dom: str = ""
    markdown: str = ""
    title: str = ""
    text_length: int = 0
    density: float = 0.0
    strategy: str = "density"
    notes: list[str] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        """是否提取到了像样的正文。"""
        return self.text_length >= MIN_CONTENT_LENGTH


def _soup(html: str) -> BeautifulSoup:
    """解析 HTML,优先用 lxml(更快),失败回退 html.parser。"""
    if not html or not html.strip():
        return BeautifulSoup("", "html.parser")
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def clean_dom(soup: BeautifulSoup) -> BeautifulSoup:
    """删除结构性噪声与命中关键词的噪声元素。

    :param soup: 待清洗的 DOM。
    :return: 清洗后的 DOM(原地修改并返回)。
    """
    for tag in soup.find_all(list(NOISE_TAGS)):
        tag.decompose()

    for element in list(soup.find_all(True)):
        if not isinstance(element, Tag):
            continue
        # 已 decompose 的节点 attrs 为 None,必须跳过,否则取属性会抛 AttributeError
        if getattr(element, "decomposed", False) or element.attrs is None:
            continue
        attrs = element.get("class") or []
        class_text = " ".join(attrs) if isinstance(attrs, list) else str(attrs)
        element_id = str(element.get("id") or "")
        haystack = f"{class_text} {element_id}"
        if haystack.strip() and NOISE_PATTERN.search(haystack):
            element.decompose()
    return soup


def text_density(node: Tag) -> float:
    """计算节点的文本密度。

    密度 = **子树文本长度** / (1 + **后代标签数**)。文字多、标签少 → 密度高,
    这正是正文段的特征;导航栏是"标签多、文字少",密度低。

    注意必须用**子树**文本而不是"直接文本":正文通常写成
    ``<article><p>…</p><p>…</p></article>``,直接文本只有空白,
    按直接文本算会让所有正文容器密度恒为 0。

    :param node: 候选节点。
    :return: 密度值。
    """
    text_len = len(node.get_text(" ", strip=True))
    descendants = len(node.find_all(True))
    return text_len / float(descendants + 1)


def direct_text_length(node: Tag) -> int:
    """节点直接文本的字符数。"""
    return len(
        "".join(str(child) for child in node.children if isinstance(child, str)).strip()
    )


def find_main_block(soup: BeautifulSoup) -> tuple[Tag | None, float]:
    """找出正文主区块。

    :param soup: 已清洗的 DOM。
    :return: ``(主区块节点, 密度)``;找不到时返回 ``(None, 0.0)``。
    """
    best: Tag | None = None
    best_density = 0.0
    best_text = -1
    for tag in soup.find_all(list(BLOCK_TAGS)):
        if not isinstance(tag, Tag):
            continue
        density = text_density(tag)
        text_len = len(tag.get_text(" ", strip=True))
        # 密度优先;密度接近时取文本更多的那个
        if density > best_density or (density == best_density and text_len > best_text):
            best_density = density
            best_text = text_len
            best = tag
    if best is None:
        return None, 0.0
    return best, text_density(best)


def expand_continuous(soup: BeautifulSoup, block: Tag) -> Tag:
    """把主区块扩展成**连续**的高密度区域。

    只取一个节点常常会丢掉正文的前后段落。这里在主区块的父节点下,
    保留与主区块密度相当(≥ 最佳密度 × :data:`SIBLING_KEEP_RATIO`)的兄弟节点,
    并保持它们在文档中的连续性(一旦遇到低密度节点就停止扩展)。

    :param soup: 已清洗的 DOM。
    :param block: 主区块节点。
    :return: 组合后的容器节点。
    """
    best_density = text_density(block)
    parent = block.parent
    if not isinstance(parent, Tag):
        return block

    children = [child for child in parent.children if isinstance(child, Tag)]
    try:
        index = children.index(block)
    except ValueError:
        return block

    keep: list[Tag] = []
    # 向前扩展
    for i in range(index - 1, -1, -1):
        sibling = children[i]
        if text_density(sibling) >= best_density * SIBLING_KEEP_RATIO:
            keep.insert(0, sibling)
        else:
            break
    # 向后扩展
    for i in range(index + 1, len(children)):
        sibling = children[i]
        if text_density(sibling) >= best_density * SIBLING_KEEP_RATIO:
            keep.append(sibling)
        else:
            break

    if not keep:
        return block
    keep.append(block)

    container = soup.new_tag("div")
    container["data-extracted"] = "main"
    for node in keep:
        container.append(node.extract())
    return container


def extract_title(soup: BeautifulSoup) -> str:
    """提取页面标题(优先 ``<title>``,其次 ``og:title``)。"""
    if soup.title and soup.title.string:
        return str(soup.title.string).strip()
    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content"):
        return str(meta["content"]).strip()
    return ""


def html_to_markdown(html_fragment: str) -> str:
    """把 HTML 片段转成 markdown。

    :param html_fragment: HTML 片段。
    :return: markdown 文本(失败时回退为纯文本剥离)。
    """
    if not html_fragment:
        return ""
    try:
        from markdownify import markdownify as _md  # 第三方库:HTML → Markdown

        text = _md(html_fragment, heading_style="ATX", bullets="-")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html_fragment)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _fallback_trafilatura(html: str) -> str:
    """用 trafilatura 做兜底正文提取(密度算法失效时)。"""
    try:
        import trafilatura  # 第三方库:兜底正文提取引擎

        extracted = trafilatura.extract(html, include_comments=False, include_tables=True)
        return extracted or ""
    except Exception:
        return ""


def extract_html(html: str, *, use_fallback: bool = True) -> HtmlExtraction:
    """主入口:HTML → 三层 residual。

    :param html: 原始 HTML。
    :param use_fallback: 密度算法失效时是否启用 trafilatura 兜底。
    :return: 提取结果。
    """
    result = HtmlExtraction(raw_html=html or "")
    if not html or not html.strip():
        result.notes.append("空 HTML")
        return result

    soup = _soup(html)
    result.title = extract_title(soup)

    clean_dom(soup)
    block, density = find_main_block(soup)
    result.density = density

    if block is None:
        result.cleaned_dom = str(soup)
        result.markdown = html_to_markdown(result.cleaned_dom)
        result.text_length = len(result.markdown)
        result.notes.append("未找到块级候选,退回整页")
        return result

    container = expand_continuous(soup, block)
    result.cleaned_dom = str(container)
    result.markdown = html_to_markdown(result.cleaned_dom)
    result.text_length = len(result.markdown.strip())

    if result.text_length < MIN_CONTENT_LENGTH and use_fallback:
        fallback = _fallback_trafilatura(html)
        if len(fallback.strip()) > result.text_length:
            result.markdown = fallback.strip()
            result.text_length = len(result.markdown)
            result.strategy = "trafilatura"
            result.notes.append("密度算法正文过短,已回退 trafilatura")
    return result


def extract_links(soup: BeautifulSoup, base_url: str = "") -> list[str]:
    """抽取页面里的链接(引擎做 URL 发现时用)。

    :param soup: DOM。
    :param base_url: 用于把相对链接补成绝对链接。
    :return: 链接列表(去重,保持文档顺序)。
    """
    from urllib.parse import urljoin

    out: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href) if base_url else href
        if url not in out:
            out.append(url)
    return out


def soup_of(html: str) -> BeautifulSoup:
    """对外暴露的解析入口(引擎层复用)。

    :param html: HTML 文本。
    :return: BeautifulSoup 对象。
    """
    return _soup(html)


def element_text(node: Any) -> str:
    """取节点文本(供其他提取器复用)。

    :param node: BeautifulSoup 节点。
    :return: 文本内容。
    """
    if node is None:
        return ""
    if isinstance(node, Tag):
        return node.get_text(" ", strip=True)
    return str(node)
