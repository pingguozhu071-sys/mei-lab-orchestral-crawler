# mei-lab-orchestral-crawler

版本:0.1.0

分层调度 + 多格式提取 + 反检测的 Python 爬虫框架。
核心原则:**最快路径优先**——能用一次 HTTP 请求解决的,绝不开浏览器。

```
                            ┌───────────────────────────────┐
   seed URL ──────────────▶ │  Queue (SQLite WAL, 幂等入队) │
                            │  pending → running → done/failed│
                            └───────────────┬───────────────┘
                                            │ claim
                                            ▼
                            ┌───────────────────────────────┐
                            │        LayeredRouter          │
                            │  嗅探(静态启发,不联网) → 分层  │
                            └───┬───────────────────────┬───┘
                    content 层  │                       │  cost 层
                ┌───────────────┴────────┐   ┌──────────┴──────────┐
                ▼                        ▼   ▼                     ▼
        ┌───────────────┐        ┌───────────────┐     ┌────────────────────┐
        │  HttpWorker   │        │ BrowserWorker │     │  ExtractionPipeline│
        │ curl_cffi     │        │ 独立 Chromium  │     │ HTML / JSON / PDF  │
        │ TLS 指纹模拟   │        │ 设备档案+注入   │     │ 密度启发 + JSON-LD  │
        │ ≥1.5s 限速     │        │ 网络守卫       │     │ 行为模拟           │
        └───────┬───────┘        └───────┬───────┘     └─────────┬──────────┘
                │                        │                       │
                └────────────┬───────────┴───────────────────────┘
                             ▼
                  ┌──────────────────────┐      ┌────────────────────┐
                  │ Vault (SQLite)       │─────▶│ Export             │
                  │ SHA256 增量 + AES-GCM │      │ JSONL/CSV/MD/XLSX  │
                  └──────────────────────┘      └────────────────────┘
```

## 快速开始

```bash
# 1) 安装(建议用虚拟环境)
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"     # Windows
# pip install -e ".[dev]"                                  # Linux / macOS

# 2) 安装浏览器(Patchright 的隐身版 Chromium)
./.venv/Scripts/patchright.exe install chromium          # Windows
# patchright install chromium                             # Linux / macOS

# 3) 可选:PDF 支持
./.venv/Scripts/python.exe -m pip install -e ".[pdf]"

# 4) 抓取
mel crawl --url https://quotes.toscrape.com/ --max 20

# 4b) 交互窗口:粘贴链接按 Enter 直接爬(推荐日常使用)
mel shell

# 5) 查看统计 / 导出
mel stats
mel export --format markdown --output ./data

# 6) 指纹自检(等同 bot.sannysoft.com 的本地等价物)
python fingerprint_probe.py
```

命令一览:

| 命令 | 作用 |
|---|---|
| `mel shell` | **交互窗口**:粘贴链接按 Enter 直接爬(见下) |
| `mel crawl --url URL [--url URL2] [--max N]` | 抓取(默认遵守 robots.txt,全局限速 ≥1.5s) |
| `mel resume [--max N]` | 断点续传:继续处理队列里未完成的任务 |
| `mel stats` | 任务 / Episode / 仓库统计,含轻量 vs 浏览器通道占比 |
| `mel export --format markdown\|jsonl\|csv\|xlsx --output DIR` | 导出(按域名分目录) |

### 交互窗口(`mel shell`)

```
$ mel shell
mei-lab-orchestral-crawler 交互窗口
粘贴一条链接按 Enter 即可抓取;输入 :help 看命令,:quit 退出。

url> 抓取中… https://quotes.toscrape.com/
✓ http 通道 | Quotes to Scrape | 2533 字符 | 2.6s
url> https://quotes.toscrape.com/js/
✓ browser 通道 | Quotes to Scrape | 1429 字符 | 4.1s
url> :quit
会话结束。
```

* 粘贴即抓:整段复制来的文本会自动挑出其中的 URL(去引号、去句末标点)。
* 常驻 Worker 池:连续粘贴多条链接不必反复起进程,单条约 2~4 秒。
* 自动分层:轻量通道抓到空壳页会自动升级到浏览器通道再抓一次。
* 内置命令:`:help` `:stats` `:latest` `:export [jsonl|csv|markdown|xlsx]` `:quit`。
* 输入的 URL 会先做安全校验(仅 http/https,拒绝 localhost/环回/私网/保留地址),
  不通过会直接给出原因并跳过。

## 目录结构

### 工程规模(由 `tools/doc_numbers.py` 生成,勿手改)

<!-- AUTO:NUMBERS -->
| 指标 | 数值 |
|---|---|
| 包内模块数 | 32 |
| 包内代码行 | 10093 |
| 测试文件数 | 10 |
| 测试代码行 | 1576 |
| 测试函数数 | 123 |
| 站点规则数 | 3 |
<!-- /AUTO:NUMBERS -->

校验命令(数字过期则返回非 0,可挂 CI):

```bash
python tools/doc_numbers.py --check
```

```
meilab/
├── core/     types.py(Pydantic 模型) queue.py(SQLite 队列+状态机)
├── router/   sniff.py(静态嗅探) layer.py(分层路由)
├── workers/  base.py(进程骨架+SSRF 校验) http.py(curl_cffi)
│             browser.py(独立 Chromium) pool.py(进程池/崩溃重启)
├── extract/  html.py(密度启发) jsondata.py(JSON/JSON-LD/hydration)
│             pdf.py(可选 pymupdf) pipeline.py(编排)
├── stealth/  profiles.py(21 台设备档案) inject.py(指纹注入)
│             behavior.py(行为模拟) netguard.py(网络守卫)
├── storage/  vault.py(SQLite+SHA256 增量+AES-GCM) export.py(四种导出)
├── engine.py 主循环编排   cli.py 命令行
```

## 环境依赖

以下版本为本机 `pip show` 实测结果(可用 `pip show <包>` 复核):

| 组件 | 版本 | 用途 |
|---|---|---|
| curl_cffi | 0.16.0 | 轻量通道 TLS/JA3 指纹模拟 |
| patchright | 1.61.2 | 浏览器通道主引擎(隐身版) |
| playwright | 1.62.0 | patchright 缺失时的回退引擎 |
| trafilatura | 2.2.0 | 正文提取兜底(密度算法失效时) |
| lxml | 6.1.1 | BS4 解析后端 |
| orjson | 3.11.9 | 高速 JSON 序列化 |
| openpyxl | 3.1.5 | xlsx 导出 |
| ddddocr | 1.6.1 | 本地 OCR(验证码识别) |
| opencv-python-headless | 5.0.0.93 | 滑块缺口检测 |
| numpy | 2.5.2 | OpenCV 数值底座 |
| pycryptodome | 3.23.0 | m3u8 AES-128 分片解密 |
| yt-dlp | 2026.7.4 | 视频通道外部引擎 |
| pydantic | 2.14.2 | 数据模型 |
| Deno | 2.9.8 | yt-dlp 的 EJS 挑战 JS 运行时 |

> `opencv-python-headless` 工程书原定 `4.12.0.88`,但该版本要求 `numpy<2.3.0`,
> 与钉死的 `numpy==2.5.2` 冲突,实测不可安装;已改用同样支持 numpy 2.5 的
> `5.0.0.93`。

## 已知限制(均为本机实测结论,不是推测)

1. **Patchright 1.61.2 不执行任何脚本注入**。实测 `context.add_init_script` 与
   `page.add_init_script` 均不生效,`route.fetch()+fulfill()` 改写 HTML 也不生效;
   官方 Playwright 同样代码正常。框架因此在启动时**实测探测**引擎能力
   (`BrowserWorker._probe_init_script`),不支持时自动降级为"加载后加固"
   (`harden_page`),功能不缺,只是注入时机从"文档开始"变成"加载之后"。
2. **Windows 中文路径下 libcurl 无法加载 CA 证书**:
   `curl: (77) error adding trust anchors`。框架启动时把 certifi 的 CA bundle
   复制到纯 ASCII 路径并设置 `CURL_CA_BUNDLE`(`workers/http.py::ensure_ascii_ca_bundle`)。
3. **Windows + spawn 不允许把同步原语放在 Process 子类实例属性上**
   (队列/Event),否则子进程报 `DuplicateHandle [WinError 5]`。框架改用
   `Manager().Queue()` 代理队列 + `STOP` 哨兵,并在 `__getstate__` 里剔除
   不可 pickle 的运行时对象。
4. **PDF 是可选能力**:未安装 `pymupdf` 时 PDF 任务会以"不支持 PDF"入失败队列,
   其余通道不受影响(降级 ≠ 失败)。
5. **Google Chrome 未安装**:机器上已有 Chrome,Patchright 拒绝覆盖安装,
   浏览器通道默认使用 Patchright Chromium。
6. **网络可达性**:本机所在网络对 `news.ycombinator.com`、`bot.sannysoft.com`
   连接超时(TCP 层不可达),冒烟改用可达站点;这属于网络环境问题,不是框架缺陷。
   指纹检测改由仓库根目录的 `fingerprint_probe.py` 承担(同款链路、本地等价检测)。

## 伦理边界

* **默认遵守 robots.txt**:轻量通道启动时拉取并解析目标站点的 `robots.txt`,
  被 `Disallow` 的 URL 直接跳过(可用 `--ignore-robots` 关闭,但请不要滥用)。
* **全局限速 ≥1.5 秒 + 随机抖动**,每个 Worker 进程独立计时。
* **只采集公开页面**:不触碰登录态、不绕过验证码、不破付费墙。
* **SSRF 防护**:所有请求(含每一跳重定向)都要过协议白名单 + DNS 解析后 IP 段校验,
  环回/私网/链路本地/云元数据地址一律拒绝。
* **网络守卫**:浏览器通道拦截并掐断一切非目标域请求(埋点/广告/第三方字体)。
* 请把 `User-Agent` 改成你自己的联系方式,让站点管理员知道谁在访问。
