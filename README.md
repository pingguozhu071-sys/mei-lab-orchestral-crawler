# mei-lab-orchestral-crawler

> **分层调度 + 多格式提取 + 反检测**的 Python 爬虫框架。
> 核心原则:**最快路径优先**——能用一次 HTTP 请求解决的,绝不开浏览器。
>
> 项目归属:Lob 体系的采集引擎组成部分。

[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-178%20passed-brightgreen.svg)](tests/)

---

## 目录

- [这是什么 / 不是什么](#这是什么--不是什么)
- [核心能力](#核心能力)
- [架构](#架构)
- [快速开始](#快速开始)
- [命令完全手册](#命令完全手册)
- [目录结构](#目录结构)
- [工程规模](#工程规模)
- [关键设计决策](#关键设计决策)
- [实测发现与环境限制](#实测发现与环境限制)
- [环境依赖](#环境依赖)
- [开发与发版](#开发与发版)
- [伦理与合规](#伦理与合规)
- [路线图](#路线图)
- [致谢与姊妹工程](#致谢与姊妹工程)
- [许可](#许可)

---

## 这是什么 / 不是什么

**是什么**

一个**可嵌入的采集引擎**:提供队列、路由、Worker 池、多格式提取、反检测四层能力,
既能用命令行当工具用,也能当库嵌进别的系统。

**不是什么**

- 不是 GUI 产品(没有窗口、没有安装包;日常交互用 `mel shell` 这个终端窗口);
- 不是服务(不常驻、不对外提供接口、不是多用户系统);
- 不是分布式采集集群(单机单进程多 Worker 进程模型);
- 不做账号权限内内容的采集(登录墙/付费墙一律不绕)。

**它明确会做的事**:公开页面的抓取与结构化、媒体(视频/音频)的下载、
反爬对抗(指纹伪装 / TLS 指纹模拟 / 行为模拟 / 验证码识别)。

---

## 核心能力

### 1. 分层调度:先便宜,后昂贵

```
                     ┌──────────────────────────────┐
  种子 URL ─────────▶│  Queue (SQLite WAL, 幂等入队) │
                     │  pending → running → done/…  │
                     │  租约 + CAS 精确一次处理      │
                     └───────────────┬──────────────┘
                                     │ claim
                                     ▼
                     ┌──────────────────────────────┐
                     │       LayeredRouter           │
                     │  静态嗅探(不联网)→ 分层决策   │
                     └──┬───────────────────────┬───┘
          内容层         │                       │  成本层
              ┌─────────┴─────────┐   ┌─────────┴──────────┐
              ▼                   ▼   ▼                    ▼
      ┌───────────────┐   ┌───────────────┐    ┌────────────────────┐
      │  HttpWorker   │   │ BrowserWorker │    │   ExtractionPipeline│
      │ curl_cffi     │   │ 独立 Chromium │    │ HTML/JSON/PDF/媒体 │
      │ TLS 指纹模拟   │   │ 设备档案+注入 │    │ 密度启发 + JSON-LD  │
      │ ≥1.5s 限速     │   │ 网络守卫       │    │ SimHash + 质量闸   │
      └───────┬───────┘   └───────┬───────┘    └─────────┬──────────┘
              │                   │                      │
              └───────────┬───────┴──────────────────────┘
                          ▼
                ┌───────────────────────┐   ┌────────────────────┐
                │  Vault (SQLite)        │──▶│ Export              │
                │  SHA256 增量 + AES-GCM │   │ JSONL/CSV/MD/XLSX/  │
                │  SimHash 近重复        │   │ SQLite(FTS5)        │
                └───────────────────────┘   └────────────────────┘
```

**关键设计:分层升级(lazy escalation)**

静态启发无法预知一个页面是否需要 JS 渲染。所以框架的做法是:
先用最便宜的 HTTP 通道试一次 → 如果抓到的正文**近乎为空**(判定为"空壳页"),
任务自动回队并升级到浏览器通道**再试一次**。

实测(`https://quotes.toscrape.com/js/`):

| 通道 | 提取到的正文 |
|---|---|
| HTTP(轻量) | **29 字符**(只有导航栏) |
| Browser(升级后) | **1429 字符**(JS 渲染后的完整列表) |

升级**不消耗 `--max` 预算**——那是同一个任务的换通道重试,不是一个新任务。

### 2. 多格式提取

| 格式 | 提取方式 | 实测 |
|---|---|---|
| HTML | 文本密度启发式 + 去噪 + 连续区块扩展,trafilatura 兜底 | 2533 字符 / 页 |
| JSON API | 直接解析 + 记录列表归一化 | ✓ |
| JSON-LD | `<script type="application/ld+json">` | ✓(B 站 VideoObject) |
| hydration state | `__NEXT_DATA__`(Next.js)/ `window.__NUXT__`(Nuxt) | ✓ |
| PDF | pymupdf(可选依赖) | ✓ 真实 PDF 提取 |
| 媒体 | 平台解析器 → 通用 yt-dlp → m3u8/AES-128 | ✓ 720P 直链 453,914,132 字节 |

**三层 residual**:每次提取同时给出 `raw_html`(原始)/ `cleaned_dom`(去噪后结构)/
`markdown`(纯正文),证据链完整,不丢信息。

### 3. 反检测

| 手段 | 实现 |
|---|---|
| 设备档案 | **21 台自洽虚拟设备**(Win/macOS/Linux/Android/iOS),locale↔时区↔平台**按构造保证一致** |
| 指纹注入 | webdriver / cdc_ 残迹 / chrome.runtime / platform / languages / GPU / plugins / 触摸 |
| 行为模拟 | 泊松打字节奏、对数正态停留时长、贝塞尔鼠标轨迹(录制样本 7 : 生成 3) |
| 网络守卫 | 浏览器通道拦截并掐断一切非目标域请求 |
| TLS 指纹 | curl_cffi `impersonate="chrome"`(JA3 指纹模拟) |
| 验证码 | ddddocr OCR + cv2 滑块缺口检测 |

**指纹自证探针**(仓库根目录 `fingerprint_probe.py`)用与生产完全相同的链路,
逐项检查会被 bot 检测站点标红的特征:

```
实际使用档案: android_tablet_s9 | platform: Linux armv8l | locale: pt-BR | tz: America/Sao_Paulo
  OK   window.chrome = object          OK   window.chrome.runtime = True
  OK   cdc_ 痕迹数量 = 0               OK   navigator.platform = Linux armv8l
  OK   navigator.languages = ["pt","pt-BR"]
  OK   navigator.hardwareConcurrency = 8
  OK   navigator.maxTouchPoints = 5    OK   plugins 数量 = 5
  OK   WebGL 掩码 = WebKit | WebKit WebGL
  OK   WebGL 未掩码 = Qualcomm | Adreno (TM) 740
标红项: 0 / 10
```

### 4. 工程化(这才是爬虫框架的护城河)

- **队列**:SQLite WAL + 幂等入队 + 显式状态机 + **租约 + CAS 精确一次处理**
  (Worker 崩溃后任务自动回收,不会被覆盖)
- **限流**:全局 ≥1.5s + 抖动 + robots `Crawl-delay` + 每域并发上限 + `Retry-After` + 域名冷却
- **SSRF 闸**:协议白名单 + DNS 解析后 IP 段判定 + 逐跳重定向复检(上限 10 跳)
  + DNS 判定 90 秒缓存 + 数字字面量主机名还原 + **26 条对抗样本回归**
- **三层脱敏**:日志 / 旁路 / 产物,19 类敏感 URL 参数,手机号/邮箱/IP
- **发版门禁**:6 项检查,任一失败即阻止发布;静态基线只降不升

---

## 快速开始

### 环境要求

Windows 10/11 x64 · Linux · macOS;Python **3.12+**。

### 安装

```bash
# 1) 虚拟环境
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"      # Windows
# pip install -e ".[dev]"                                   # Linux / macOS

# 2) 浏览器(Patchright 的隐身版 Chromium;下载后无头运行)
./.venv/Scripts/patchright.exe install chromium            # Windows
# patchright install chromium                               # Linux / macOS

# 3) 可选:PDF 支持
./.venv/Scripts/python.exe -m pip install -e ".[pdf]"

# 4) 可选:媒体通道需要 JS 运行时(yt-dlp 用)
winget install --id DenoLand.Deno --exact      # 或安装 Node.js ≥22
```

### 最小用例

```bash
# 抓一个页面
mel crawl --url https://quotes.toscrape.com/ --max 20

# 日常使用建议开交互窗口:粘贴链接按 Enter 直接爬
mel shell

# 看统计
mel stats

# 导出成果
mel export --format markdown --output ./data
```

### 当库用

```python
from meilab.engine import Engine, EngineConfig

engine = Engine(EngineConfig(output_dir="data", max_per_domain=10))
try:
    summary = engine.crawl(["https://quotes.toscrape.com/"], max_tasks=20)
    print(summary.as_dict())
finally:
    engine.close()
```

---

## 命令完全手册

以下每个命令都附**真实运行回执**(不是伪代码示例)。

### `mel crawl` —— 抓取

```bash
mel crawl --url URL [--url URL2 ...] [--max N] [--output DIR]
          [--http-workers N] [--browser-workers N] [--delay S] [--timeout S]
          [--headless/--headed] [--ignore-robots/--respect-robots] [--json]
```

```bash
$ mel crawl --url https://quotes.toscrape.com/ --max 5
queued 21 | dispatched 5 | succeeded 5 | failed 0 | elapsed 22.5s
通道分布: {'http': 5}
已抓取的页面:
  - https://quotes.toscrape.com/
  - https://quotes.toscrape.com/author/Albert-Einstein
  - https://quotes.toscrape.com/tag/change/page/1/
```

要点:

- **默认遵守 robots.txt**(`--ignore-robots` 可关,但请不要滥用);
- 显式给的 URL **优先**被领取,不会被队列里的遗留任务插队;
- `--max N` 精确控制派发量(在途任务也占预算);
- 同域链接会自动发现并入队,受深度与数量限制;登录/注册类路径**不采集**。

### `mel shell` —— 交互窗口(日常主力)

```
$ mel shell
mei-lab-orchestral-crawler 交互窗口
粘贴一条链接按 Enter 即可抓取;输入 :help 看命令,:quit 退出。

url> 抓取中… https://quotes.toscrape.com/
✓ http 通道 | Quotes to Scrape | 2533 字符 | 3.2s
url> 抓取中… https://quotes.toscrape.com/js/
✓ browser 通道 | Quotes to Scrape | 1429 字符 | 4.1s
url> :quit
会话结束。
```

- 整段复制来的文本会自动挑出其中的 URL(去引号、去句末标点);
- Worker 池常驻,连续粘贴不必反复起进程,单条约 2–4 秒;
- 输入的 URL 先过安全校验(仅 http/https,拒绝 localhost/环回/私网/保留地址),
  不通过会直接给出原因并跳过。

内置命令:`:help` `:stats` `:latest` `:export [jsonl|csv|markdown|xlsx|sqlite]` `:quit`。

### `mel resume` —— 断点续传

```bash
$ mel resume --max 10
续传完成:成功 10 / 失败 0 / 共 0 个任务,20.16 秒
```

从 SQLite 队列继续取 pending 任务,不需要重新给种子 URL。

### `mel stats` —— 统计

```
$ mel stats
任务状态 {"pending": 0, "running": 0, "done": 7, "failed": 0, "dead_letter": 0}
Episode  {"success": 7, "failed": 0, "blocked": 0, "degraded": 1}
仓库     {"documents": 7, "total_seen": 7, "encrypted": false}
通道     {"browser": 2, "http": 3}
通道占比 轻量 60.00% / 浏览器 40.00%
域名分布 quotes.toscrape.com:7
```

### `mel export` —— 导出

```bash
mel export --format markdown|jsonl|csv|xlsx|sqlite --output DIR
```

按域名分目录。`sqlite` 格式带 **FTS5 全文索引**,可直接检索:

```
$ mel export --format markdown --output ./data
导出完成: 3 条记录,4 个文件
  - data/markdown/www.bilibili.com/asmr_ku100_.._malice____bilibili-7eb13a21.md
  - data/markdown/www.bilibili.com/index.md
```

### `mel media` —— 媒体抓取(视频/音频)

```bash
mel media --url URL [--output DIR] [--cookies FILE] [--audio]
          [--prefer auto|native|ytdlp] [--timeout S] [--json]
```

分发优先级:**平台解析器 → 通用 yt-dlp 通道 → m3u8/AES-128**。

```bash
$ mel resolve --url https://www.bilibili.com/video/BV1Dvpg6BEt5/
【ASMR/KU100】超级温柔的深度采耳⟡ 让人浑身酥麻的舒适感..【鬼花/malice】
(bilibili / bilibili_api)
时长 6972s · B 站原生 API
  - 高清 720P | mp4 | 453914132 字节
```

特性:

- **通用 yt-dlp 通道**覆盖 yt-dlp 支持的所有站点(不只是五个平台);
- `.part` + 原子改名 + **字节校验** + `Range` 续传;
- m3u8 支持 `EXT-X-KEY`(AES-128)与 `EXT-X-MAP`,**分片级续传**(`.state.json` 记进度);
- 产物认定用**目录快照差集**——目录里本来就有的旧文件不会被冒充成本次成果;
- 缺组件时**明确报错或入死信**,绝不静默降级(依工程书 §10.4 降级矩阵)。

### `mel comments` —— B 站评论

```bash
$ mel comments --url https://www.bilibili.com/video/BV1GJ411x7h7/
评论接口返回 访问权限不足(平台已限制匿名访问;需要用户自备 cookie 才能读取评论)
```

平台已限制匿名读取(新接口需 WBI 签名 + 登录态)。用 `--cookies` 提供自备 cookie。
**如实报出平台原因,不假装成功。**

### `mel rule` —— 声明式站点规则

```bash
mel rule validate rules/sites/bilibili-video.yaml
mel rule test rules/sites/bilibili-video.yaml --url https://www.bilibili.com/video/BV1Dvpg6BEt5/
```

```
$ mel rule validate rules/sites/bilibili-video.yaml
✓ 规则合法:bilibili-video
  匹配域名: bilibili.com, b23.tv
  字段: title, author, published, description
  媒体: cover, thumbnail

$ mel rule test rules/sites/bilibili-video.yaml --url https://www.bilibili.com/video/BV1Dvpg6BEt5/
  title: 【ASMR/KU100】超级温柔的深度采耳⟡ 让人浑身酥麻的舒适感..【鬼花/malice】
  author: 鬼花まりすofficial
  description: 大家好,初次见面,我是鬼花まりす今后也请多多关照♡
  thumbnail: https://i2.hdslb.com/bfs/archive/7a4360729d3dfb76bc8c081f262c23a7efba03d4.jpg@1200w_630h
```

规则格式(与 kiana-crawler 同构):

```yaml
name: bilibili-video
match: [bilibili.com, b23.tv]     # 域名出现在 URL 里即命中
item_type: video
detail:
  fields:
    title:    {sel: "h1.video-title", text: true}
    published: {sel: ".pubdate", text: true, transform: date_parse}
    cover:    {sel: "meta[property='og:image']", attr: content}
  media:
    thumbnail: {sel: ".video-cover img", attr: src}
```

可选 `transform`:`date_parse` / `strip` / `int`。**加站点不改代码**。

---

## 目录结构

```
meilab_orchestral_crawler/
├── pyproject.toml
├── README.md                     本文件
├── PROJECT_STATE.md              工程台账(环境回执 / 执行证据 / 自检)
├── fingerprint_probe.py          指纹自证探针
├── rules/sites/*.yaml            声明式站点规则
├── tools/
│   ├── release_check.py          发版门禁(6 项)
│   └── doc_numbers.py            文档数字同源
├── meilab/
│   ├── core/       types.py(Pydantic 模型) queue.py(SQLite 队列+状态机+租约)
│   ├── router/     sniff.py(静态嗅探) layer.py(分层路由) feed.py(RSS/sitemap)
│   ├── workers/    base.py(进程骨架+SSRF) http.py(curl_cffi)
│   │               browser.py(独立 Chromium) pool.py(进程池/崩溃重启)
│   ├── extract/    html.py(密度启发) jsondata.py(JSON/JSON-LD/hydration)
│   │               pdf.py(可选) pipeline.py(编排) site_rules.py(YAML 规则)
│   ├── stealth/    profiles.py(21 台设备) inject.py(指纹注入)
│   │               behavior.py(行为模拟) netguard.py(网络守卫) captcha.py(验证码)
│   ├── media/      ytdlp_channel.py(通用通道) m3u8_downloader.py(HLS/AES)
│   │               resolvers.py(五平台) wbi.py(B站签名) downloader.py(编排)
│   │               cookies.py(加密 cookie)
│   ├── privacy/    sanitizer.py(三层脱敏)
│   ├── storage/    vault.py(仓库+增量+AES-GCM) export.py(五种导出)
│   │               quality.py(SimHash+质量闸) observability.py(stats/checkpoint/死信)
│   ├── engine.py   主循环编排
│   └── cli.py      命令行(click)
└── tests/
    ├── test_queue.py            队列:幂等/并发 claim/状态机
    ├── test_queue_lease.py      租约 / CAS / 回收
    ├── test_sniff.py            嗅探:URL 模式 → 通道
    ├── test_router.py           路由:任务类型 / URL → Worker
    ├── test_extract_html.py     提取:密度 / 去噪 / 三层 residual
    ├── test_profiles.py         设备档案:遍历全库一致性
    ├── test_media.py            媒体:m3u8 / AES-128 / 平台 / cookie
    ├── test_ssrf.py             SSRF:26 条对抗样本
    ├── test_captcha.py          验证码:能力探测 / OCR / 滑块
    └── test_privacy_quality.py  脱敏边界 / SimHash / 质量闸
```

> 10 个测试文件 / 123 个测试函数(数字由 `tools/doc_numbers.py` 生成)。

---

## 工程规模

<!-- AUTO:NUMBERS -->

| 指标 | 数值 |
|---|---|
| 包内模块数 | 32 |
| 包内代码行 | 10119 |
| 测试文件数 | 10 |
| 测试代码行 | 1576 |
| 测试函数数 | 123 |
| 站点规则数 | 3 |

<!-- /AUTO:NUMBERS -->

校验命令(数字过期则返回非 0,可挂 CI):

```bash
python tools/doc_numbers.py --check
```

---

## 关键设计决策

### 队列:租约 + CAS,而不是"标记 running 就完事"

朴素做法里,Worker 崩溃后那个任务会**永久卡在 `running`**——直到下次手动干预。
本框架给任务加租约:

```
claim → 写 leased_at / lease_expires(默认 120s)
心跳  → heartbeat() 续租
完成  → mark_done_checked() 做 CAS:只有当前租约持有者能标记完成
回收  → reclaim_expired() 每轮把过期租约收回 pending
```

效果:Worker 中途崩溃,任务在租约到期后自动重跑,且**后完成的结果不会覆盖先完成的**。

### 脱敏的边界:队列里的 URL 不脱敏

产物里的 URL 签名参数(`token`/`sign`/`w_rid` …共 19 类)必须抹掉,
但**任务队列里的 URL 不能抹**——它是后续请求的凭据,抹掉签名参数会让续爬直接 403。
这条边界写死在代码里,不是靠人记。

### SSRF:判据查结构,不查文本

验证码检测不能只做字符串匹配——很多站点会**预加载**风控 SDK
(如 B 站每页都有 `risk-captcha-sdk/CaptchaLoader.js`),那是"预防性加载"而非真弹了验证码。
所以检测前先剔除 `<script>` 段,只看脚本之外的组件容器与可见文案。

同样的教训还有:"判据必须查结构,不许查文本""'没有告警'不等于'通过'""计数口径必须与被移动的状态同源"。

### 媒体:平台解析器不是唯一路径

抖音/小红书/快手/网易云的直链都需要请求签名,自研签名容易被平台一次变更打断。
所以这四家**统一委托通用 yt-dlp 通道**;B 站有稳定的公开 API,走原生 API(实测更准、也更快)。
任何平台解析失败都回退通用通道,而不是直接失败。

---

## 实测发现与环境限制

以下都是**本机实测结论**,不是推测。每条都对应 `PROJECT_STATE.md` 里的可复核回执。

| # | 发现 | 影响与处置 |
|---|---|---|
| 1 | **Patchright 1.61.2 不执行任何脚本注入**(`add_init_script` 与 `route.fulfill` 改写 HTML 均无效,官方 Playwright 正常) | 改为**运行时能力探测**(`_probe_init_script()` 用 `about:blank` 实测),不支持时降级为"加载后加固"。功能不缺,只是注入时机变了 |
| 2 | **Windows 中文路径下 libcurl 无法加载 CA 证书**(`curl: (77)`) | 启动时把 certifi 的 CA bundle 复制到纯 ASCII 路径并设 `CURL_CA_BUNDLE` |
| 3 | **Windows + spawn 不能把同步原语放在 Process 子类实例属性上**(报错 `DuplicateHandle [WinError 5]`,但真因是 pickle 失败) | `__getstate__` 剔除运行时对象 + 队列改 `Manager().Queue()` 代理 |
| 4 | **WebGL 掩码值不要误判**:`gl.VENDOR` 在真实 Chrome 里就显示 `WebKit` | 真正要对齐的是 `WEBGL_debug_renderer_info` 的 `UNMASKED_*` |
| 5 | **无头 Chromium 的 `navigator.plugins` 为空**(真实 Chrome 有 5 个) | 注入脚本补齐 5 个 PDF 插件与 mimeTypes |
| 6 | **B 站评论匿名不可读**(新接口需 WBI 签名 + 登录态,即便带浏览器 cookie 仍 -403) | 支持 `--cookies`,匿名时如实报出平台原因 |
| 7 | `news.ycombinator.com`、`bot.sannysoft.com` 在本网络 TCP 层不可达 | 冒烟改用可达站点;指纹检测由 `fingerprint_probe.py` 承担 |
| 8 | 工程书钉死的 `opencv-python-headless 4.12.0.88` 要求 `numpy<2.3.0`,与钉死的 `numpy==2.5.2` 冲突 | 改用 `5.0.0.93`(与 numpy 2.5 兼容),其余 12 个组件版本与工程书一致 |
| 9 | Google Chrome 已安装,Patchright 拒绝覆盖 | 不执行卸载(破坏用户环境),浏览器通道默认用 Chromium |

---

## 环境依赖

以下版本为本机 `pip show` 实测结果:

| 组件 | 版本 | 用途 |
|---|---|---|
| curl_cffi | 0.16.0 | 轻量通道 TLS/JA3 指纹模拟 |
| patchright | 1.61.2 | 浏览器通道主引擎(隐身版) |
| playwright | 1.62.0 | patchright 缺失时的回退引擎 |
| trafilatura | 2.2.0 | 正文提取兜底(密度算法失效时) |
| lxml | 6.1.1 | BS4 解析后端 |
| orjson | 3.11.9 | 高速 JSON 序列化 |
| openpyxl | 3.1.5 | xlsx 导出 |
| ddddocr | 1.6.1 | 本地 OCR 验证码识别 |
| opencv-python-headless | 5.0.0.93 | 滑块缺口检测 |
| numpy | 2.5.2 | OpenCV 数值底座 |
| pycryptodome | 3.23.0 | m3u8 AES-128 分片解密 |
| yt-dlp | 2026.7.4 | 通用媒体通道外部引擎 |
| pymupdf | 2.0 | PDF 提取(可选 extras) |
| pydantic | 2.14.2 | 数据模型 |
| PyYAML | 6.0.3 | 站点规则 |
| Deno | 2.9.8 | yt-dlp 的 EJS 挑战 JS 运行时(系统级) |

---

## 开发与发版

```bash
# 跑测试
python -m pytest tests/ -v

# 发版门禁(6 项:版本一致 / 工作区干净 / 全测 / ruff 基线 / 依赖漏洞 / 密钥泄漏)
python tools/release_check.py

# 文档数字同源
python tools/doc_numbers.py --check    # 校验
python tools/doc_numbers.py --write    # 写回
```

门禁的设计原则:

- 任一失败即阻止发布;
- **无法得出结论的检查记 SKIP,不会自动视为 PASS**;
- ruff / mypy 静态基线**只允许下降,不允许回升**。

---

## 伦理与合规

- **默认遵守 robots.txt**:抓取前拉取并解析目标站点的 `robots.txt`,
  被 `Disallow` 的 URL 直接跳过;同时尊重 `Crawl-delay`。
- **全局限速 ≥1.5 秒 + 随机抖动**,外加每域并发上限(默认 20)与 `Retry-After` 退避。
- **只采集公开页面**:不触碰登录态、不绕过验证码、不破付费墙。
  链接发现会跳过 `/login`、`/signup`、`/register`、`/account` 等路径。
- **SSRF 防护**:所有出网请求(含每一跳重定向)都要过协议白名单 +
  DNS 解析后 IP 段判定,环回/私网/链路本地/云元数据地址一律拒绝。
- **三层脱敏**:手机号/邮箱/IP 与 19 类敏感 URL 参数在产物落盘前被替换。
- 请把 `User-Agent` 改成你自己的联系方式,让站点管理员知道谁在访问。

---

## 路线图

- [x] 分层调度 + 分层升级(空壳页自动换通道重试)
- [x] 多格式提取(HTML / JSON / JSON-LD / hydration / PDF)
- [x] 反检测(21 台设备档案 / 指纹注入 / 行为模拟 / 网络守卫 / 验证码)
- [x] 媒体通道(通用 yt-dlp + m3u8/AES-128 + 五平台解析 + 加密 cookie)
- [x] 工程化(租约 CAS / 三层脱敏 / SSRF 加固 / 发版门禁)
- [ ] 声明式规则扩面(目前 3 条,目标覆盖主流新闻/论坛/电商)
- [ ] 代理出口管理与粘性会话
- [ ] 覆盖率阈值(目前只生成报告,不设强制阈值——事故回归比行覆盖率更能代表质量目标)

---

## 致谢与姊妹工程

本工程的能力设计大量参考了同作者的姊妹工程
[`kiana-crawler`](https://github.com/pingguozhu071-sys/kiana-crawler)(GPL-3.0):
它的《旧版本问题与整改记录》沉淀的五条纪律,以及租约/CAS、逐跳 SSRF、
三层脱敏、五级回退等工程做法,都是本框架的直接来源。

两者是**独立实现**:本工程不复制 kiana 的代码,架构也不同(kiana 是 asyncio 单进程 +
Windows 桌面 GUI,本工程是 multiprocessing 多进程 + CLI/可嵌入)。
kiana 独有的能力(桌面 GUI、安装包、LLM 增强、代理出口管理、抖音/小红书/快手/网易云
直链解析)本工程**没有**,也不打算照搬——工程书的技术栈封闭集合与之冲突。

---

## 许可

MIT。见 [LICENSE](LICENSE)。
