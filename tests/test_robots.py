"""robots.txt 解析单测(离线,用简书真实 robots 文本)。"""

from __future__ import annotations

from meilab.workers.http import DEFAULT_UA, RobotsChecker

# 采自 https://www.jianshu.com/robots.txt(2026-10-31,HTTP 200,803 字节)
JIANSHU_ROBOTS = """
# See http://www.robotstxt.org/wc/norobots.html for documentation on how to use the robots.txt file
User-agent: *
Disallow: /search
Disallow: /convos/

User-agent: trendkite-akashic-crawler
Request-rate: 1/2 # load 1 page per 2 seconds
Crawl-delay: 60

User-agent: YisouSpider
Request-rate: 1/10 # load 1 page per 10 seconds
Crawl-delay: 60

User-agent: Cliqzbot
Disallow: /

User-agent: Googlebot
Request-rate: 2/1 # load 2 page per 1 seconds
Crawl-delay: 10
Allow: /

User-agent: Mediapartners-Google
Allow: /
"""


def test_crawl_delay_parsed_for_matching_agent() -> None:
    """Googlebot 组声明的 Crawl-delay: 10 必须被解析出来。"""
    checker = RobotsChecker(user_agent="Googlebot")
    checker.load_text(JIANSHU_ROBOTS, "https://www.jianshu.com")
    delay = checker.crawl_delay("https://www.jianshu.com/article/1")
    assert delay == 10.0


def test_crawl_delay_absent_for_unlisted_agent() -> None:
    """未点名的 UA 落到 * 组;该组没有 Crawl-delay,应为 0。"""
    checker = RobotsChecker(user_agent=DEFAULT_UA)
    checker.load_text(JIANSHU_ROBOTS, "https://www.jianshu.com")
    assert checker.crawl_delay("https://www.jianshu.com/article/1") == 0.0


def test_crawl_delay_for_named_bot_group() -> None:
    """另一个具名组的 60 秒同样能取到。"""
    checker = RobotsChecker(user_agent="YisouSpider")
    checker.load_text(JIANSHU_ROBOTS, "https://www.jianshu.com")
    assert checker.crawl_delay("https://www.jianshu.com/x") == 60.0


def test_disallow_still_enforced() -> None:
    """Crawl-delay 之外,Disallow 语义不受影响。"""
    checker = RobotsChecker(user_agent=DEFAULT_UA)
    checker.load_text(JIANSHU_ROBOTS, "https://www.jianshu.com")
    assert checker.allowed("https://www.jianshu.com/search?q=x") is False
    assert checker.allowed("https://www.jianshu.com/article/1") is True


def test_commented_crawl_delay_is_not_a_directive() -> None:
    """注释行('# Crawl-delay: 5')不等于指令(豆瓣样本的反例)。"""
    checker = RobotsChecker(user_agent=DEFAULT_UA)
    checker.load_text("User-agent: *\n# Crawl-delay: 5\nAllow: /\n", "https://example.com")
    assert checker.crawl_delay("https://example.com/x") == 0.0
