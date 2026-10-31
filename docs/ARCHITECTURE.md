# 架构

本文件描述 mei-lab-orchestral-crawler 的分层结构、单页处理顺序、各模块职责、
数据库表结构与并发/限流/安全机制。

- 设计取舍与问题定义见 [技术白皮书](WHITEPAPER.md)
- 环境与构建见 [构建与运行](BUILD.md)
- 踩过的坑见 [工程经验记录](ENGINEERING-LESSONS.md)

---

## 1. 分层

```
┌──────────────────────────────────────────────────────────────┐
│ 入口层    cli.py(click)· engine.py(编排)                     │
├──────────────────────────────────────────────────────────────┤
│ 调度层    router/sniff.py(离线嗅探) · router/layer.py(路由)   │
│           core/queue.py(SQLite 队列,租约,CAS)                │
├──────────────────────────────────────────────────────────────┤
│ 执行层    workers/pool.py(进程池)                             │
│           workers/http.py · workers/browser.py               │
│           media/downloader.py(媒体在父进程内完成)             │
├──────────────────────────────────────────────────────────────┤
│ 处理层    extract/pipeline.py → html / jsondata / pdf         │
│           extract/site_rules.py(声明式规则)                  │
│           storage/quality.py(SimHash + 质量闸)               │
├──────────────────────────────────────────────────────────────┤
│ 支撑层    stealth/*(档案,注入,行为,守卫,验证码)              │
│           privacy/sanitizer.py(三层脱敏)                     │
│           storage/vault.py · export.py · observability.py    │
│           workers/base.py(SSRF 原语)· media/cookies.py       │
└──────────────────────────────────────────────────────────────┘
```

依赖方向自上而下,不存在反向 import。`workers/base.py` 里的 SSRF 原语被
`media/`、`router/feed.py`、`extract/` 共用,是全仓扇入最高的模块。

## 2. 单页处理顺序

`engine.crawl()` 的主循环每一步都可对应到代码位置。

| # | 步骤 | 位置 | 说明 |
|---|---|---|---|
| 1 | 入队 | `queue.enqueue()` | `UNIQUE(url, task_type)` 保证幂等;重复入队只抬优先级 |
| 2 | 重置显式 URL | `engine._reset_urls()` | 用户点名的 URL 允许重跑(之前抓过也再抓) |
| 3 | 回收僵死任务 | `queue.reclaim_running()` / `reclaim_expired()` | 启动时全量回收;运行中按租约过期回收 |
| 4 | 领取 | `queue.claim()` / `claim_by_urls()` | 单条 `UPDATE … RETURNING` 内完成「选中 + 置 running + 写租约」 |
| 5 | 路由 | `router.route(task, sniff(task.url))` | 带 `payload.escalate=browser` 的任务跳过嗅探直接进浏览器通道 |
| 6 | 派发 | `pool.dispatch(task, decision)` | 同域在途数受 `max_per_domain` 约束;媒体任务不进池 |
| 7 | 执行 | `HttpWorker` / `BrowserWorker` | robots → SSRF → 限速 → 请求(重定向逐跳复检) |
| 8 | 收结果 | `pool.drain()` → `pool.release()` | 释放域名在途名额 |
| 9 | 验证码分支 | `engine._try_solve_captcha()` | 命中验证码且识别成功 → 回队重试一次 |
| 10 | 提取 | `pipeline.extract(body, content_type)` | 见 §6 |
| 11 | 站点规则 | `match_rule()` → `rule.extract()` | 命中则覆盖通用启发式的结果 |
| 12 | 质量闸 | `assess_quality()` | 低质量拒收;空壳页触发升级而不是直接判失败 |
| 13 | 落库 | `vault.store()` | SHA-256 增量:同 URL 内容未变只累加 `seen_count` |
| 14 | 记 Episode | `queue.save_episode()` | 无论成败都记,便于统计与排障 |
| 15 | 打卡 | `queue.mark_done_checked()` | CAS:只有当前租约持有者能标记完成 |
| 16 | 出链发现 | `engine._discover_links()` | 同域、受深度与每页数量限制、跳过登录注册类路径 |
| 17 | 可观测 | `observability.stats.emit()` | 写 `stats.jsonl`,更新 `checkpoint.json` |

`_handle_result()` 返回一个布尔值,表示**该任务是否已进入终态**。
升级到浏览器通道时返回 `False`——那次尝试不计入 `--max` 预算,
任务会被重新领取。这一处细节决定了 `--max 1` 到底是「处理 1 个任务」还是
「尝试 1 次抓取」。

## 3. 队列

### 3.1 表结构

`queue.db`(WAL 模式,`PRAGMA user_version` 记录 schema 版本,当前 v2):

```sql
tasks (
  id TEXT PRIMARY KEY, url TEXT NOT NULL, task_type TEXT NOT NULL,
  priority INTEGER, status TEXT, retry_count INTEGER, depth INTEGER,
  parent_id TEXT, payload TEXT, worker_id TEXT,
  leased_at REAL, lease_expires REAL,        -- v2 新增
  created_at REAL, updated_at REAL,
  UNIQUE (url, task_type)
)
episodes (
  id TEXT PRIMARY KEY, task_id TEXT, url TEXT, outcome TEXT,
  rounds_runned INTEGER, anti_bot_signals TEXT, channel TEXT,
  http_status INTEGER, content_type TEXT, title TEXT,
  content_hash TEXT, content TEXT, raw_meta TEXT, error TEXT,
  started_at REAL, finished_at REAL
)
media_downloads (
  id TEXT PRIMARY KEY, task_id TEXT, source_url TEXT, platform TEXT,
  media_id TEXT, kind TEXT, title TEXT, status TEXT, method TEXT,
  file_path TEXT, file_size INTEGER, progress REAL, retry_count INTEGER,
  error TEXT, meta TEXT, created_at REAL, updated_at REAL,
  UNIQUE (source_url, kind)
)
```

迁移是幂等的:建表用 `IF NOT EXISTS`,加列先查 `pragma_table_info` 再 `ALTER`。
`SCHEMA_VERSION` 递增时在 `_migrate()` 里补一条固定语句——PRAGMA 不支持参数绑定,
所以这里刻意用字面量而不是字符串拼接。

### 3.2 状态机

```
pending ──► running ──► done
   │           │
   │           ├────► failed ──► pending(重试)
   │           │         │
   └───────────┴─────────┴────► dead_letter
```

`ALLOWED_TRANSITIONS` 显式列出合法跃迁,非法跃迁抛 `InvalidTransition`。
`pending → done` 这种跳过执行的跃迁会被拒绝。

### 3.3 租约与 CAS

三条 SQL 承担全部语义:

```sql
-- 领取:选中 + 置 running + 写租约,单条语句原子完成
UPDATE tasks SET status='running', worker_id=?, leased_at=?, lease_expires=?,
                 updated_at=?
WHERE id IN (SELECT id FROM tasks WHERE status='pending'
             ORDER BY priority DESC, created_at ASC LIMIT ?)
RETURNING …

-- 续租:长任务执行期间调用
UPDATE tasks SET lease_expires=?, updated_at=? WHERE id=? AND status='running'

-- CAS 打卡:租约时间戳必须仍是自己领到的那一个
UPDATE tasks SET status='done', leased_at=NULL, lease_expires=NULL, updated_at=?
WHERE id=? AND status='running' AND leased_at=?
```

`reclaim_expired()` 每一轮把 `lease_expires < now` 的 running 任务收回 pending。
默认租约 120 秒(`DEFAULT_LEASE_SECONDS`)。

这三个机制解决的是同一类问题:Worker 崩溃后任务不能永久卡在 running;
两个 Worker 同时处理完同一任务时,后完成的那个不能覆盖先完成的结果。

## 4. 并发模型

单机、单父进程、多个 Worker 子进程。

- 父进程只做编排:领取、派发、收结果、落库。**不执行任何抓取**。
- 子进程各自持有一个浏览器/一条 HTTP 连接池,互不共享。
- 队列用 `multiprocessing.Manager().Queue()` 代理,不用裸 `mp.Queue`——
  裸队列作为 `Process` 子类实例属性时,Windows spawn 下子进程反序列化会报
  `DuplicateHandle [WinError 5]`(真因是父进程 pickle 失败,见经验记录 G-03)。
- Worker 实例通过 `__getstate__` 剔除运行时对象(`_opener` / `_curl` / `_pw` /
  `_browser` / `_context`),保证 spawn 可序列化。
- 崩溃检测靠 `is_alive()` 轮询;槽位死亡时 `reap()` 重新拉起同类型进程,
  并释放该槽位在途任务占用的域名名额。

并发上限有三处,取最小值生效:

| 层级 | 参数 | 默认 |
|---|---|---|
| 全局在途 | `EngineConfig.max_inflight` | 8 |
| 每域在途 | `EngineConfig.max_per_domain` | 20 |
| Worker 数量 | `--http-workers` / `--browser-workers` | 2 / 1 |

## 5. 限流

限速在 `HttpWorker._throttle()` 一处收口,取四者最大值:

1. `delay`(下限强制 1.5 秒)
2. 随机抖动 `jitter`(用 `secrets.SystemRandom`,不是 `random`)
3. robots.txt 的 `Crawl-delay`
4. 该域名的冷却截止时间

`Retry-After` 解析支持秒数与 HTTP 日期两种格式,上限 300 秒。
收到 429 时:按 `Retry-After` 退避 → 给该域名设冷却 → 剩余重试次数内再试。
冷却期内的后续请求会自动延长等待,不需要调用方关心。

## 6. 提取管线

`ExtractionPipeline.extract()` 的分派顺序:

```
Content-Type 头 ──► json / html / pdf / text
        │
        └─ 头缺失或不可信 ──► 嗅探字节流
                              PDF 魔数 / JSON 首字符 / HTML 标签
```

各分支的产出统一为 `ExtractResult`:标题、正文、内容哈希、`meta` 字典。
`meta` 里保留诊断信息——密度值、命中的结构化数据类型、质量分、站点规则名。

### 6.1 HTML

文本密度用「子树文本长度 ÷ (1 + 后代标签数)」。这个定义是有代价换来的:
早期实现用「直接文本 ÷ 后代标签数」,结果 `<article><p>…</p></article>`
这类正文容器密度恒为 0,主区块永远选不中(见经验记录 G-08)。

流程:去噪(标签 + class/id 关键词)→ 找密度最高的块级容器 →
向前后扩展连续的高密度兄弟节点 → 转 Markdown。
正文短于 200 字符时回退 trafilatura。

三层 residual 同时保留:`raw_html`(原始)、`cleaned_dom`(去噪后结构)、
`markdown`(正文)。证据链不丢。

### 6.2 JSON

四种来源,按优先级:JSON-LD → `__NEXT_DATA__` → `window.__NUXT__` → 纯 API 响应。
`normalize_records()` 从任意嵌套结构里找「记录列表」(先按常见键名,
再深度优先找第一个字典元素列表)。

Nuxt 的解析不能用正则匹配大括号(嵌套 JSON 会被非贪婪匹配截断),
改用 `json.JSONDecoder().raw_decode()` 做括号感知解析;
`JSON.parse("…")` 形态另配一个 JS 字符串字面量读取器处理 `\"` 转义。

### 6.3 站点规则

规则文件在 `rules/sites/*.yaml`,结构:

```yaml
name: bilibili-video
match: [bilibili.com, b23.tv]
item_type: video
detail:
  fields:
    title:     {sel: "h1.video-title", text: true}
    published: {sel: ".pubdate", text: true, transform: date_parse}
  media:
    thumbnail: {sel: "meta[property='og:image']", attr: content}
```

字段三种写法:字符串(等价于取文本)、`{sel, text: true}`、`{sel, attr: name}`。
可选 `transform`:`date_parse` / `strip` / `int`。
`mel rule validate` 检查结构,`mel rule test` 用真实页面试跑。

规则命中时**覆盖**通用启发式的抽取结果;规则不命中不报错,退回启发式。

## 7. 媒体通道

三条路径,按可用性依次尝试:

```
.m3u8 直链 ──────────────────► 自研 HLS 下载器(AES-128 + 分片续传)
平台页面 ─► 平台解析器命中 ────► 自研直链下载器(Range 续传 + 原子改名)
        └► 未命中/失败 ──────► 通用 yt-dlp 通道
```

`MediaItem` 是统一出口,字段:`source / media_id / kind / platform / streams /
images / method / title / duration / needs_cookie / meta`。
下游不关心是哪个解析器产出的。

| 平台 | 方式 | 说明 |
|---|---|---|
| Bilibili | 原生 API(`view` → `playurl`) | 匿名可拿 720P;CDN 支持 Range |
| 抖音 / 小红书 / 快手 / 网易云 | 委托通用 yt-dlp | 直链签名易变,不做自研签名 |
| 其他 | 通用 yt-dlp | yt-dlp 支持的站点全部覆盖 |

关键实现细节:

- yt-dlp 通过 `sys.executable -m yt_dlp` 子进程调用,保证与当前虚拟环境同一份依赖
- **产物认定用目录快照差集**:下载前后各扫一次目录,只认新增的完整文件;
  `.part` / `.ytdl` 是中间态,不算成果
- m3u8 分片级续传靠 `.state.json` 记录「已完成分片数 + 字节数」;
  续传时按记录位置截断,避免上次写了一半的残留
- AES-128 用 pycryptodome 的 CBC 模式,IV 取自标签或按分片序号推导
- 每个流 URL 下载前都过 SSRF 校验——播放列表本身可能是攻击向量

**边界**:拿不到高清流时明确报出平台原因,不假装成功。
B 站评论接口匿名返回 `-403`,引擎把平台给的原话转述给用户,并支持 `--cookies`。

## 8. 反检测

| 组件 | 机制 |
|---|---|
| `profiles.py` | 21 台设备。locale 与时区由 `sale_region` 经 `REGIONS` 表推导,不允许手写;移动设备必须带触摸且 UA 含 `Mobile` |
| `inject.py` | init script + 加载后加固。覆盖 webdriver、cdc_ 残迹、chrome.runtime、platform、languages、hardwareConcurrency、maxTouchPoints、插件列表、WebGL 未掩码值、`Function.prototype.toString` |
| `behavior.py` | 打字间隔服从泊松过程(指数分布),停留时长服从对数正态(重尾),鼠标轨迹按 7:3 混合录制样本与三次贝塞尔生成 |
| `netguard.py` | Context 级路由拦截,只放行目标域及子域,其余 abort |
| `captcha.py` | ddddocr 识别文本验证码,cv2 模板匹配找滑块缺口 |

两处必须记住的实现事实:

1. **属性定义在原型上。** `navigator.platform` 等是 `Navigator.prototype` 的访问器,
   只在实例上 `defineProperty` 会被原型上的 getter 抢先。注入脚本原型与实例双改。
2. **掩码值不是失败信号。** 真实 Chrome 里 `gl.VENDOR` 就返回 `WebKit`。
   需要对齐的是 `WEBGL_debug_renderer_info` 扩展里的 `UNMASKED_*`(37445/37446)。

`BrowserWorker` 在 `setup()` 时用一次 `about:blank` 实测当前引擎是否真的执行
init script(`_probe_init_script`),结果存进 `_init_script_supported`。
不支持时 `apply_profile(inject_init_script=False)`,改由 `harden_page()`
在加载后补齐。这是 Patchright 1.61.2 的行为决定的(见经验记录 G-01)。

## 9. 存储

`vault.db` 的 `documents` 表以 **url 为主键**:

```sql
documents (
  url TEXT PRIMARY KEY, content_hash TEXT NOT NULL, domain TEXT, title TEXT,
  content TEXT, nonce BLOB, encrypted INTEGER, version INTEGER,
  meta TEXT, first_seen REAL, updated_at REAL, seen_count INTEGER
)
```

增量语义:同 URL 正文 SHA-256 未变 → `seen_count + 1`,版本不变;
变了 → 版本 +1,覆盖正文。这样反复抓同一页不会把库撑大。

加密可选:密钥取 `MELAB_KEY`,十六进制 / base64 / 任意口令统一 SHA-256 派生为
32 字节主密钥,每条记录独立随机 nonce,`cryptography` 的 AESGCM。
未配置密钥时明文存储,`stats()` 里的 `encrypted` 字段会如实反映。

`quality.py` 的 SimHash 用 SHA-256 截断作词哈希。SimHash 只要求词哈希均匀分布,
原始论文用 MD5 是惯例而非要求;换 SHA-256 不影响汉明距离的性质。
阈值 3 位以内视为近重复。

## 10. 可观测

| 产物 | 内容 |
|---|---|
| `data/stats.jsonl` | 逐事件:`task_done` / `task_failed`,带 url、channel、耗时 |
| `data/checkpoint.json` | 最近一次进度快照,进程中断后可读 |
| `data/deadletter.jsonl` | 批量落库失败的兜底记录,避免静默丢失 |

`mel stats` 读取 `tasks` / `episodes` / `documents` / `media_downloads`
四张表的聚合,输出通道占比与域名分布。

## 11. 配置

`EngineConfig` 是唯一的配置入口,CLI 参数只是它的投影。

| 字段 | 默认 | 作用 |
|---|---|---|
| `output_dir` | `data` | 队列库、仓库、产物根目录 |
| `http_workers` / `browser_workers` | 2 / 1 | Worker 数量 |
| `delay` / `jitter` | 1.5 / 0.5 | 最小间隔与抖动 |
| `timeout` | 20.0 | 单次请求超时 |
| `retries` | 2 | 网络失败重试次数 |
| `max_inflight` | 8 | 全局在途上限 |
| `max_per_domain` | 20 | 每域在途上限 |
| `max_depth` | 1 | 链接发现深度 |
| `max_links_per_page` | 20 | 每页最多入队多少链接 |
| `obey_robots` | True | 是否遵守 robots.txt |
| `use_curl` | True | 是否优先 curl_cffi |
| `captcha_retry` | True | 验证码识别成功后是否回填重试 |
| `use_site_rules` | True | 是否应用声明式规则 |
| `quality_gate` | True | 是否启用质量闸 |
| `vault_key` | None | 仓库加密密钥(回退 `MELAB_KEY`) |
| `cookie_file` | None | 媒体通道的自备 cookie |

CLI 参数与 `EngineConfig` 字段之间的映射集中在 `cli._engine()`,
避免出现「命令行调了但引擎没接」这类静默失效。

## 12. 错误处理与降级

| 失效点 | 行为 |
|---|---|
| 网络异常 | 指数退避重试,次数用尽后标记失败 |
| 429 | 按 `Retry-After` 退避 + 域名冷却;冷却不计入主重试次数 |
| 5xx | 指数退避重试 |
| 反爬信号 | 记入 Episode 的 `anti_bot_signals`,按 BLOCKED 处理 |
| 验证码 | 识别成功 → 回填重试一次;识别不可用 → 按降级策略失败并说明原因 |
| 空壳页 | 升级到浏览器通道重试一次(不消耗预算) |
| 低质量正文 | 质量闸拒收;若来自 HTTP 通道则先尝试升级 |
| 组件缺失 | 明确报错或入死信,不静默降级(pymupdf / ddddocr / yt-dlp / pycryptodome 各自对应一条降级路径) |
| Worker 崩溃 | 租约过期回收任务 + 池重启进程 |
| 批量落库失败 | 写 `deadletter.jsonl` |

设计原则只有一条:**能在便宜通道上判定失败,就不要升级;必须升级时,只升级一次。**
无限升级会让「最快路径优先」变成「最终都走最贵路径」。
