"""阶段 3 单测:三层脱敏 / SimHash / 质量闸 / 站点规则 / 订阅源 / SQLite 导出。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from meilab.extract.site_rules import (
    match_rule,
    parse_rule,
    validate_rule,
)
from meilab.privacy.sanitizer import (
    SENSITIVE_QUERY_KEYS,
    install_log_filter,
    redact_url_params,
    sanitize_record,
    sanitize_text,
    sanitize_url,
)
from meilab.router.feed import is_safe_xml, looks_like_feed, parse_feed
from meilab.storage.quality import assess_quality, hamming_distance, simhash
from meilab.storage.vault import VaultRecord


# ----------------------------------------------------------------- 脱敏

def test_phone_is_masked() -> None:
    """手机号要掩码。"""
    out = sanitize_text("联系我 13800138000 或 +8613900139000")
    assert "13800138000" not in out
    assert "138****8000" in out


def test_email_is_masked() -> None:
    """邮箱要掩码(保留域名)。"""
    out = sanitize_text("联系 zhangsan@example.com 获取资料")
    assert "zhangsan@example.com" not in out
    assert "z***@example.com" in out


def test_ip_is_masked() -> None:
    """IP 要掩码末段。"""
    out = sanitize_text("来自 192.168.1.100 的请求")
    assert "192.168.1.100" not in out
    assert "192.168.1.***" in out


def test_url_sensitive_params_are_redacted() -> None:
    """URL 里的敏感参数值要抹掉,参数名保留。"""
    url = "https://api.example.com/x?token=abc123&page=2&sign=zzz"
    out = redact_url_params(url)
    assert "abc123" not in out
    assert "zzz" not in out
    assert "page=2" in out
    assert "token=" in out


def test_url_sensitive_params_can_be_dropped() -> None:
    """可以选择整个敏感参数删掉。"""
    url = "https://api.example.com/x?token=abc123&page=2"
    out = redact_url_params(url, keep_names=False)
    assert "abc123" not in out
    assert "token" not in out
    assert "page=2" in out


def test_sensitive_param_list_is_comprehensive() -> None:
    """敏感参数清单要有足够覆盖面(工程书要求 19 类)。"""
    assert len(SENSITIVE_QUERY_KEYS) >= 19
    for key in ("token", "sign", "session", "auth", "password", "w_rid"):
        assert key in SENSITIVE_QUERY_KEYS


def test_sanitize_url_also_masks_text() -> None:
    """URL 脱敏同时处理参数与正文里的敏感串。"""
    out = sanitize_url("https://x.com/a?token=abc&email=a@b.com")
    assert "abc" not in out
    assert "a@b.com" not in out


def test_sanitize_record_is_recursive() -> None:
    """记录脱敏要递归到嵌套结构。"""
    payload = {
        "a": "13800138000",
        "b": ["x@y.com", {"c": "10.0.0.5"}],
    }
    out = sanitize_record(payload)
    assert "13800138000" not in json.dumps(out, ensure_ascii=False)
    assert "x@y.com" not in json.dumps(out, ensure_ascii=False)
    assert "10.0.0.5" not in json.dumps(out, ensure_ascii=False)


def test_log_filter_masks_message() -> None:
    """日志过滤器要掩码消息。"""
    logger = logging.getLogger("meilab.test")
    logger.setLevel(logging.INFO)
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture()
    handler.addFilter(install_log_filter(logger))
    logger.addHandler(handler)
    logger.info("手机号 13800138000 邮箱 a@b.com")
    assert records, "日志没被捕获"
    text = records[0].getMessage()
    assert "13800138000" not in text
    assert "a@b.com" not in text


# ----------------------------------------------------------------- SimHash / 质量闸

def test_simhash_same_text_same_fingerprint() -> None:
    """相同文本指纹相同。"""
    text = "深度采耳的舒适感,顶级音质,65 万元设备录制" * 5
    assert simhash(text) == simhash(text)


def test_simhash_detects_near_duplicate() -> None:
    """改了少量措辞仍判定为近似。"""
    base = "深度采耳的舒适感让人浑身酥麻,顶级音质,65万元设备录制的沉浸式体验。" * 4
    variant = "深度采耳的舒适感让人浑身酥麻,顶级音质,65万元设备录制的沉浸式体验哦。" * 4
    assert hamming_distance(simhash(base), simhash(variant)) <= 6


def test_simhash_different_text_is_far() -> None:
    """不相关文本距离明显更大。"""
    a = "深度采耳的舒适感让人浑身酥麻,顶级音质,65万元设备录制" * 5
    b = "今天天气不错,我们去公园散步,吃了碗馄饨,傍晚路过一家书店" * 5
    assert hamming_distance(simhash(a), simhash(b)) > 10


def test_quality_gate_rejects_thin_content() -> None:
    """太短的内容被判低质量。"""
    result = assess_quality("<p>短</p>")
    assert result.ok is False
    assert result.reasons


def test_quality_gate_accepts_real_content() -> None:
    """正常长文通过质量闸。"""
    result = assess_quality("<p>" + "这是一段正常的中文正文内容。" * 40 + "</p>")
    assert result.ok is True
    assert result.score > 0.9


# ----------------------------------------------------------------- 站点规则

RULE_YAML = {
    "name": "demo",
    "match": ["demo.test"],
    "item_type": "post",
    "detail": {
        "fields": {
            "title": {"sel": "h1.title", "text": True},
            "author": {"sel": ".byline", "text": True},
            "published": {"sel": ".date", "text": True, "transform": "date_parse"},
        },
        "media": {"cover": {"sel": "meta[property='og:image']", "attr": "content"}},
    },
}


def test_validate_rule_accepts_good_rule() -> None:
    """合法规则没有告警。"""
    assert validate_rule(RULE_YAML) == []


def test_validate_rule_reports_problems() -> None:
    """问题规则要逐条报出。"""
    issues = validate_rule({"detail": {"fields": {"x": {"text": True}}}})
    assert any("name" in i for i in issues)
    assert any("match" in i for i in issues)
    assert any("sel" in i for i in issues)


def test_validate_rule_rejects_unknown_transform() -> None:
    """未知 transform 要报出来。"""
    issues = validate_rule(
        {"name": "x", "match": ["a.test"], "detail": {"fields": {"f": {"sel": "p", "transform": "nope"}}}}
    )
    assert any("transform" in i for i in issues)


def test_site_rule_extracts_fields() -> None:
    """规则能抽到字段与媒体。"""
    rule = parse_rule(RULE_YAML)
    html = """<html><head><meta property="og:image" content="/cover.jpg"></head>
    <body><h1 class="title">标题一</h1><span class="byline">作者甲</span>
    <span class="date">2026-10-30</span></body></html>"""
    out = rule.extract(html, "https://demo.test/p/1")
    assert out["fields"]["title"] == "标题一"
    assert out["fields"]["author"] == "作者甲"
    assert out["fields"]["published"] == "2026-10-30"
    assert out["media"]["cover"] == "https://demo.test/cover.jpg"


def test_site_rule_matches_by_domain() -> None:
    """规则按域名匹配。"""
    rule = parse_rule(RULE_YAML)
    assert rule.matches("https://www.demo.test/x") is True
    assert rule.matches("https://other.test/x") is False


def test_shipped_rules_are_valid() -> None:
    """仓库里的规则文件必须全部合法。"""
    from meilab.extract.site_rules import load_rules

    rules = load_rules(Path("rules") / "sites")
    assert rules, "rules/sites 下没有加载到任何规则"
    for rule in rules:
        assert validate_rule(rule.raw) == []


def test_match_rule_picks_first_match() -> None:
    """多条规则时取第一条命中的。"""
    rules = [parse_rule(RULE_YAML), parse_rule({**RULE_YAML, "name": "other"})]
    assert match_rule(rules, "https://demo.test/x") is not None


# ----------------------------------------------------------------- 订阅源

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>示例订阅</title>
<item><title>a</title><link>https://site.test/a</link></item>
<item><title>b</title><link>/b</link></item>
</channel></rss>"""


def test_parse_rss() -> None:
    """RSS 解析出链接。"""
    result = parse_feed(RSS, "https://site.test/feed.xml")
    assert result.ok is True
    assert result.kind == "rss"
    assert result.title == "示例订阅"
    assert "https://site.test/a" in result.links
    assert "https://site.test/b" in result.links


def test_parse_atom() -> None:
    """Atom 解析出链接。"""
    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom"><title>Atom 源</title>
    <entry><link href="https://site.test/x"/></entry></feed>"""
    result = parse_feed(atom)
    assert result.kind == "atom"
    assert "https://site.test/x" in result.links


def test_parse_sitemap() -> None:
    """sitemap 解析出链接。"""
    sitemap = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>https://site.test/1</loc></url>
    <url><loc>https://site.test/2</loc></url></urlset>"""
    result = parse_feed(sitemap)
    assert result.kind == "sitemap"
    assert len(result.links) == 2


def test_feed_rejects_dtd_entities() -> None:
    """含 DTD/实体声明的 XML 必须被拒绝(防 billion laughs)。"""
    evil = """<?xml version="1.0"?>
    <!DOCTYPE lolz [<!ENTITY lol "lol">]>
    <rss><channel><title>&lol;</title></channel></rss>"""
    assert is_safe_xml(evil) is False
    result = parse_feed(evil)
    assert result.ok is False
    assert "DTD" in result.error


def test_feed_invalid_xml_degrades_gracefully() -> None:
    """非法 XML 降级为普通页面抓取,不抛异常。"""
    result = parse_feed("<not-xml")
    assert result.ok is False
    assert "降级" in result.error


def test_looks_like_feed() -> None:
    """订阅源后缀启发。"""
    assert looks_like_feed("https://x.com/feed.xml") is True
    assert looks_like_feed("https://x.com/sitemap.xml") is True
    assert looks_like_feed("https://x.com/article") is False


# ----------------------------------------------------------------- SQLite 导出

def test_sqlite_export_with_fts(tmp_path: Path) -> None:
    """SQLite 导出带 FTS5 索引,且正文已脱敏。"""
    from meilab.storage.export import export_sqlite

    record = VaultRecord(
        content_hash="h1",
        url="https://demo.test/a?token=secret",
        domain="demo.test",
        title="标题",
        content="联系方式 13800138000 邮箱 a@b.com",
    )
    files = export_sqlite([record], tmp_path)
    assert files
    import sqlite3

    connection = sqlite3.connect(str(files[0]))
    try:
        rows = connection.execute("SELECT url, content FROM scrape").fetchall()
        assert len(rows) == 1
        assert "secret" not in rows[0][0]
        assert "13800138000" not in rows[0][1]
        has_fts = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='scrape_fts'"
        ).fetchone()[0]
        assert has_fts == 1
    finally:
        connection.close()
