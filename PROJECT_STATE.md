# PROJECT_STATE —— mei-lab-orchestral-crawler 工程台账

> **这是本工程的唯一记忆。**
> 上下文丢失时直接读本文件即可继续,不必从头考古。
>
> 纪律(来自姊妹工程 kiana-crawler 的《旧版本问题与整改记录》):
>
> 1. **判据必须查结构,不许查文本。**
> 2. **「没有告警」不等于「通过」**——正面结论必须形成可断言状态。
> 3. **复核命令必须真能跑出它上面的数字。**
> 4. **计数口径必须与被移动的状态同源。**
> 5. 横向原则:**同一能力只允许一份实现。**

本文件里的每一个数字都附**可现场执行的复核命令**;命令与文档不一致时,以命令为准。

---

## 目录

1. [项目定位](#1-项目定位)
2. [技术栈](#2-技术栈)
3. [交付文件清单](#3-交付文件清单)
4. [台账总表](#4-台账总表)
5. [关键工程发现](#5-关键工程发现)
6. [环境回执](#6-环境回执)
7. [执行证据](#7-执行证据)
8. [打通路线回执](#8-打通路线回执)
9. [已知限制与后续待办](#9-已知限制与后续待办)
10. [保守决策记录](#10-保守决策记录)
11. [复核命令速查](#11-复核命令速查)

---

## 1. 项目定位

**分层调度 + 多格式提取 + 反检测**的 Python 采集引擎。

- 核心原则:**最快路径优先**——能用一次 HTTP 请求解决的,绝不开浏览器。
- 形态:**CLI + 可嵌入库**。不是 GUI 产品、不是服务、不支持多用户。
- 项目归属:Lob 体系的采集引擎组成部分。

### 1.1 明确不做的事(边界写死)

| 不做 | 原因 |
|---|---|
| asyncio / aiohttp / httpx | 工程书第 1 节封闭集合 |
| Redis / 分布式队列 | 同上;单机场景收益低 |
| 外部 API key / 云服务 | 同上 |
| GUI / 安装包 | 形态定位为引擎;日常交互用 `mel shell` |
| 绕过登录墙 / 付费墙 / 验证码 | 伦理红线 |
| 采集账号权限内内容 | 伦理红线 |

---

## 2. 技术栈

### 2.1 运行时依赖(全部版本钉死)

| 组件 | 版本 | 用途 |
|---|---|---|
| pydantic | 2.14.2 | 数据模型 |
| click | 8.5.0 | 命令行 |
| rich | 15.0.0 | 终端渲染 |
| beautifulsoup4 | 4.15.0 | HTML 解析 |
| lxml | 6.1.1 | BS4 解析后端 |
| markdownify | 1.2.3 | HTML → Markdown |
| cryptography | 50.0.2 | 内容仓库 AES-256-GCM、cookie Fernet |
| curl_cffi | 0.16.0 | 轻量通道 TLS/JA3 指纹模拟 |
| patchright | 1.61.2 | 浏览器通道主引擎(隐身版) |
| playwright | 1.62.0 | patchright 缺失时的回退引擎 |
| trafilatura | 2.2.0 | 正文提取兜底 |
| orjson | 3.11.9 | 高速 JSON 序列化 |
| openpyxl | 3.1.5 | xlsx 导出 |
| ddddocr | 1.6.1 | 本地 OCR 验证码识别 |
| opencv-python-headless | 5.0.0.93 | 滑块缺口检测 |
| numpy | 2.5.2 | OpenCV 数值底座 |
| pycryptodome | 3.23.0 | m3u8 AES-128 分片解密 |
| yt-dlp | 2026.7.4 | 通用媒体通道外部引擎 |
| PyYAML | 6.0.3 | 声明式站点规则 |

### 2.2 可选 extras

| extras | 内容 | 缺失时的行为 |
|---|---|---|
| `pdf` | pymupdf 2.0 | PDF 任务以"不支持 PDF"入失败队列,其余通道不受影响 |
| `dev` | pytest、ruff、mypy、pip-audit | 仅开发用 |

### 2.3 系统级依赖

| 组件 | 版本 | 用途 |
|---|---|---|
| Deno | 2.9.8 | yt-dlp 的 EJS 挑战 JS 运行时(缺失则视频通道 degraded) |
| ffmpeg | 可选 | m3u8 分片合并(缺失则只保留分片) |

---

## 3. 交付文件清单

```
meilab_orchestral_crawler/
├── pyproject.toml                打包与依赖声明
├── README.md                     用户文档(命令手册 + 架构 + 伦理)
├── PROJECT_STATE.md              本文件
├── LICENSE                       MIT
├── .gitignore
├── fingerprint_probe.py          指纹自证探针(等价于 bot.sannysoft.com)
├── rules/sites/
│   ├── bilibili-video.yaml       B 站视频页规则
│   ├── zhihu-question.yaml       知乎问答页规则
│   └── template.yaml             新站点规则模板
├── tools/
│   ├── release_check.py          发版门禁(6 项)
│   └── doc_numbers.py            文档数字同源(--check / --write)
├── meilab/
│   ├── __init__.py
│   ├── core/
│   │   ├── types.py              Task / Episode / DeviceProfile / RoundState / MediaItem
│   │   ├── queue.py              SQLite 队列:状态机 + 租约 + CAS + 媒体表 + 迁移
│   │   └── __init__.py
│   ├── router/
│   │   ├── sniff.py              静态嗅探(URL/内容类型/媒体/反爬信号)
│   │   ├── layer.py              分层路由(ChannelDecision)
│   │   ├── feed.py               RSS/Atom/sitemap 播种(防 XML 实体扩展)
│   │   └── __init__.py
│   ├── workers/
│   │   ├── base.py               Worker 进程骨架 + SSRF 校验原语
│   │   ├── http.py               轻量通道(curl_cffi + urllib 回退 + 限速 + 冷却)
│   │   ├── browser.py            浏览器通道(独立 Chromium + 档案 + 注入 + 守卫)
│   │   ├── pool.py               Worker 池(spawn/join/崩溃重启/每域上限)
│   │   └── __init__.py
│   ├── extract/
│   │   ├── html.py               密度启发 + 去噪 + 三层 residual
│   │   ├── jsondata.py           JSON API / JSON-LD / __NEXT_DATA__ / __NUXT__
│   │   ├── pdf.py                PDF 提取(可选 pymupdf,优雅降级)
│   │   ├── pipeline.py           提取管线编排
│   │   ├── site_rules.py         声明式站点规则解释器
│   │   └── __init__.py
│   ├── stealth/
│   │   ├── profiles.py           21 台自洽虚拟设备
│   │   ├── inject.py             指纹注入(CDP 痕迹清除 + 档案注入)
│   │   ├── behavior.py           泊松打字 / 对数正态停留 / 贝塞尔轨迹
│   │   ├── netguard.py           网络守卫(拦截非目标域)
│   │   ├── captcha.py            验证码识别(ddddocr + cv2)
│   │   └── __init__.py
│   ├── media/
│   │   ├── ytdlp_channel.py      通用 yt-dlp 通道
│   │   ├── m3u8_downloader.py    HLS 分片 + AES-128 + 分片续传
│   │   ├── resolvers.py          五平台解析(B站原生 API + 四家委托 yt-dlp)
│   │   ├── wbi.py                B 站 WBI 签名
│   │   ├── cookies.py            cookie 校验 / Fernet 加密 / Cookie 头
│   │   ├── downloader.py         媒体下载编排
│   │   └── __init__.py
│   ├── privacy/
│   │   ├── sanitizer.py          三层脱敏
│   │   └── __init__.py
│   ├── storage/
│   │   ├── vault.py              内容仓库(SHA256 增量 + AES-256-GCM)
│   │   ├── export.py             五种导出(jsonl/csv/markdown/xlsx/sqlite+FTS5)
│   │   ├── quality.py            SimHash 近重复 + 质量闸
│   │   ├── observability.py      stats.jsonl / checkpoint / deadletter
│   │   └── __init__.py
│   ├── engine.py                 主循环编排
│   └── cli.py                    命令行
└── tests/
    ├── test_queue.py             队列:幂等/并发 claim/状态机
    ├── test_queue_lease.py       租约 / CAS / 回收
    ├── test_sniff.py             嗅探:URL 模式 → 通道
    ├── test_router.py            路由:任务类型 / URL → Worker
    ├── test_extract_html.py      提取:密度 / 去噪 / 三层 residual
    ├── test_profiles.py          设备档案:遍历全库一致性
    ├── test_media.py             媒体:m3u8 / AES-128 / 平台 / cookie
    ├── test_ssrf.py              SSRF:26 条对抗样本
    ├── test_captcha.py           验证码:能力探测 / OCR / 滑块
    └── test_privacy_quality.py   脱敏边界 / SimHash / 质量闸
```

---

## 4. 台账总表

### 4.1 原始 15 批次(工程书定义)

| 批次 | 交付 | 自检结论 | 验证 |
|---|---|---|---|
| 1 | pyproject, PROJECT_STATE, core/types.py | 无不确定 API;≈250 行 | 模型约束被后续用例覆盖 |
| 2 | core/queue.py, core/__init__.py, test_queue.py | 无不确定 API;≈330 行**超限** | 9 项(含 4 进程 spawn 并发 claim) |
| 3 | router/sniff.py, router/__init__.py, test_sniff.py | 无不确定 API;≈290 行 | 15 项 |
| 4 | router/layer.py, test_router.py | 无不确定 API;≈180 行 | 9 项 |
| 5 | workers/base.py, workers/http.py, workers/__init__.py | 无不确定 API;http.py ≈330 行**超限** | 真实抓取 + SSRF 实测 |
| 6 | workers/browser.py | 无不确定 API;≈245 行 | 真实渲染(静态站 + JS 站) |
| 7 | workers/pool.py | 无不确定 API;≈235 行 | 真实进程:分发/回收/崩溃重启 |
| 8 | stealth/profiles.py, stealth/__init__.py, test_profiles.py | 无不确定 API;≈330 行**超限** | 13 项(遍历全库一致性) |
| 9 | stealth/inject.py, stealth/netguard.py | 无不确定 API;≈250/120 行 | 真实浏览器冒烟 |
| 10 | stealth/behavior.py | 无不确定 API;≈330 行**超限** | 统计性冒烟(泊松/重尾/轨迹) |
| 11 | extract/html.py, extract/__init__.py, test_extract_html.py | 无不确定 API;≈300 行 | 12 项 |
| 12 | extract/jsondata.py | 无不确定 API;≈230 行 | 四条路径实跑 |
| 13 | extract/pipeline.py, extract/pdf.py, storage/__init__.py | 无不确定 API | 管线分发 + 真实 PDF |
| 14 | storage/vault.py, storage/export.py | 无不确定 API | 增量 + AES-GCM + 四格式 |
| 15 | engine.py, cli.py, README.md | 无不确定 API | 端到端抓取/续传/统计/导出 |

### 4.2 追加交付

| 阶段 | 内容 | 提交 |
|---|---|---|
| 交互窗口 | `mel shell`:粘贴即抓;常驻 Worker 池 | `eee28cb` |
| B 站实抓修复 | 验证码误报 / URL 优先 / `--max` 超发 / 结构化字段入库 | `d050d45` |
| 文档编号修正 | 台账章节编号 | `8a630f6` |
| **阶段 0** | 媒体通道契约层(MediaItem / media 表 / Channel.MEDIA) | `cccec02` |
| **阶段 1** | 媒体通道(通用 yt-dlp + m3u8/AES-128 + 五平台 + cookie) | `8bbac5a` |
| **阶段 2** | P0 正确性(租约/CAS/Retry-After/每域限速/SSRF 加固)+ 验证码分支 | `4c3cda2` |
| **阶段 3** | 数据卫生(三层脱敏/SimHash/质量闸/缓存/播种/YAML 规则/SQLite 导出) | `9d7a464` |
| **阶段 4** | 可观测(stats/checkpoint/死信)+ 发版门禁 + 数字同源 | `2a8e333` |
| **阶段 5** | 终验:升级预算修复 + 数字同源写入 + 文档终版 | `1ead3cc` `9956f02` |

### 4.3 当前状态

```
测试        178 passed(10 个文件 / 123 个测试函数)
发版门禁    6 项 · 通过 6 · 失败 0 · 跳过 0
ruff 基线   0(只降不升)
依赖漏洞    未发现
密钥泄漏    未发现
```

---

## 5. 关键工程发现

**全部为本机实测,不是推测。**每条对应第 7 节的回执。

| # | 发现 | 根因 | 处置 |
|---|---|---|---|
| 1 | **Patchright 1.61.2 不执行任何脚本注入** | 其驱动刻意忽略注入内容(`add_init_script` 与 `route.fulfill` 改写 HTML 均无效,官方 Playwright 正常) | 引入**运行时能力探测**(`_probe_init_script()` 用 `about:blank` 实测);不支持时降级为"加载后加固"(`harden_page`) |
| 2 | **Windows 中文路径下 libcurl 无法加载 CA 证书**(`curl: (77) error adding trust anchors`) | libcurl 不接受非 ASCII 路径的 CA 文件 | `ensure_ascii_ca_bundle()` 把 certifi 的 CA 复制到纯 ASCII 路径并设 `CURL_CA_BUNDLE` |
| 3 | **`DuplicateHandle [WinError 5]` 是假象** | 真因是父进程 pickle Worker 实例失败(`urllib` opener 含 lambda) | 三处:`__getstate__` 剔除运行时对象、opener 惰性构造、队列改 `Manager().Queue()` 代理 |
| 4 | **navigator 属性在原型上** | 只在实例 `defineProperty` 会被 `Navigator.prototype` 的 getter 抢先 | 注入脚本改为原型 + 实例双改 |
| 5 | **无头 Chromium `navigator.plugins` 为空** | 真实 Chrome 有 5 个 PDF 插件,这是最容易被一条 JS 识破的特征 | 注入脚本补齐 5 个插件与 mimeTypes |
| 6 | **WebGL 掩码值不是注入失败** | 真实 Chrome 里 `gl.VENDOR` 就显示 `WebKit` | 改为对齐 `WEBGL_debug_renderer_info` 的 `UNMASKED_*`(37445/37446) |
| 7 | **验证码误报** | 只做裸字符串匹配,B 站每页预加载 `risk-captcha-sdk/CaptchaLoader.js` | 检测前剔除 `<script>` 段,只看脚本外的组件容器与可见文案 |
| 8 | **正文密度必须用子树文本** | 用"直接文本 ÷ 后代标签数"时 `<article><p>…</p></article>` 密度恒为 0 | 改为"子树文本长度 ÷ (1 + 后代标签数)" |
| 9 | **骨架页会骗过密度算法** | 骨架页导航文字少但结构像正文 | 叠加"最长可见文本块"判据 + 质量闸 + 分层升级 |
| 10 | **B 站评论匿名不可读** | 新接口 `/x/v2/reply/wbi/main` 需 WBI 签名 + 登录态,带浏览器 cookie(buvid3/b_nut)仍 -403 | 支持 `--cookies`;匿名时**如实报出平台原因** |
| 11 | **升级到浏览器通道不能消耗 `--max` 预算** | "轻量试一次→升级"把预算吃掉,循环退出,任务永远卡 pending | `_handle_result` 返回"是否终态",升级返回 `False`,`processed` 不计数 |
| 12 | **Nuxt 的 `window.__NUXT__` 不能用正则匹配大括号** | 嵌套 JSON 被非贪婪匹配截断 | 改用 `json.JSONDecoder().raw_decode`;`JSON.parse("...")` 形态另用 JS 字符串字面量读取器 |
| 13 | **yt-dlp 的中间态不是产物** | `.part` / `.ytdl` 会被误当成下载成果 | 产物认定用**目录快照差集**,只认完整文件 |
| 14 | **平台解析器不是唯一路径** | 抖音/小红书/快手/网易云直链需请求签名,平台一次变更即失效 | 这四家统一委托通用 yt-dlp 通道;B 站走原生 API;失败自动回退 |
| 15 | **`opencv-python-headless 4.12.0.88` 与 `numpy==2.5.2` 冲突** | 前者要求 `numpy<2.3.0` | 改用 `5.0.0.93`;其余 12 个组件版本与工程书一致 |
| 16 | **B 站 CDN 支持 Range** | 实测 `Range: bytes=0-1023` → `206`,总长与 playurl 一致 | 直链下载器可续传 |

---

## 6. 环境回执

安装方式:项目内虚拟环境 `.venv`(Python 3.14.6 / pip 26.1.2)。

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

### 6.1 依赖安装(真实 pip 输出,尾部)

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

首次安装失败(真实报错,如实留档):

```
ERROR: Cannot install None, meilab-orchestral-crawler and meilab-orchestral-crawler[dev]==0.1.0
because these package versions have conflicting dependencies.
The conflict is caused by:
    meilab-orchestral-crawler 0.1.0 depends on numpy==2.5.2
    opencv-python-headless 4.12.0.88 depends on numpy<2.3.0 and >=2; python_version >= "3.9"
ERROR: ResolutionImpossible
```

处置:`opencv-python-headless` 由 `4.12.0.88` 改为 `5.0.0.93`(与 numpy 2.5 兼容的最新 headless 版),
`numpy` 保持 `2.5.2`。**其余 12 个组件版本与工程书钉死值完全一致。**

### 6.2 逐条自证

```
patchright 1.61.2 | playwright 1.62.0 | trafilatura 2.2.0 | lxml (6,1,1,0) |
orjson 3.11.9 | openpyxl 3.1.5 | ddddocr ok | cv2 5.0.0 | numpy 2.5.2 |
pycryptodome "AES ok" | yt-dlp 2026.07.04 | pymupdf 2.0(PyMuPDF 2.0, MuPDF 1.28.5) |
pydantic 2.14.2 | PyYAML 6.0.3
```

curl_cffi JA3(说明 TLS 指纹模拟生效):

```
ja3_hash = 35c61a611436bd206b042c36810d36ad
ja3 = 771,4865-4866-4867-49195-49199-49196-49200-52393-52392-60-61
```

> 工程书验证命令写的是 `...json()['ja3_hash']`,实际 peet.ws 当前把 ja3 字段放在
> `tls` 子对象下,顶层无该键(直接取会 KeyError),已按实际结构调整。

### 6.3 浏览器

```
$ patchright install chromium      # 无输出,退出码 0
$ playwright install chromium
已安装: chromium-1228 / chromium-1234
         chromium_headless_shell-1228 / chromium_headless_shell-1234
```

Patchright 期望 revision 为 **1228**(与 `browsers.json` 一致),实际已安装。

真实启动自证:

```
TITLE_TEXT = ok
UA = Mozilla/5.0 test
WEBDRIVER = False
PATCHRIGHT_LAUNCH_OK
```

可选 Chrome 被拒(真实回执):

```
║ requires *removal* of a current installation first.
║ - Use "--force" to install browser:
║     patchright install --force chrome
```

⇒ 机器上已存在 Google Chrome,Patchright 拒绝覆盖。**不执行卸载**(破坏用户环境),
浏览器通道默认用 Chromium,`chrome` 渠道标记 degraded。

### 6.4 JS 运行时

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

### 6.5 降级矩阵实际状态

| 组件 | 状态 | 说明 |
|---|---|---|
| curl_cffi | 可用(需 ASCII CA 路径) | 见发现 #2 |
| patchright / playwright | 可用 | Chromium 已装,启动自证通过;注入能力受限(见发现 #1) |
| Google Chrome | degraded | 需 `--force` 覆盖安装,拒绝执行 |
| ddddocr / opencv | 可用 | cv2 5.0.0 + onnxruntime 1.31.1 |
| yt-dlp / deno | 可用 | deno 2.9.8,yt-dlp-ejs 0.8.0 |
| pycryptodome | 可用 | AES ok |
| pymupdf | 可用(可选 extras) | 2.0,真实 PDF 提取成功 |
| trafilatura | 可用 | 密度算法失效时兜底,实测被触发 |
| PyYAML | 可用 | 站点规则 |
| pip-audit | 可用(开发依赖) | 发版门禁用,2.10.1 |

---

## 7. 执行证据

### 7.1 §9 冒烟全清单

| # | 项目 | 命令 | 回执 |
|---|---|---|---|
| 1 | 静态站 | `mel crawl --url https://quotes.toscrape.com/ --max 5` | queued 21 / dispatched 5 / **succeeded 5** / failed 0 / 22.5s |
| 2 | JS 渲染站 | `mel crawl --url https://quotes.toscrape.com/js/ --max 3` | 通道分布 **http 3 / browser 3**;`/js/` 页 http 通道 0 字符 → 升级 → browser **1429 字符** |
| 3 | 指纹校验 | `python fingerprint_probe.py` | **标红 0 / 10**,UA/platform/locale/触摸/GPU 全部自洽 |
| 4 | 统计 | `mel stats` | 通道占比 轻量 60% / 浏览器 40%;仓库 3 篇;域名分布正确 |
| 5 | 导出 | `mel export --format <fmt> --output ./data` | 五格式全过:jsonl 1 / csv 1 / markdown 4 / xlsx 1 / **sqlite 1** |
| 6 | 断点续传 | `mel resume --max 10` | 成功 10 / 失败 0 / 20.16s |
| 7 | 媒体通道 | `mel resolve --url <B站>` | **B 站原生 API**;720P / mp4 / 453,914,132 字节;时长 6972s |
| 8 | 交互窗口 | `mel shell` | ✓ http 通道 / Quotes to Scrape / 2533 字符 / 3.2s |
| 9 | 站点规则 | `mel rule validate` / `mel rule test` | 规则通过;坏规则报 3 条问题;真实页抽出 title/author/description/thumbnail |

### 7.2 指纹自证(完整回执)

```
engine: patchright | init_script_supported: False
抓取自检: 200 html_len 8940 ok True
实际使用档案: android_tablet_s9 | platform: Linux armv8l | locale: pt-BR | tz: America/Sao_Paulo

[通过/不触发检测]
  OK   window.chrome = object
  OK   window.chrome.runtime = True
  OK   cdc_ 痕迹数量 = 0
  OK   navigator.platform = Linux armv8l
  OK   navigator.languages = ["pt","pt-BR"]
  OK   navigator.hardwareConcurrency = 8
  OK   navigator.maxTouchPoints = 5
  OK   plugins 数量 = 5
  OK   WebGL 掩码 = WebKit | WebKit WebGL
  OK   WebGL 未掩码 = Qualcomm | Adreno (TM) 740
[被标红]
  (无)
标红项: 0 / 10
```

### 7.3 媒体通道

```
# 通用 yt-dlp 通道
ytdlp resolve ok: True | title: master | formats: 27
ytdlp test-download ok: True | files: ['master [master].mp4'] | partial: [] | size: 6163473

# m3u8 明文(Apple 公共测试流)
hls: {'ok': True, 'segments': 5, 'bytes': 7717212, 'resumed': False, 'error': ''}
hls-resume: {'ok': True, 'segments': 5, 'bytes': 4624988, 'resumed': True, 'error': ''}
master playlist: is_master True variant prog_index.m3u8
direct: {'ok': True, 'bytes': 5731, 'resumed': False, 'error': ''}

# B 站原生解析与 CDN 续传能力
bilibili item: ('【ASMR/KU100】超级温柔的深度采耳⟡...', 'bilibili_api', 1, 453914132)
bilibili CDN range: 206 | got bytes: 1024 | content-range: bytes 0-1023/453914132
```

### 7.4 阶段 2 运行时行为

```
retry-after 120s: 120.0
HTTP date 解析: 89.9 秒
retry-after bad: 0.0
同域派发数(上限2): 2 | inflight: {'same.example.com': 2}
释放一个后 inflight: {'same.example.com': 1}
领取后状态: {'pending': 0, 'running': 1, ...}
回收过期租约: 1 → {'pending': 1, 'running': 0, ...}
```

### 7.5 SSRF 对抗样本

26 条全部拦截(环回 / 私网 / 链路本地 / CGNAT / 6to4 / 基准测试段 / 云元数据 /
十进制·十六进制·八进制·省略段数字字面量 / IPv6 zone id / FQDN 尾点 / 协议白名单)。

### 7.6 发版门禁

```
[OK]     版本一致性: 版本一致:0.1.0
[OK]     Git 工作区干净: 工作区干净
[OK]     全量测试: ..................................                  [100%]
[OK]     ruff 静态基线: ruff 错误 0 个(基线 0,只降不升)
[OK]     依赖漏洞审计: 未发现已知漏洞
[OK]     密钥泄漏扫描: 未发现疑似密钥

总计 6 项:通过 6 · 失败 0 · 跳过 0
```

### 7.7 进程卫生

正常抓取后**我们的 python 进程残留 = 0**(实测)。
之前观察到的 2 个残留来自被中断的运行,不是泄漏。

---

## 8. 打通路线回执

> 依据《kiana-crawler × mei-lab 对比报告》,按"五阶段全量清零"执行。
> 每阶段独立提交、独立可交付。

### 阶段 0:契约与骨架(`cccec02`)

`MediaItem` / `MediaKind` / `MediaStatus` / `MediaPlatform` / `TaskType.MEDIA`;
`media_downloads` 表 + `PRAGMA user_version` 迁移(v1);`Channel.MEDIA` + `detect_media()`;
`WorkerKind.MEDIA`;3 处动态拼接 SQL 改参数化语句。

实测:`user_version = 1`;媒体记录写入/更新/统计正常;**73 passed**。

### 阶段 1:媒体通道(`8bbac5a`)

新增 `meilab/media/`(7 个模块)。分发优先级:
**m3u8 直链 → 平台原生直链 → 通用 yt-dlp 通道**。

| 模块 | 职责 |
|---|---|
| `ytdlp_channel.py` | 通用 yt-dlp 通道(subprocess 调 `python -m yt_dlp`;`--continue` 续传;`--test` 快速验证;产物认定用目录快照差集;JS 运行时自动探测) |
| `m3u8_downloader.py` | 标准库解析 m3u8 + pycryptodome AES-128-CBC + **分片级续传** + 分片重试 |
| `resolvers.py` | 五平台:B 站原生 API;抖音/小红书/快手/网易云委托通用通道 |
| `wbi.py` | B 站 WBI 签名 |
| `cookies.py` | cookie 校验 / Fernet 加密存储 / Netscape → Cookie 头 |
| `downloader.py` | 编排 + 直链续传下载器 |

新命令:`mel media` / `mel resolve` / `mel comments`。**89 passed**。

### 阶段 2:P0 正确性 + 验证码(`4c3cda2`)

租约 + CAS(修报告 D1:Worker 崩溃后任务永久卡 running);`Retry-After` + 域名冷却;
robots `Crawl-delay` + 每域并发上限(默认 20);SSRF 加固(10 跳 / DNS TTL 90s /
主机名规范化 / 数字字面量还原 / 补私网段);验证码分支(ddddocr + cv2,识别成功回填重试一次,
识别不可用按 §10.4 降级入死信并如实报原因)。**150 passed**。

### 阶段 3:数据卫生与提取扩展(`9d7a464`)

三层脱敏(修报告 D5);SimHash + 质量闸;声明式站点规则(3 条 + 模板 + `rule validate/test`);
ETag/Last-Modified 条件缓存;RSS/Atom/sitemap 播种(defusedxml + DTD 拒绝 + 5MiB 上限);
导出加 SQLite(FTS5);orjson 热路径。

### 阶段 4:可观测与发版门禁(`2a8e333`)

`stats.jsonl` / `checkpoint.json` / `deadletter.jsonl`;`tools/release_check.py` 6 项门禁;
`tools/doc_numbers.py` 数字同源。

### 阶段 5:终验(`1ead3cc` `9956f02`)

修掉「升级不消耗 `--max` 预算」;`doc_numbers.py` 增加 `--write`;
§9 冒烟全清单重跑;门禁 6/6 全绿;文档终版。**178 passed**。

---

## 9. 已知限制与后续待办

### 9.1 明确的限制(实测结论,非猜测)

| # | 限制 | 说明 |
|---|---|---|
| 1 | Patchright 下注入只发生在"加载之后" | 引擎能力探测的必然结果;需要文档级注入时切官方 playwright(代价是暴露 CDP) |
| 2 | B 站评论匿名不可读 | 平台限制,需自备 cookie |
| 3 | 抖音/小红书/快手/网易云走通用通道 | 直链签名易被平台变更打断;不保证长期可用 |
| 4 | `news.ycombinator.com`、`bot.sannysoft.com` 本网络不可达 | 环境限制 |
| 5 | Google Chrome 未安装 | 机器上已有 Chrome,Patchright 拒绝覆盖 |
| 6 | 站点规则只有 3 条 | 规则系统已闭环,内容待扩 |
| 7 | 覆盖率只生成报告,不设阈值 | 事故回归比行覆盖率更能代表质量目标 |

### 9.2 超 300 行的文件(工程书的质量红线)

| 文件 | 量级 | 处置 |
|---|---|---|
| `engine.py` | 最大 | 编排集中;再拆需先划清职责边界 |
| `core/queue.py` | ≈470 | 已冻结,扩展另开文件 |
| `workers/http.py` | ≈380 | 同上 |
| `media/resolvers.py` | ≈390 | 五平台元数据与注释占体积 |
| `stealth/profiles.py` | ≈350 | 设备声明表占体积;新增设备追加声明而非逻辑 |
| `stealth/behavior.py` | ≈330 | 同上 |
| `media/m3u8_downloader.py` | ≈330 | — |

### 9.3 后续待办

1. 声明式规则扩面(目标:主流新闻/论坛/电商各 2–3 条);
2. 代理出口管理与粘性会话;
3. 覆盖率阈值(先只报告,积累足够事故回归后再设阈值);
4. 任务优先级分层(目前单一 priority 字段);
5. `mel media` 的批量模式与断点续传编排。

---

## 10. 保守决策记录

| # | 决策 | 理由 |
|---|---|---|
| 1 | `Episode.rounds_runned` 沿用工程书原文拼写 | 不擅自改字段契约,只在 docstring 标注 |
| 2 | `DeviceProfile.is_mobile` 与 `mobile` 并存 | Playwright 的 `is_mobile` 与工程书的 `mobile` 语义可能不同,双字段并存并在 validator 中同步校验 |
| 3 | 设备档案的 locale/timezone **按构造保证一致** | 由 `sale_region` 经 `REGIONS` 表推导,不允许手写——比"逐条手写并校验"更不容易出错 |
| 4 | 行为模拟用 `random.Random`,但种子来自 `secrets.randbits(64)` | 行为统计模拟不是密码学用途;需要"给定种子可复现"以便排障 |
| 5 | SimHash 词哈希用 SHA-256 截断 | SimHash 只要求词哈希**均匀分布**,论文用 MD5 只是惯例;SHA-256 避免弱哈希碰撞隐患 |
| 6 | WBI 签名用 MD5 | B 站接口**协议强制**,换 SHA-256 会被服务端直接 -403 拒绝;不是安全原语用途 |
| 7 | 队列里的 URL **不脱敏** | 它是后续请求的凭据,抹掉签名参数会让续爬 403;产物层才脱敏 |
| 8 | 网络守卫按 Context 创建时的域绑定白名单 | 跨域时 Context 销毁重建,守卫随之更新,不会"锁死在第一个域" |
| 9 | 四家平台委托 yt-dlp 而非自研签名 | 平台一次改版即失效;通用通道是更稳的长期路径 |
| 10 | `opencv-python-headless` 偏离工程书钉死版本 | 4.12.0.88 与 numpy 2.5 冲突,装不上;改 5.0.0.93 并如实留档 |

---

## 11. 复核命令速查

```bash
cd mei-lab_orchestral_crawler

# —— 规模与数字 ——
find meilab -name "*.py" ! -name "__init__.py" | wc -l     # 包内模块数
wc -l $(find meilab -name "*.py") | tail -1                 # 包内代码行
wc -l $(find tests -name "*.py") | tail -1                   # 测试代码行
grep -rc "^\s*def test_" tests/*.py | awk -F: '{s+=$2} END {print s}'   # 测试函数数
python tools/doc_numbers.py --check                          # 文档数字是否过期(非 0 = 过期)

# —— 质量 ——
./.venv/Scripts/python.exe -m pytest tests/ -v
./.venv/Scripts/python.exe tools/release_check.py

# —— 冒烟 ——
mel crawl --url https://quotes.toscrape.com/ --max 5
mel crawl --url https://quotes.toscrape.com/js/ --max 3
./.venv/Scripts/python.exe fingerprint_probe.py
mel stats
mel export --format markdown --output ./data
mel resolve --url https://www.bilibili.com/video/BV1Dvpg6BEt5/
mel rule validate rules/sites/bilibili-video.yaml

# —— 缺失能力核验(应全部无输出)——
for kw in "Retry-After" crawl_delay sanitiz SimHash checkpoint lease proxy sitemap ETag; do
  grep -rIl "$kw" meilab tests
done
```

> 最后一条用于核验"对比报告 D 列表里的缺失项是否已补齐";若某项有了实现,说明该缺口已关闭,
> 记得同步更新第 9 节与对比报告。

---

*台账完*
