# 变更记录

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [0.1.0] — 2026-10-31

首个可用版本。从零构建,按六个阶段交付。

### 新增

**调度与队列**

- SQLite 队列(WAL):任务、Episode、媒体下载三张表,`PRAGMA user_version` 迁移
- 状态机 `pending → running → done/failed → dead_letter`,非法跃迁抛异常
- 幂等入队:`UNIQUE(url, task_type)`,重复入队只抬优先级
- 租约机制:`leased_at` / `lease_expires` / `worker_id`,默认 120 秒,心跳续租
- 比较交换打卡:只有当前租约持有者能把任务标记完成
- 过期租约自动回收,Worker 崩溃后任务不会永久卡住
- 断点续传:`mel resume` 从队列继续

**路由**

- 离线静态嗅探:URL 后缀、路径模式、查询参数、平台域名、媒体直链
- 两层通道决策:HTTP / 浏览器,加媒体通道
- 分层升级:空壳页(有脚本、正文近乎为空)自动改走浏览器通道重试一次,
  且不消耗 `--max` 预算
- 显式 URL 优先领取,不被队列里的遗留任务插队
- RSS / Atom / sitemap 播种(防 XML 实体扩展:拒绝 DTD/ENTITY,5 MiB 上限)

**抓取**

- HTTP 通道:curl_cffi 浏览器级 TLS 指纹,urllib 回退
- 浏览器通道:每个 Worker 独立 Chromium,独立 context,跨域销毁
- 四重限速:最小间隔 1.5 秒、随机抖动、robots `Crawl-delay`、域名冷却
- `Retry-After` 解析(秒数与 HTTP 日期),上限 300 秒,429 退避不消耗主重试次数
- 每域并发上限(默认 20),Worker 崩溃时自动释放名额
- 逐跳 SSRF 闸:协议白名单、主机名规范化、数字字面量还原、DNS 后地址判定
  (缓存 90 秒)、最多 10 跳重定向
- 手动重定向,每次跳转重新校验

**提取**

- HTML:文本密度启发式 + 去噪 + 连续区块扩展,trafilatura 兜底
- 三层 residual:原始 HTML / 清洗后 DOM / Markdown
- JSON API、JSON-LD、`__NEXT_DATA__`、`window.__NUXT__`
- PDF(pymupdf,可选依赖,缺失时明确降级)
- 声明式站点规则:YAML + `mel rule validate` / `mel rule test`;
  字段支持 `key` 键值对取值(豆瓣 `#info` 类容器);meta 纯文本
  content 不被误拼为 URL
- 内置规则 6 条:B 站视频、网易云歌曲、豆瓣书籍、澎湃新闻、知乎问答、起手模板,
  前四条经真实页面 `rule test` 验证
- 质量闸:正文长度、链接密度、实词数三项判据

**媒体**

- 通用 yt-dlp 通道:子进程调用,`--continue` 续传,产物用目录快照差集认定
- m3u8:标准库解析,支持 `EXT-X-KEY`(AES-128)、`EXT-X-MAP`,分片级续传
- 直链下载:Range 续传、`.part`、原子改名、字节校验
- DASH 分轨下载 + ffmpeg 合流(`fnval=4048`):B 站解锁 1080P60,
  缺 ffmpeg 时明确报错并保留分轨;CDN 请求只带 Referer 不跨域带 cookie
- Bilibili 原生解析(`view` → `playurl`,DASH 优先、mp4 durl 兜底)+ WBI 签名
- 评论采集:`/x/v2/reply` + `pn` 翻页 + 楼中楼内嵌(实测 48 条 2 页取完)
- 抖音 / 小红书 / 快手 / 网易云委托通用通道
- 自备 cookie:文件校验、Fernet 加密存储、Netscape → Cookie 头

**反检测**

- 21 台自洽虚拟设备(Win / macOS / Linux / Android / iOS),locale 与时区按构造推导
- 指纹注入:webdriver、cdc_ 残迹、chrome.runtime、platform、languages、
  hardwareConcurrency、maxTouchPoints、插件列表、WebGL 未掩码值、`toString` 归一
- 运行时能力探测:引擎不支持 `add_init_script` 时降级为加载后加固
- 行为模拟:泊松打字节奏、对数正态停留、贝塞尔轨迹(人工样本 7:3 混合)
- 网络守卫:Context 级拦截,只放行目标域
- 验证码:ddddocr OCR + cv2 滑块缺口;识别成功回填重试一次

**存储与导出**

- 内容仓库:以 URL 为主键,SHA-256 增量(同 URL 内容未变只累加见到次数)
- 可选 AES-256-GCM 加密(`MELAB_KEY`)
- SimHash 64 位近重复检测(词哈希用 SHA-256 截断)
- 五种导出:JSONL / CSV / Markdown / XLSX / SQLite(FTS5 全文索引)
- 导出前递归脱敏

**安全**

- 三层脱敏:日志 handler、CLI/异常旁路、导出产物
- 19 类敏感 URL 参数、手机号、邮箱、IPv4
- 队列 URL 明确不脱敏(携带下次请求的签名)

**可观测**

- `stats.jsonl`(逐事件)、`checkpoint.json`(进度快照)、
  `deadletter.jsonl`(批量落库失败兜底)
- `mel stats` 输出任务/Episode/仓库/媒体统计与通道占比

**接口**

- CLI:`crawl` `resume` `stats` `export` `shell` `media` `resolve` `comments`
  `rule validate` `rule test`
- 交互窗口 `mel shell`:粘贴链接按回车即抓,常驻 Worker 池
- 可嵌入:`Engine` / `EngineConfig`

**工程化**

- 发版门禁 6 项:版本一致性、工作区干净、全量测试、ruff 基线(只降不升)、
  依赖漏洞、密钥泄漏
- 文档数字同源:`tools/doc_numbers.py --check` / `--write`
- 测试 178 个用例,全部离线

### 已知问题

- Patchright 1.61.2 不执行 `add_init_script`,指纹注入只能发生在页面加载之后
- B 站评论只能走 `/x/v2/reply`(`reply/wbi/main` 恒定 -403);匿名单页仅 3 条,带 cookie 20 条
- DASH 合流依赖机器上有 ffmpeg;缺失时保留分轨并明确报错
- 七个文件超过 300 行,已冻结
- 覆盖率只报告,不设阈值
- 站点规则覆盖 6 个站点;36kr / 少数派为纯 SPA,需浏览器通道才能扩规则

详见 [技术白皮书 · 已知架构债](docs/WHITEPAPER.md#5-已知架构债)
与 [工程经验记录](docs/ENGINEERING-LESSONS.md)。

---

## 版本说明

`0.1.0` 是首个版本,API 尚未稳定。`0.x` 期间不保证向后兼容;
队列库与仓库库的 schema 变更会通过 `PRAGMA user_version` 迁移,
但字段语义调整可能不提供迁移路径。
