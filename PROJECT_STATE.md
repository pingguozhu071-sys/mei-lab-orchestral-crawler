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
| 包内模块数 | 33 |
| 包内代码行 | 11119 |
| 测试文件数 | 11 |
| 测试代码行 | 1905 |
| 测试函数数 | 140 |
| 站点规则数 | 8 |

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
| 收尾阶段 | DASH 合流、评论采集修复、站点规则扩面 + `key` 取值 schema | 见 §3.8 |
| 提速轮 | 并行 Range 下载、镜像测速;少数派/36kr 规则 | 见 G-20 / §3.9 |

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

### 3.6 真实样本验证(2026-10-31,详见经验记录 G-17 / G-18)

样本来源:独立采集流程(简书 robots.txt、浙江美术馆 gb2312、
建築研究所 Shift_JIS、四平台公开分享链接)。修复前留缺陷回执,修复后复抓对照。

| # | 验证项 | 结果 |
|---|---|---|
| 1 | 编码:gb2312 / Shift_JIS 页面 | 缺陷坐实(修复前 U+FFFD 243/10091)→ 修复 → 复抓 **U+FFFD=0**,标题正确(浙江美术馆 / 国立研究開発法人 建築研究所) |
| 2 | robots `Crawl-delay` | 缺陷坐实(Googlebot 应 10.0 实得 0.0)→ 修复 → **10.0**;未点名 UA 为 0.0(* 组无声明,正确) |
| 3 | 四平台嗅探 | 抖音/小红书/快手/网易云全部正确映射到 media 通道与对应平台 |
| 4 | 四平台媒体解析(委托 yt-dlp) | 网易云 **OK**(《再见莫妮卡》);抖音/小红书/快手明确失败并报出原因(签名/客户端渲染),未静默 |
| 5 | gzip | 样本报告怀疑的 gzip 问题在本框架**不存在**(curl_cffi 透明解压) |

样本未覆盖(维持开口):JS 挑战页、真实 AES-128 HLS(公开样本未找到,
后者可用本地构造继续)、提取规则扩面(见 `rules/PENDING.md`)。

### 3.7 登录态 cookie 验证(2026-10-31,用户提供 cookie 文件)

cookie 内容不打印、不入库;以下只记结论。

| # | 验证项 | 结果 |
|---|---|---|
| 1 | cookie 有效性 | `x/web-interface/nav` 返回 `isLogin: True`,SESSDATA 有效 |
| 2 | 评论接口(登录态) | 仍 `-403`;加 `dm_img_*` 风控参数仍 `-403` ⇒ 需要浏览器渲染上下文,G-10 关账为平台限制,评论采集改走浏览器通道渲染(工作项) |
| 3 | cookie 转发修复 | `bilibili_resolve` 原先收了 cookies 参数但未传给 API,已接通 |
| 4 | 清晰度 | 匿名与登录态在 `fnval=1` 下都是 720P(格式上限);DASH(`fnval=4048`)+ 登录态实测 `accept_quality=[116,80,64,32,16]`(1080P60),DASH 支持为媒体通道工作项 |

### 3.8 DASH / 评论 / 规则收尾(2026-10-31,详见经验记录 G-19)

| # | 验证项 | 结果 |
|---|---|---|
| 1 | DASH 解析 | `fnval=4048` 返回 `dash.video` 15 条 + `dash.audio` 3 条;选轨取最高档、同档 H.264 优先 |
| 2 | DASH 下载 + 合流 | 实抓 BV1Dvpg6BEt5 全片:1,250,182,765 字节 / 207 秒;ffprobe 复核 **h264 1920×1080 @60fps + aac 48kHz 立体声**,时长 6971.67s 与源一致 |
| 3 | CDN 凭证边界 | DASH 直链在 `bilivideo.com`,与 cookie 域不同——下载只带 Referer 不带 cookie,不跨域泄漏凭证 |
| 4 | 评论采集(端点纠正) | G-10 的真因是**端点选错**:`reply/wbi/main` 恒定 -403(平台限制);`/x/v2/reply` + `pn` 翻页正常(48 条评论 2 页取完、页间不重复、楼中楼内嵌)。浏览器通道与登录态都不是必要条件 |
| 5 | 评论翻页实测 | 带 cookie:p1=20 条 + p2=16 条,共 48 条;匿名只有 3 条/页(登录态影响条数而非可访问性) |
| 6 | 规则扩面 | 新增 netease-song / douban-book / thepaper-news 三条,全部 `rule test` 实测通过(晴天/周杰伦/叶惠美;小王子/作者/译者/出版社/ISBN/9.1;澎湃 og 三字段) |
| 7 | `key` 取值 schema | 豆瓣 `#info` 键值对容器可按 key 拆字段;meta 纯文本 content 不再被 urljoin 污染 |
| 8 | 环境漂移 | 仓库 `.venv` 是唯一正确环境;系统 Python 缺 `markdownify`,会让 markdown 转换静默退化为正则剥离(G-19) |

### 3.9 下载提速与 SPA 规则(2026-10-31,详见经验记录 G-20)

| # | 验证项 | 结果 |
|---|---|---|
| 1 | 并行分块下载 | 8 连接 Range 分块,聚合实测 ~75-90 MB/s;1.07GB 视频轨分块下载 12.4s + 合并 3.6s |
| 2 | 镜像测速 | 候选直链先各测 2MB 再排序:B 站音频主链(mcdn 节点)实测连接超时,测速把好镜像排到前面 |
| 3 | 并行路径触发修复 | Range 头此前只在续传时携带 → 全新下载拿到 200,并行分支恒不触发(G-20);无条件携带后实测 1.25GB 全片 **207s → 41s**,字节与首轮完全一致(1,250,182,765) |
| 4 | faststart 移除 | `-movflags +faststart` 的整文件第二遍重写(~7s)对本地播放无收益,去掉;moov 在尾只影响边下边播 |
| 5 | 分阶段打点 | resolve 0.4s / 测速 0.9s / 视频分块 12.4s / 合并 3.6s / 音频 2.3s / 合流 ~4s |
| 6 | 少数派规则 | **SSR 确认**(此前误判 SPA,系样例 URL 无效);`rule test` 实测通过(教育的下一步 · 其二) |
| 7 | 36kr 规则 | **真 SPA 确认**(HTTP 25KB 壳);规则按渲染后 DOM 写,浏览器渲染 HTML 直接喂 `rule.extract` 五字段全中 |
| 8 | 豆瓣电影 | 条目页当天被限流(2950 字节拦截壳),待冷却(登记于 PENDING.md) |

## 4. 已知限制

| # | 限制 | 性质 |
|---|---|---|
| 1 | Patchright 下指纹注入只发生在加载之后 | 引擎能力约束,已探测并降级 |
| 2 | B 站评论只能走 `/x/v2/reply`(pn 翻页);`reply/wbi/main` 恒定 -403,匿名单页只有 3 条 | 平台限制;已按可用端点实现(见 §3.8) |
| 3 | 四个平台的直链委托 yt-dlp,实测仅网易云解析成功,抖音/小红书/快手明确失败 | 直链签名与客户端渲染,失败路径如实报错(见 §3.6) |
| 3b | DASH 合流依赖机器上有 ffmpeg | 缺 ffmpeg 时明确报错并保留分轨,不冒充成功(见 §3.8);moov 在文件尾,不支持边下边播 |
| 4 | 无 GUI、无安装包 | 形态选择 |
| 5 | 七文件超 300 行 | 已冻结,扩展进新模块 |
| 6 | 站点规则覆盖 8 个站点 | 机制完成;36kr 规则只在浏览器通道下生效(HTTP 通道是壳),豆瓣电影待限流冷却(见 `rules/PENDING.md`) |
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
# 复核一律走项目 .venv(系统 Python 缺 markdownify 会让 md 转换静默退化,见 G-19)
.venv/Scripts/python.exe -m pytest tests/ -q   # 全部通过
.venv/Scripts/python.exe tools/release_check.py         # 6 项
.venv/Scripts/python.exe tools/doc_numbers.py --check   # 文档数字是否过期

mel crawl --url https://quotes.toscrape.com/js/ --max 3   # 应出现 browser 通道
python fingerprint_probe.py                               # 应 0 标红
mel resolve --url https://www.bilibili.com/video/BV1Dvpg6BEt5/   # method=bilibili_dash,1080P60
mel comments --url https://www.bilibili.com/video/BV1Dvpg6BEt5/ --page 2   # pn 翻页,带 cookie 每页 20 条
```
