"""HTML 提取单测:密度启发式、去噪与三层 residual。"""

from __future__ import annotations

import pytest

from meilab.extract.html import (
    HtmlExtraction,
    clean_dom,
    expand_continuous,
    extract_html,
    extract_links,
    extract_title,
    find_main_block,
    html_to_markdown,
    soup_of,
    text_density,
)

LOREM = (
    "密度启发式依赖正文段落" * 12
    + "。这段文字足够长,让提取器认为它是正文而不是导航噪声。"
    + "更多正文内容用于达到最小长度阈值。" * 12
)


def make_page(body: str = LOREM) -> str:
    """构造一个带导航、侧栏、评论、广告与正文的测试页面。"""
    return f"""<!DOCTYPE html>
<html><head><title>测试页面 | 示例站</title>
<meta property="og:title" content="OG 标题">
<style>.x{{color:red}}</style>
<script>var tracker = 1;</script>
</head>
<body>
<nav class="navbar"><a href="/">首页</a><a href="/about">关于</a></nav>
<aside class="sidebar"><ul><li><a href="/a">链接A</a></li><li><a href="/b">链接B</a></li></ul></aside>
<div class="ad-banner">买一送一 买一送一 买一送一</div>
<article class="post-content">
  <h1>正文标题</h1>
  <p>{body}</p>
  <p>{body}</p>
</article>
<div class="comment-list"><p>评论内容评论内容评论内容</p></div>
<div class="social-share"><a href="https://twitter.com/x">分享</a></div>
<footer>版权所有</footer>
</body></html>"""


def test_extract_returns_three_layers() -> None:
    """必须同时给出 raw_html / cleaned_dom / markdown 三层。"""
    result = extract_html(make_page())
    assert isinstance(result, HtmlExtraction)
    assert result.raw_html.startswith("<!DOCTYPE html>")
    assert "<article" in result.cleaned_dom or "post-content" in result.cleaned_dom
    assert "#" in result.markdown
    assert result.text_length > 100


def test_noise_is_removed() -> None:
    """导航/侧栏/评论/广告/页脚等噪声不进入 cleaned_dom。"""
    result = extract_html(make_page())
    lowered = result.cleaned_dom.lower()
    for token in ("navbar", "sidebar", "ad-banner", "comment-list", "social-share", "<footer"):
        assert token not in lowered, f"噪声未清除: {token}"
    assert "tracker" not in result.cleaned_dom
    assert "color:red" not in result.cleaned_dom


def test_main_content_is_kept() -> None:
    """正文内容必须保留下来。"""
    result = extract_html(make_page())
    assert "密度启发式依赖正文段落" in result.markdown


def test_title_extraction() -> None:
    """标题优先取 <title>。"""
    soup = soup_of(make_page())
    assert extract_title(soup) == "测试页面 | 示例站"


def test_og_title_fallback() -> None:
    """没有 <title> 时退回 og:title。"""
    soup = soup_of('<html><head><meta property="og:title" content="OG 标题"></head><body>x</body></html>')
    assert extract_title(soup) == "OG 标题"


def test_text_density_prefers_text_heavy_nodes() -> None:
    """文本密度:文字多、标签少的节点密度更高。"""
    soup = soup_of("<div id='a'><p>x</p><p>y</p><p>z</p></div>")
    dense = soup_of("<div id='b'> lots of direct text here </div>").find("div")
    sparse = soup.find("div")
    assert text_density(dense) > text_density(sparse)


def test_find_main_block_picks_article() -> None:
    """主区块应定位到 article。"""
    soup = soup_of(make_page())
    clean_dom(soup)
    block, density = find_main_block(soup)
    assert block is not None
    assert density >= 0
    assert "post-content" in str(block) or "正文" in block.get_text()


def test_expand_continuous_keeps_neighbours() -> None:
    """连续扩展能带上相邻的高密度段落。"""
    html = "<body><div class='p1'>" + LOREM + "</div><div class='p2'>" + LOREM + "</div></body>"
    soup = soup_of(html)
    block, _ = find_main_block(soup)
    assert block is not None
    container = expand_continuous(soup, block)
    assert "p1" in str(container) or "p2" in str(container)


def test_empty_html_is_safe() -> None:
    """空输入不炸。"""
    result = extract_html("")
    assert result.markdown == ""
    assert result.text_length == 0
    assert result.notes


def test_html_to_markdown_strips_tags() -> None:
    """markdown 转换去掉标签。"""
    md = html_to_markdown("<h1>标题</h1><p>正文</p><script>x=1</script>")
    assert "标题" in md
    assert "<h1>" not in md


def test_extract_links_filters_and_dedups() -> None:
    """链接抽取过滤锚点/js/mailto 并去重。"""
    soup = soup_of(
        '<a href="/a">a</a><a href="/a">dup</a><a href="#top">top</a>'
        '<a href="javascript:void(0)">js</a><a href="mailto:x@y.z">mail</a>'
        '<a href="https://example.org/x">abs</a>'
    )
    links = extract_links(soup, base_url="https://example.com/")
    assert "https://example.com/a" in links
    assert "https://example.org/x" in links
    assert not any(l.startswith("#") or l.startswith("javascript:") or l.startswith("mailto:") for l in links)
    assert len(links) == len(set(links))


def test_short_page_uses_fallback_or_is_marked() -> None:
    """正文过短时要么走兜底,要么明确标注,不能假装成功。"""
    result = extract_html("<html><body><p>短</p></body></html>")
    assert result.strategy in ("density", "trafilatura")
    if result.strategy == "density":
        assert result.text_length < 200
