# mei-lab-orchestral-crawler

简体中文 ｜ [English](README.md) ｜ [日本語](README.ja-JP.md)

一个实验性质的网页采集引擎。一次运行把一组 URL 走完「嗅探 → 路由 → 抓取 → 提取 →
去重 → 存储」,产物落在本地磁盘。

设计取舍的完整说明见 [技术白皮书](docs/WHITEPAPER.md) 与 [架构文档](docs/ARCHITECTURE.md)。

CLI + 可嵌入库 · Python 3.12+ · v0.1.0 · Windows / Linux / macOS

---

## 概览

| | |
|---|---|
| 引擎 | 32 个模块,10,119 行 |
| 测试 | 10 个文件,123 个测试函数,178 个用例,全部离线 |
| 通道 | HTTP(curl_cffi,浏览器级 TLS 指纹)与浏览器(每个 Worker 一个独立 Chromium) |
| 提取 | HTML(文本密度)、JSON API、JSON-LD、`__NEXT_DATA__`、`window.__NUXT__`、PDF |
| 媒体 | 通用 yt-dlp 通道、m3u8 + AES-128、五个平台解析器 |
| 反检测 | 21 台自洽设备档案、指纹注入、网络守卫、行为模拟 |
| 队列 | SQLite WAL、租约 + 比较交换、幂等入队、可续跑 |
| 安全 | 逐跳 SSRF 闸(26 条对抗样本回归)、三层脱敏 |
| 站点规则 | 声明式 YAML,加站点不改代码 |
| 发版门禁 | 6 项,静态基线只允许下降 |

## 抓取模型

整个引擎围绕一条规则:**用能用的最便宜通道**。

URL 先过一遍离线静态嗅探,推断它大概是什么内容、需要多贵的手段。看起来是静态 HTML 的
交给 HTTP 通道,路径模式或内容特征暗示需要 JS 渲染的交给浏览器通道。嗅探被允许朝便宜的
方向犯错:如果 HTTP 通道拿回来的是空壳页、提取到的正文近乎为空,任务会重新入队,
改走浏览器通道再试一次。

`https://quotes.toscrape.com/js/` 上的实测:

| 通道 | 提取到的正文 |
|---|---|
| HTTP | 29 字符(只有导航) |
| 浏览器(升级后) | 1,429 字符 |

这次升级重试**不占用 `--max` 的页面预算**——它是同一个任务换条通道,不是一个新页面。

## 架构

```
   种子 URL
       │
       ▼
 ┌───────────────┐  领取(租约 + CAS)   ┌──────────────────┐
 │ SQLite 队列    │ ───────────────────► │  LayeredRouter   │
 │ 任务 / 媒体    │ ◄─────────────────── │ (离线嗅探)        │
 └───────────────┘  结果 / 回队          └────┬────────┬────┘
                                              │        │
                                 HTTP Worker  │        │  浏览器 Worker
                                  (curl_cffi) ▼        ▼  (独立 Chromium)
                                       ┌─────────────────────────┐
                                       │   ExtractionPipeline    │
                                       │ HTML / JSON / PDF / 媒体 │
                                       └───────────┬─────────────┘
                                                   ▼
                                      ┌────────────────────────────────┐
                                      │ Vault(SQLite)→ 导出             │
                                      │ SHA-256 增量 · SimHash · AES    │
                                      │ JSONL · CSV · MD · XLSX · SQLite│
                                      └────────────────────────────────┘
```

模块职责、单页处理的完整步骤、数据库表结构见
[架构文档](docs/ARCHITECTURE.md)。

## 快速开始

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# pip install -e ".[dev]"                              # Linux / macOS

patchright install chromium                             # 浏览器通道
pip install -e ".[pdf]"                                 # 可选:PDF 支持
```

浏览器二进制不由 pip 安装。跳过这一步,浏览器通道会被禁用,动态页面进死信队列,
其余通道照常工作。

```bash
mel crawl --url https://quotes.toscrape.com/ --max 20
mel shell                       # 粘链接按 Enter 直接抓
mel stats
mel export --format markdown --output ./data
```

当库用:

```python
from meilab.engine import Engine, EngineConfig

engine = Engine(EngineConfig(output_dir="data", max_per_domain=10))
try:
    print(engine.crawl(["https://quotes.toscrape.com/"], max_tasks=20).as_dict())
finally:
    engine.close()
```

## 命令

| 命令 | 用途 |
|---|---|
| `mel crawl --url URL [--max N]` | 采集页面;默认遵守 robots.txt 与 Crawl-delay |
| `mel shell` | 交互窗口:粘链接,回车即抓 |
| `mel resume [--max N]` | 继续处理队列里未完成的任务 |
| `mel stats` | 任务 / Episode / 仓库统计,含通道占比 |
| `mel export --format <fmt> --output DIR` | 导出:`jsonl` `csv` `markdown` `xlsx` `sqlite`(带 FTS5) |
| `mel media --url URL [--audio] [--cookies F]` | 下载媒体:平台解析器 → 通用 yt-dlp → m3u8 |
| `mel resolve --url URL` | 只解析媒体流,不下载 |
| `mel comments --url URL [--cookies F]` | B 站评论 |
| `mel rule validate <file>` | 校验站点规则 |
| `mel rule test <file> --url URL` | 用真实页面跑一条规则 |

## 实测数据

Windows 11,Python 3.14.6,单机。

```
mel crawl --url https://quotes.toscrape.com/ --max 5
  入队 21 · 派发 5 · 成功 5 · 失败 0 · 22.5 s

mel crawl --url https://quotes.toscrape.com/js/ --max 3
  通道:http 3 · browser 3
  /js/ 页:http 0 字符 → 升级 → 浏览器 1,429 字符

mel resolve --url https://www.bilibili.com/video/BV1Dvpg6BEt5/
  B 站原生 API · 720P · mp4 · 453,914,132 字节 · 时长 6,972 s
  CDN 支持 Range:206 Partial Content

fingerprint_probe.py
  10 项检测 · 0 项标红
```

指纹探针跑的是生产代码里的 Worker、设备库和注入链路,在本地页面上逐项报出 bot 检测
通常会抓的信号:`navigator.webdriver`、`cdc_*` 残迹、`window.chrome.runtime`、
`navigator.platform`、`languages`、`hardwareConcurrency`、`maxTouchPoints`、插件数量、
WebGL 掩码值与未掩码值。

## 已知限制

以下结论都在本机实测过,不是推测。

1. **Patchright 1.61.2 不执行 `add_init_script`,也不认 `route.fulfill` 改写过的 HTML。**
   同一份代码在官方 Playwright 下正常。因此指纹注入发生在页面加载**之后**,而不是第一个
   脚本执行之前。引擎在启动时探测这个能力,并在不支持时显式降级,不靠假设。
2. **B 站评论匿名读不到。** 当前接口需要 WBI 签名加登录态;只带浏览器拿到的 cookie
   仍返回 `-403`。`mel comments` 会把平台给的原因原样报出来,并支持 `--cookies`。
3. **抖音、小红书、快手、网易云交给 yt-dlp 处理。** 这几家的直链签名随时会变,自己维护
   签名器是长期负担。解析器在失败时回退到通用通道,而不是直接判失败。
4. **没有 GUI,也没有安装包。** 这是一套 CLI 和库,`mel shell` 是它的交互前端。
5. **`news.ycombinator.com` 与 `bot.sannysoft.com` 在本网络不可达**(TCP 超时),
   冒烟改用其它站点,外部指纹检测由本地探针替代。
6. **站点规则目前覆盖三个站点**(B 站、知乎、加一个模板)。机制是完整的,规则库不是。
7. **覆盖率只出报告,不设阈值。** 在这里,事故回归比行覆盖率更能说明质量。
8. **七个文件超过 300 行。** 它们已冻结,新能力进新模块,不往里堆。

## 安全边界

- 抓取前拉取并解析 `robots.txt`,`Disallow` 与 `Crawl-delay` 都遵守。
- 全局最小间隔 1.5 秒加抖动,配合每域并发上限、`Retry-After` 退避与域名冷却。
- 只抓公开页面。登录墙、付费墙、验证码都不绕。链接发现会跳过
  `/login`、`/signup`、`/register`、`/account`。
- 每一个出网请求——包括每一次重定向——都要过协议白名单和 DNS 解析后的地址检查。
  环回、私网、链路本地、云元数据地址一律拒绝。
- 手机号、邮箱、IP 与 19 类敏感 URL 参数在写入磁盘前被替换。队列里的 URL 刻意**不**
  脱敏:它带着下一次请求需要的签名。

## 项目结构

```
meilab/
  core/      数据模型、SQLite 队列(状态机、租约、CAS、迁移)
  router/    嗅探、分层路由、RSS/sitemap 播种
  workers/   进程骨架 + SSRF、HTTP、浏览器、Worker 池
  extract/   HTML、JSON、PDF、管线、站点规则
  stealth/   设备档案、注入、行为、网络守卫、验证码
  media/     yt-dlp 通道、m3u8、平台解析器、WBI、cookie、下载编排
  privacy/   脱敏
  storage/   仓库、导出、质量闸、可观测
  engine.py  主循环
  cli.py     命令行
rules/sites/ 声明式站点规则
tools/       release_check、doc_numbers
tests/       10 个文件、123 个测试函数
```

## 开发

```bash
python -m pytest tests/ -v        # 178 个用例,离线
python tools/release_check.py     # 6 项,任一失败即阻止发布
python tools/doc_numbers.py --check
```

发版门禁检查版本一致性、工作区干净、全量测试、静态分析基线(只降不升)、依赖漏洞、
密钥泄漏。得不出结论的检查记为跳过,不记为通过。

## 姊妹工程

[kiana-crawler](https://github.com/pingguozhu071-sys/kiana-crawler) 是同一来源的另一套
采集引擎。它是 Windows 桌面产品:asyncio 内核、PySide6 窗口、622 MB 安装包、
四个平台直链解析、LLM 增强。本工程用另一条路解同一个问题:多进程而非 asyncio、
CLI 而非 GUI、更窄的依赖集。

两者之间没有共享代码。真正传过来的是工程做法——租约 + CAS 的队列语义、逐跳 SSRF 校验、
三层脱敏,以及一份"这些坑别再踩"的清单。本工程自己踩出来的问题记在
[工程经验记录](docs/ENGINEERING-LESSONS.md)。

## 许可

MIT,见 [LICENSE](LICENSE)。
