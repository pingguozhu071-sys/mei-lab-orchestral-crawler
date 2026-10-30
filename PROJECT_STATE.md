# PROJECT_STATE —— mei-lab-orchestral-crawler 台账(终版)

> 唯一生产者,全程无人值守,断点可续,分批交付。
> 本文件是项目的唯一记忆:环境回执、执行证据、每批自检、风险记录,全部为真实终端输出。
> 上下文丢失时直接读本文件即可继续。

## 1. 项目定位

分层调度 + 多格式提取 + 反检测的 Python 爬虫框架。
核心原则:**最快路径优先**——能用一次 HTTP 请求解决的,绝不开浏览器。

## 2. 技术栈

原始封闭集合:`pydantic v2` / `click` / `rich` / `beautifulsoup4` / `markdownify` / `cryptography`
第 10 节追加(版本钉死):`curl_cffi 0.16.0` / `patchright 1.61.2` / `playwright 1.62.0` /
`trafilatura 2.2.0` / `lxml 6.1.1` / `orjson 3.11.9` / `openpyxl 3.1.5` /
`ddddocr 1.6.1` / `opencv-python-headless 5.0.0.93` / `numpy 2.5.2` /
`pycryptodome 3.23.0` / `yt-dlp 2026.7.4`
可选 extras:`pdf = ["pymupdf>=1.24"]`、`dev = ["pytest>=8.0"]`
禁止:asyncio / aiohttp / httpx、Redis、外部 API key、云服务。

## 3. 实际交付文件清单(25 个)

```
meilab_orchestral_crawler/
├── pyproject.toml              批次 1
├── PROJECT_STATE.md            本文件
├── README.md                   批次 15
├── meilab/
│   ├── __init__.py             批次 1(补正)
│   ├── core/types.py           批次 1
│   ├── core/queue.py           批次 2
│   ├── core/__init__.py        批次 2
│   ├── router/sniff.py         批次 3
│   ├── router/layer.py         批次 4
│   ├── router/__init__.py      批次 3(+批次 4 追加导出)
│   ├── workers/base.py         批次 5
│   ├── workers/http.py         批次 5
│   ├── workers/browser.py      批次 6(+批次 9 接入注入/守卫)
│   ├── workers/pool.py         批次 7
│   ├── workers/__init__.py     批次 5
│   ├── stealth/profiles.py     批次 8
│   ├── stealth/inject.py       批次 9
│   ├── stealth/netguard.py     批次 9
│   ├── stealth/behavior.py     批次 10
│   ├── stealth/__init__.py     批次 8(+批次 9/10 追加导出)
│   ├── extract/html.py         批次 11
│   ├── extract/jsondata.py     批次 12
│   ├── extract/pdf.py          批次 13
│   ├── extract/pipeline.py     批次 13
│   ├── extract/__init__.py     批次 11
│   ├── storage/vault.py        批次 14
│   ├── storage/export.py       批次 14
│   ├── storage/__init__.py     批次 13(+批次 14 追加导出)
│   ├── engine.py               批次 15
│   └── cli.py                  批次 15
└── tests/
    ├── test_queue.py           批次 2
    ├── test_sniff.py           批次 3
    ├── test_router.py          批次 4
    ├── test_extract_html.py    批次 11
    └── test_profiles.py        批次 8
```

## 4. 台账

已完成批次:**1 ~ 15(全部)**
当前批次:无
单元测试:**59 passed**
真实冒烟:静态站 / JS 站(分层升级)/ 指纹探针 / stats / export / resume 全部通过

| 批次 | 交付 | 自检结论 | 单测/冒烟 |
|---|---|---|---|
| 1 | pyproject, PROJECT_STATE, core/types.py | 无不确定 API;≈250 行 | 模型层约束在批次 2/8 的用例中被覆盖 |
| 2 | core/queue.py, core/__init__.py, test_queue.py | 无不确定 API;≈330 行**超限** | 9 项(含 4 进程 spawn 并发 claim) |
| 3 | router/sniff.py, router/__init__.py, test_sniff.py | 无不确定 API;≈290 行 | 15 项 |
| 4 | router/layer.py, test_router.py | 无不确定 API;≈180 行 | 9 项 |
| 5 | workers/base.py, workers/http.py, workers/__init__.py | 无不确定 API;http.py ≈330 行**超限** | 真实抓取冒烟 + SSRF 守卫实测 |
| 6 | workers/browser.py | 无不确定 API;≈245 行 | 真实渲染冒烟(静态站 + JS 站) |
| 7 | workers/pool.py | 无不确定 API;≈235 行 | 真实进程冒烟:分发/回收/崩溃重启 |
| 8 | stealth/profiles.py, stealth/__init__.py, test_profiles.py | 无不确定 API;≈330 行**超限** | 13 项(遍历全库一致性) |
| 9 | stealth/inject.py, stealth/netguard.py | 无不确定 API;≈250/120 行 | 真实浏览器冒烟(指纹自洽 + 第三方域拦截) |
| 10 | stealth/behavior.py | 无不确定 API;≈330 行**超限** | 统计性冒烟(泊松间隔/重尾/轨迹) |
| 11 | extract/html.py, extract/__init__.py, test_extract_html.py | 无不确定 API;≈300 行 | 12 项 |
| 12 | extract/jsondata.py | 无不确定 API;≈230 行 | 四条路径实跑 |
| 13 | extract/pipeline.py, extract/pdf.py, storage/__init__.py | 无不确定 API | 管线分发实跑 + PDF 真实提取 |
| 14 | storage/vault.py, storage/export.py | 无不确定 API;≈300/200 行 | 增量 diff + AES-GCM + 四格式导出实跑 |
| 15 | engine.py, cli.py, README.md | 无不确定 API;engine ≈420 行**超限** | 端到端抓取/续传/统计/导出实跑 |

## 5. 关键工程发现(全部为本机实测,非推测)

1. **Patchright 1.61.2 不执行任何脚本注入**。`context.add_init_script`、
   `page.add_init_script` 均不生效;`route.fetch()+fulfill()` 改写 HTML 也不生效。
   同一份代码在官方 Playwright 下正常。
   ⇒ 框架改为**运行时能力探测**(`BrowserWorker._probe_init_script()` 用一次
   `about:blank` 实测),不支持时降级为"加载后加固"(`harden_page`)。
2. **Windows 中文路径下 libcurl 无法加载 CA 证书**(`curl: (77) error adding trust anchors`)。
   ⇒ `workers/http.py::ensure_ascii_ca_bundle()` 把 certifi 的 CA bundle 复制到纯 ASCII
   路径并设置 `CURL_CA_BUNDLE`。
3. **Windows + spawn 不允许把同步原语放在 Process 子类实例属性上**(队列/Event),
   子进程会报 `DuplicateHandle [WinError 5]`——但这只是**症状**,真因是父进程
   pickle 失败(`urllib` opener 含 lambda 不可 pickle)。⇒ 三处修复:
   `__getstate__` 剔除运行时对象、opener 惰性构造、队列改用 `Manager().Queue()` 代理。
4. **无头 Chromium 的 `navigator.plugins` 为空**(真实 Chrome 有 5 个),是最容易被
   一条 JS 识破的特征 ⇒ 注入脚本补齐 5 个 PDF 插件与 mimeTypes。
5. **navigator.platform 等属性定义在 `Navigator.prototype` 上**,只在实例上
   `defineProperty` 会被原型上的 getter 抢先 ⇒ 注入脚本改为原型 + 实例双改。
6. **WebGL 掩码值不要误判**:`gl.VENDOR/gl.RENDERER` 在真实 Chrome 里就显示
   `WebKit | WebKit WebGL`,这不是注入失败;真正要对齐的是
   `WEBGL_debug_renderer_info` 里的 `UNMASKED_*`(37445/37446)。
7. **Nuxt 的 `window.__NUXT__` 不能用正则匹配大括号**(嵌套 JSON 会截断)
   ⇒ 改用 `json.JSONDecoder().raw_decode`;`JSON.parse("...")` 形态另用 JS 字符串
   字面量读取器(处理 `\"` 转义)。
8. **正文密度必须用子树文本**:原用"直接文本 ÷ 后代标签数"时,
   `<article><p>…</p></article>` 这类正文容器密度恒为 0,主区块永远选不中。
9. **`news.ycombinator.com` 与 `bot.sannysoft.com` 在本网络 TCP 层不可达**
   (连接超时),冒烟改用可达站点;这属于网络环境,不是框架缺陷。

## 6. 环境回执(第 10 节)

安装方式:项目内虚拟环境 `.venv`(Python 3.14.6 / pip 26.1.2)。
命令:`python -m venv .venv` → `./.venv/Scripts/python.exe -m pip install -e ".[dev]"`

### 6.1 依赖(真实 pip 输出,尾部)

```
Successfully installed Pillow-13.0.0 annotated-types-0.8.0 babel-2.18.0 beautifulsoup4-4.15.0
brotli-1.2.1 certifi-2026.10.23 cffi-2.1.1 charset-normalizer-3.5.2 click-8.5.0 colorama-0.4.6
courlan-1.4.0 cryptography-50.0.2 curl_cffi-0.16.0 dateparser-1.4.3 ddddocr-1.6.1 et-xmlfile-2.0.0
flatbuffers-25.12.19 greenlet-3.5.6 htmldate-1.10.0 idna-3.20 iniconfig-2.3.1 justext-3.0.2
lxml-6.1.1 lxml_html_clean-0.4.6 markdown-it-py-4.2.0 markdownify-1.2.3 mdurl-0.1.2
meilab-orchestral-crawler-0.1.0 mutagen-1.48.1 numpy-2.5.2 onnxruntime-1.31.1
opencv-python-5.0.0.93 opencv-python-headless-5.0.0.93 openpyxl-3.1.5 orjson-3.11.9
pycryptodome-3.23.0 pycryptodomex-3.24.1 pydantic-2.14.2 pydantic-core-2.50.2 pyee-13.0.1
pytest-9.2.0 python-dateutil-3.0.0 requests-2.34.2 rich-15.0.0 soupsieve-3.0.4
tld-0.13.2 trafilatura-2.2.0 typing-extensions-4.16.0 yt-dlp-2026.7.4 yt-dlp-ejs-0.8.0
```

首次安装失败(真实报错):

```
ERROR: Cannot install None, meilab-orchestral-crawler and meilab-orchestral-crawler[dev]==0.1.0
because these package versions have conflicting dependencies.
The conflict is caused by:
    meilab-orchestral-crawler 0.1.0 depends on numpy==2.5.2
    opencv-python-headless 4.12.0.88 depends on numpy<2.3.0 and >=2; python_version >= "3.9"
ERROR: ResolutionImpossible
```

处置(依 §10.5.2 如实记录差异):`opencv-python-headless` 由工程书钉死的 `4.12.0.88`
改为 **`5.0.0.93`**(PyPI 上与 numpy 2.5 兼容的最新 headless 版),`numpy` 保持钉死的
`2.5.2`。**其余 12 个组件版本与工程书完全一致。**

逐条自证:

```
patchright 1.61.2 | playwright 1.62.0 | trafilatura 2.2.0 | lxml (6,1,1,0) |
orjson 3.11.9 | openpyxl 3.1.5 | ddddocr ok | cv2 5.0.0 | numpy 2.5.2 |
pycryptodome "AES ok" | yt-dlp 2026.07.04 | pymupdf 2.0(PyMuPDF 2.0, MuPDF 1.28.5)
```

curl_cffi JA3:

```
ja3_hash = 35c61a611436bd206b042c36810d36ad
ja3 = 771,4865-4866-4867-49195-49199-49196-49200-52393-52392-60-61
```

> 工程书验证命令写的是 `...json()['ja3_hash']`,实际 peet.ws 当前把 ja3 字段放在
> `tls` 子对象下,顶层无该键(直接取会 KeyError),已按实际结构调整。

### 6.2 浏览器

```
$ patchright install chromium      # 无输出,退出码 0
$ playwright install chromium
已安装: chromium-1228 / chromium-1234 / chromium_headless_shell-1228 / -1234
```

Patchright 期望 revision 为 **1228**(与 `browsers.json` 一致),实际已安装。

真实启动自证:

```
TITLE_TEXT = ok
UA = Mozilla/5.0 test
WEBDRIVER = False
PATCHRIGHT_LAUNCH_OK
```

可选 Chrome:`patchright install chrome` 被拒(真实回执)

```
║ requires *removal* of a current installation first.
║ - Use "--force" to install browser:
║     patchright install --force chrome
```

⇒ 机器上已存在 Google Chrome,Patchright 拒绝覆盖。**不执行卸载**(破坏用户环境),
浏览器通道默认用 Patchright Chromium,`chrome` 渠道标记 degraded。

### 6.3 JS 运行时

```
$ winget install --id DenoLand.Deno --exact ...
Found Deno [DenoLand.Deno] Version 2.9.8
Successfully installed
$ deno --version
deno 2.9.8 (stable, release, x86_64-pc-windows-msvc)
v8 15.0.245.2-rusty
typescript 6.0.3
```

`yt-dlp --list-remote-components` 在 2026.7.4 上报
`error: no such option: --list-remote-components`;EJS 组件已随 `yt-dlp[default]`
装入(`yt-dlp-ejs-0.8.0`)。

### 6.4 降级矩阵实际状态

| 组件 | 状态 | 说明 |
|---|---|---|
| curl_cffi | 可用(需 ASCII CA 路径) | 见 5.2 |
| patchright / playwright | 可用 | Chromium 已装,启动自证通过 |
| Google Chrome | degraded | 需 `--force` 覆盖安装,拒绝执行 |
| ddddocr / opencv | 可用 | cv2 5.0.0 + onnxruntime 1.31.1 |
| yt-dlp / deno | 可用 | deno 2.9.8,yt-dlp-ejs 0.8.0 |
| pycryptodome | 可用 | AES ok |
| pymupdf | 可用(可选 extras) | 2.0,真实 PDF 提取成功 |
| trafilatura | 可用 | 密度算法失效时兜底,实测被触发 |

## 7. 执行证据(第 9 节,真实回执)

> 顺序按工程书要求:**先第 10 节装环境,再第 9 节跑业务冒烟**。

### 7.1 环境自检

```
Python 3.14.6
pip 26.1.2 from ... (python 3.14)
```

### 7.2 安装依赖 / 浏览器

见第 6 节(真实 pip 输出 + patchright/playwright 安装回执)。

### 7.3 冒烟(静态站)

```
$ mel crawl --url https://quotes.toscrape.com/ --max 20
queued 21 | dispatched 20 | succeeded 20 | failed 0 | blocked 0 | elapsed 22.53
通道分布: {'http': 20}
```

即:种子页 + 20 条同域链接被入队,全部走**轻量 HTTP 通道**,22.5 秒完成。

### 7.4 冒烟(JS 渲染站)

```
$ mel crawl --url https://quotes.toscrape.com/js/ --max 5
queued 3 | dispatched 5 | succeeded 3 | failed 0 | blocked 0 | escalated 2 | elapsed 10.23
通道分布: {'http': 3, 'browser': 2}
```

同一页的提取长度对比(仓库实测):

```
https://quotes.toscrape.com/js/        | len 1429   ← 浏览器通道(升级后)
https://quotes.toscrape.com/js/        | len 29     ← 轻量通道(静态 HTML 只有导航)
https://quotes.toscrape.com/           | len 2533
```

⇒ **分层升级生效**:静态启发无法预知页面是否需要 JS,先用便宜的 HTTP 试一次,
发现正文只有 29 字符且页面有脚本 → 任务回队打标 → 改走浏览器通道 → 拿到 1429 字符正文。

### 7.5 指纹校验

`bot.sannysoft.com` 在本网络不可达(TCP 超时),改用**本地指纹探针**:
与生产完全相同的 `BrowserWorker` + 设备档案 + 注入链路,逐项检查会被标红的特征。

```
engine: patchright | init_script_supported: False
抓取自检: 200 html_len 8940 ok True
实际使用档案: android_tablet_s9 | platform: Linux armv8l | locale: pt-BR | tz: America/Sao_Paulo

[通过/不触发检测]
  OK   window.chrome = object
  OK   window.chrome.runtime = True
  OK   cdc_ 痕迹数量 = 0
  OK   navigator.platform = Linux armv8l        ← 与档案一致
  OK   navigator.languages = ["pt","pt-BR"]     ← 与档案 locale 一致
  OK   navigator.hardwareConcurrency = 8
  OK   navigator.maxTouchPoints = 5             ← 移动档案的触摸能力
  OK   plugins 数量 = 5                         ← 真实 Chrome 也是 5
  OK   WebGL 掩码 = WebKit | WebKit WebGL
  OK   WebGL 未掩码 = Qualcomm | Adreno (TM) 740 ← 与 Android 档案 GPU 一致
[被标红]
  (无)
标红项: 0 / 11
```

⇒ 11 项检测特征**零标红**,且 UA / platform / locale / 时区 / 触摸 / GPU 全部自洽。

### 7.6 统计

```
$ mel stats
任务状态 {"pending": 0, "running": 0, "done": 3, "failed": 0, "dead_letter": 0}
Episode  {"success": 3, "failed": 0, "blocked": 0, "degraded": 2}
仓库     {"documents": 3, "total_seen": 3, "encrypted": false}
通道     {"browser": 2, "http": 3}
通道占比 轻量 60.00% / 浏览器 40.00%
域名分布 quotes.toscrape.com:3
```

### 7.7 导出

```
$ mel export --format markdown --output ./data
导出完成: 3 条记录,4 个文件
  - data\markdown\quotes.toscrape.com\quotes_to_scrape-4b498cbb.md
  - data\markdown\quotes.toscrape.com\quotes_to_scrape-42ce2823.md
  - data\markdown\quotes.toscrape.com\quotes_to_scrape-8bf5b816.md
  - data\markdown\quotes.toscrape.com\index.md
```

四种格式(jsonl / csv / markdown / xlsx)均实测产出文件。

### 7.8 断点续传

```
$ mel crawl --url https://quotes.toscrape.com/ --max 5
queued 21 | dispatched 12 | succeeded 5 | elapsed 16.32
$ mel resume --max 10
续传完成:成功 10 / 失败 0 / 共 0 个任务,20.16 秒
```

⇒ `resume` 从 SQLite 队列里继续取 pending 任务,不需要重新给种子 URL。

### 7.9 测试

```
$ pytest tests/ -v
collected 59 items
tests\test_extract_html.py ............   [ 20%]
tests\test_profiles.py ..............     [ 44%]
tests\test_queue.py .........              [ 59%]
tests\test_router.py .........             [ 74%]
tests\test_sniff.py ...............         [100%]
============================= 59 passed in 1.19s ==============================
```

## 8. 追加交付:交互窗口 `mel shell`(批次 15 之后)

用户需求:「复制一条链接按 Enter 就能直接爬」。

| 项 | 实现 |
|---|---|
| 入口 | `mel shell`(新增 click 子命令,实现在 `cli.py` 内,未新增包内模块) |
| 交互 | 粘贴整段文本 → 自动挑出其中第一个 URL(去空白/引号/句末标点)→ Enter 即抓 |
| 性能 | Worker 池**常驻复用**(`Engine._ensure_pool`),连续粘贴不必反复起进程,单条约 2~4 秒 |
| 分层 | `crawl_now()` 内含升级回环:轻量通道抓到空壳 → 引擎标 `escalate` → 第二轮走浏览器通道 |
| 命令 | `:help` `:stats` `:latest` `:export [jsonl|csv|markdown|xlsx]` `:quit` |
| 安全 | 输入即校验:仅 http/https,拒绝 localhost/环回/私网/保留地址(`workers.base.check_url_safe`) |
| 队列 | 新增 `TaskQueue.claim_by_urls()`:只领取指定 URL 的任务,不牵连队列里的遗留任务 |

实测回执:

```
url> 拒绝抓取 http://127.0.0.1:8080/admin
  原因: 目标 IP 127.0.0.1(127.0.0.1) 不是公网地址,按 SSRF 防护策略拒绝
url> 拒绝抓取 file:///C:/Windows/win.ini
  原因: 协议 'file' 不在允许列表 ['http', 'https']
url> 抓取中… https://quotes.toscrape.com/
✓ http 通道 | Quotes to Scrape | 2533 字符 | 2.6s
url> 抓取中… https://quotes.toscrape.com/js/
✓ browser 通道 | Quotes to Scrape | 1429 字符 | 4.1s
url> 抓取中… https://httpbin.org/json
✓ http 通道 | Wake up to WonderWidgets! | 223 字符 | 2.6s
url> :latest
Quotes to Scrape (1429 字符 / browser)
"The world as we have created it is a process of our thinking…"
url> :export jsonl
已导出 22 条 → 2 个文件
```

同 URL 重复粘贴会重置任务状态为 pending(交互语义:再抓一次),
不影响 `mel resume` 的断点续传语义。

## 9. 追加交付:B 站页面实抓引发的一轮修复(批次 15 之后)

用户给了 `https://www.bilibili.com/video/BV1Dvpg6BEt5/?share_source=copy_web&vd_source=...`
要求实抓,由此暴露并修复了 4 个真实问题(全部有实抓回执):

1. **验证码信号误报**。B 站每个页面都会**预加载**风控 SDK
   (`risk-captcha-sdk/CaptchaLoader.js`),页面里必然出现 "captcha" 字样,
   于是 `detect_anti_bot_signals` 把正常页面判成 `captcha` 拦截,
   浏览器通道的 Episode 被标成 blocked。
   ⇒ 修复:检测前先整段剔除 `<script>`,只对"脚本之外"的组件容器/可见文案
   判验证码。修复后该页 `signals: []`、`ok: True`。
   回归验证(真阳性仍要命中):
   ```
   '<div class="g-recaptcha"></div>'          -> ['captcha']
   '<div id="captcha-container">请完成验证</div>' -> ['captcha']
   '<script src=".../CaptchaLoader.js"></script><p>正常内容</p>' -> []
   '<script>var captcha=1</script>'           -> []
   'Just a moment...'                          -> ['status_503', 'cloudflare_challenge']
   ```
2. **用户点名的 URL 不优先**。队列里遗留的"已发现链接"会插队,导致用户点了 A 却跑出 B。
   ⇒ 修复:`_claim_prioritised()` 先按显式 URL 领(`claim_by_urls`),再从通用队列补;
   `_reset_urls()` 把显式 URL 重置为 pending(允许重跑已抓过的页面)。
3. **`--max N` 会超发**。在途任务不占预算,空转等待时会多派发。
   ⇒ 修复:容量 = `min(max_inflight - inflight, budget - inflight)`。
   修复后 `--max 1` 精确派发 1 个任务。
4. **结构化数据没进库**。B 站页面的 JSON-LD 里带有 uploadDate / duration /
   播放量 / 关键词 / 作者,之前只记了条数。
   ⇒ 修复:`pipeline._structured_fields()` 抽取这些标量字段写入 Episode 的
   ``raw_meta.structured``。

B 站实抓结果(修复后):

```
$ mel crawl --url "https://www.bilibili.com/video/BV1Dvpg6BEt5/?share_source=copy_web&vd_source=..." --max 1
dispatched 1 | succeeded 1 | failed 0 | blocked 0 | unchanged 1 | elapsed 2.05
已抓取的页面:
  - https://www.bilibili.com/video/BV1Dvpg6BEt5/?share_source=copy_web&vd_source=...
通道分布: {'http': 1}
```

抓到的是该视频的真实简介(1051 字符),标题为
「【ASMR/KU100】超级温柔的深度采耳⟡ 让人浑身酥麻的舒适感..【鬼花/malice】_哔哩哔哩_bilibili」。
`unchanged 1` 说明 SHA256 增量比对认出内容与上次一致。
浏览器通道对比:状态 200、渲染后 DOM 89060 字节、提取 788~1429 字符、
JSON-LD 命中 3 条(含 VideoObject 的 uploadDate/duration/播放量/关键词)。

## 10. 已知限制与后续待办

1. **超过 300 行的文件(4 个)**:`core/queue.py`、`workers/http.py`、`stealth/profiles.py`、
   `stealth/behavior.py`、`engine.py`。已冻结不再加功能;后续扩展应另开文件。
2. **Patchright 不支持注入**:浏览器通道的指纹覆盖只能发生在"加载之后"，
   对"页面脚本一加载就采集指纹"的站点仍会露出原生值。可切官方 playwright 换取
   文档级注入(代价是暴露 CDP 调试器)。
3. **验证码分支未接流程**:ddddocr/opencv 已装,但未接入提取管线(工程书 15 个批次未要求)。
4. **视频通道未实现**:yt-dlp/deno 已装,框架无该通道(`mel media` 未提供)。
5. **未做的压测**:多站点高并发、长时间运行的内存曲线。
6. **推送**:本地已完成 git 提交;推送到 GitHub 需要用户提供 SSH 密钥。

## 11. 保守决策记录

- `Episode.rounds_runned` 沿用工程书原文拼写(疑似笔误),不做重命名,仅在 docstring 标注。
- `DeviceProfile.is_mobile` 与 `mobile` 并存:Playwright 的 `is_mobile` 与工程书的
  `mobile` 语义可能不同,双字段并存并在 validator 中同步校验。
- 设备档案的 locale/timezone **按构造保证一致**(由 `sale_region` 经 `REGIONS` 表推导),
  不允许手写——这比"逐条手写并校验"更不容易出错。
- 行为模拟的随机源用 `random.Random`(非密码学用途),但**种子**来自 `secrets.randbits(64)`;
  显式传 seed 时可复现,便于排障。
- 网络守卫在 Context 创建时按当前域绑定白名单;跨域时 Context 会被销毁重建,
  守卫随之更新,不会出现"白名单锁死在第一个域"的问题。
