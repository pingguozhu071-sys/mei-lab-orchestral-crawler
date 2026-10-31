# 项目状态

工程台账:交付进度、环境回执、已知限制、决策记录。

- 使用说明见 [README](README.zh-CN.md)
- 结构与机制见 [架构](docs/ARCHITECTURE.md)
- 踩坑记录见 [工程经验记录](docs/ENGINEERING-LESSONS.md)

本文件里的数字由 `tools/doc_numbers.py` 生成,`--check` 校验是否过期。

---

## 1. 当前状态

| 项 | 值 |
|---|---|
| 版本 | 0.1.0 |
| 交付 | 15 个计划批次 + 5 个扩展阶段,全部完成 |
| 测试 | 178 个用例通过 |
| 发版门禁 | 6 项通过 |

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

## 2. 交付批次

### 2.1 基础框架

| 批次 | 内容 | 验证 |
|---|---|---|
| 1 | 打包配置、数据模型(Task / Episode / DeviceProfile / RoundState) | 模型约束被后续用例覆盖 |
| 2 | SQLite 队列:幂等入队、并发领取、状态机 | 9 项,含 4 进程并发领取不重不漏 |
| 3 | URL 嗅探:后缀、路径、查询参数 | 15 项 |
| 4 | 分层路由:任务类型与嗅探结果 → Worker | 9 项 |
| 5 | HTTP Worker:curl_cffi、限速、重试、robots | 真实抓取 + SSRF 实测 |
| 6 | 浏览器 Worker:独立 Chromium、context 复用策略 | 真实渲染(静态站 + JS 站) |
| 7 | Worker 池:分发、结果回收、崩溃重启 | 真实进程:kill 后自动重启 |
| 8 | 设备档案:21 台自洽虚拟设备 | 13 项,遍历全库一致性 |
| 9 | 指纹注入 + 网络守卫 | 真实浏览器冒烟 |
| 10 | 行为模拟:打字节奏、停留时长、鼠标轨迹 | 统计性验证(分布形状) |
| 11 | HTML 提取:密度启发、去噪、三层 residual | 12 项 |
| 12 | JSON 提取:API / JSON-LD / `__NEXT_DATA__` / `__NUXT__` | 四条路径实跑 |
| 13 | 提取管线 + PDF(可选依赖) | 真实 PDF 提取 |
| 14 | 内容仓库:增量、加密;五种导出 | 增量 + AES + 五格式实跑 |
| 15 | 主循环 + CLI | 端到端抓取/续传/统计/导出 |

### 2.2 扩展阶段

| 阶段 | 内容 | 关键验证 |
|---|---|---|
| 交互窗口 | `mel shell`:粘贴即抓,常驻 Worker 池 | 单条 2–4 秒 |
| B 站实抓修复 | 验证码误报、URL 优先、`--max` 超发、结构化字段入库 | 见经验记录 G-07 / G-11 |
| 阶段 0 | 媒体契约层:`MediaItem`、媒体表、`Channel.MEDIA` | 媒体记录读写、schema 迁移 |
| 阶段 1 | 媒体通道:通用 yt-dlp、m3u8/AES-128、五平台、cookie | 见 §3.3 |
| 阶段 2 | 租约 + CAS、`Retry-After`、每域限速、SSRF 加固、验证码分支 | 见 §3.4 |
| 阶段 3 | 三层脱敏、SimHash、质量闸、条件缓存、RSS 播种、YAML 规则、SQLite 导出 | 脱敏边界 + 近重复拦截 |
| 阶段 4 | 可观测产物、6 项发版门禁、文档数字同源 | 门禁全绿 |
| 阶段 5 | 升级预算修复、数字同源写入、端到端重跑 | 见 §3.2 |

## 3. 实测回执

### 3.1 环境

```
Python 3.14.6 · pip 26.1.2
patchright 1.61.2 · playwright 1.62.0 · curl_cffi 0.16.0
pydantic 2.14.2 · lxml 6.1.1 · trafilatura 2.2.0 · orjson 3.11.9
opencv-python-headless 5.0.0.93 · numpy 2.5.2 · ddddocr 1.6.1
pycryptodome 3.23.0 · yt-dlp 2026.7.4 · pymupdf 2.0 · PyYAML 6.0.3
deno 2.9.8 · chromium revision 1228
```

TLS 指纹:

```
ja3_hash = 35c61a611436bd206b042c36810d36ad
ja3 = 771,4865-4866-4867-49195-49199-49196-49200-52393-52392-60-61
```

### 3.2 端到端

```
mel crawl --url https://quotes.toscrape.com/ --max 5
  queued 21 · dispatched 5 · succeeded 5 · failed 0 · 22.5 s

mel crawl --url https://quotes.toscrape.com/js/ --max 3
  channels: http 3 · browser 3
  /js/ page: http 0 chars → escalate → browser 1,429 chars

fingerprint_probe.py        10 checks · 0 flagged
mel stats                   lightweight 60% / browser 40%
mel export (5 formats)      jsonl 1 · csv 1 · markdown 4 · xlsx 1 · sqlite 1
mel resume --max 10         succeeded 10 · failed 0 · 20.16 s
mel rule validate / test    规则通过;真实页抽出 4 个字段
```

### 3.3 媒体通道

```
ytdlp resolve      ok · 27 formats
ytdlp test-download ok · 6,163,473 bytes · partial: []
hls (5 segments)   ok · 7,717,212 bytes
hls resume         ok · resumed: true · 4,624,988 bytes
bilibili resolve   native API · 720P · 453,914,132 bytes · 6,972 s
bilibili range     206 · 1,024 bytes · 0-1023/453914132
```

### 3.4 限流与租约

```
retry-after "120"        120.0
retry-after HTTP date    89.9
per-domain cap (2)       dispatched 2 · inflight {same.example.com: 2}
lease reclaim            1 → pending 1 · running 0
```

### 3.5 发版门禁

```
[OK] 版本一致性 · [OK] 工作区干净 · [OK] 全量测试
[OK] ruff 基线 · [OK] 依赖漏洞 · [OK] 密钥泄漏
6 项 · 通过 6 · 失败 0 · 跳过 0
```

## 4. 已知限制

| # | 限制 | 性质 |
|---|---|---|
| 1 | Patchright 下指纹注入只发生在加载之后 | 引擎能力约束,已探测并降级 |
| 2 | B 站评论匿名不可读 | 平台限制,需自备 cookie |
| 3 | 四个平台的直链委托 yt-dlp | 减少自研签名维护 |
| 4 | 无 GUI、无安装包 | 形态选择 |
| 5 | 七文件超 300 行 | 已冻结,扩展进新模块 |
| 6 | 站点规则覆盖 3 个站点 | 机制完成,资产待补(见 `rules/PENDING.md`) |
| 7 | 覆盖率只报告不设阈值 | 事故回归优先 |
| 8 | 部分站点在本网络不可达 | 环境限制,冒烟改用可达站点 |

## 5. 决策记录

| # | 决策 | 理由 |
|---|---|---|
| 1 | 多进程而非 asyncio | 浏览器崩溃隔离、阻塞调用不需绕事件循环 |
| 2 | 队列用 `Manager().Queue()` 代理 | Windows spawn 下裸队列作为 Process 属性不可靠 |
| 3 | 设备 locale/时区由地区推导 | 按构造保证一致,比逐条校验可靠 |
| 4 | 行为模拟用 `random.Random` + `secrets` 种子 | 非密码学用途,但需可复现 |
| 5 | SimHash 词哈希用 SHA-256 截断 | 算法只要求均匀分布,避开弱哈希 |
| 6 | WBI 签名用 MD5 | 平台协议强制,换算法即 403 |
| 7 | 队列 URL 不脱敏 | 携带下次请求签名,抹掉即失效 |
| 8 | 平台解析器不做唯一路径 | 签名易失效,通用通道兜底 |
| 9 | 质量闸拒收而非降级入库 | 错数据会污染后续统计与去重 |
| 10 | `Episode.rounds_runned` 保留原拼写 | 不改既有数据契约,仅在 docstring 标注 |

## 6. 复核命令

```bash
python -m pytest tests/ -v            # 178 passed
python tools/release_check.py         # 6 项
python tools/doc_numbers.py --check   # 文档数字是否过期

mel crawl --url https://quotes.toscrape.com/js/ --max 3   # 应出现 browser 通道
python fingerprint_probe.py                               # 应 0 标红
mel resolve --url https://www.bilibili.com/video/BV1Dvpg6BEt5/
```
