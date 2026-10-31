# 工程经验记录

本文件记录这套引擎在实现与实测过程中踩出来的问题:现象、根因、处置、复核方式。

写它的理由很直接——这些坑每一个都花掉了至少一轮返工,而大部分在文档里查不到。
放在仓库里,下次不必重新踩。

编号 `G-xx` 在其它文档里被引用(架构、白皮书)。

---

## 分类索引

| 类别 | 编号 | 一句话 |
|---|---|---|
| 浏览器 | G-01 | Patchright 不执行任何脚本注入 |
| 浏览器 | G-04 | `navigator` 属性定义在原型上 |
| 浏览器 | G-05 | 无头 Chromium 的插件列表为空 |
| 浏览器 | G-06 | WebGL 掩码值不是注入失败 |
| 进程 | G-02 | 中文路径下 libcurl 加载不了 CA |
| 进程 | G-03 | `DuplicateHandle` 是假象,pickle 才是真因 |
| 提取 | G-08 | 正文密度必须用子树文本 |
| 提取 | G-09 | 骨架页会骗过密度算法 |
| 提取 | G-12 | Nuxt 的 hydration state 不能用正则取 |
| 判定 | G-07 | 「验证码」出现在 HTML 里不等于有验证码 |
| 调度 | G-11 | 通道升级吃掉了 `--max` 预算 |
| 媒体 | G-13 | yt-dlp 的中间态不是产物 |
| 媒体 | G-14 | 平台解析器不能是唯一路径 |
| 媒体 | G-16 | B 站 CDN 支持 Range |
| 编码 | G-17 | 非 UTF-8 页面按声明字符集解码 |
| robots | G-18 | robots 检查不得依赖默认 UA 抓取 |
| 平台 | G-10 | B 站评论 -403 是端点选错,不是环境不够 |
| 依赖 | G-15 | opencv 与 numpy 的版本冲突 |
| 环境 | G-19 | markdownify 缺失会静默退化为正则剥离 |
| 媒体 | G-20 | 优化路径的触发条件在主路径上永远为假 |

---

## 浏览器

### G-01 Patchright 不执行任何脚本注入

**现象**

浏览器通道的指纹注入完全不生效:`navigator.webdriver` 仍可读、`chrome.runtime` 缺失、
`platform` 是原生的 `Win32`,GPU 显示 `WebKit | WebKit WebGL`。

**根因**

用最小样本对照两个引擎:

```
patchright  ctx mark: MISSING     patchright  page mark: MISSING
playwright  ctx mark: ctx         playwright  page mark: page
```

同一份 `add_init_script` 在官方 Playwright 下正常,在 Patchright 1.61.2 下完全无效。
进一步测试 `route.fetch()` + `route.fulfill(body=改写后的 HTML)` —— 官方 Playwright
`marker: yes`,Patchright `marker: MISSING`。

结论:Patchright 的驱动刻意不提供文档级脚本注入通道。这是它规避 CDP 蜜罐的代价。

**处置**

不假设引擎能力,启动时实测。`BrowserWorker.setup()` 调 `_probe_init_script()`:
新建一个 context,`add_init_script` 写一个标记,`goto("about:blank")`,读标记。
结果存进 `_init_script_supported`。

不支持时:
- `apply_profile(context, profile, inject_init_script=False)` —— 跳过 init script
- 页面加载后调 `harden_page(page, profile)` 补齐指纹

**复核**

```
$ python fingerprint_probe.py
engine: patchright | init_script_supported: False
... 10 项检测 · 0 项标红
```

**代价**

注入发生在页面加载之后。对「脚本一加载就采集指纹」的站点,原生值仍会露出。
要文档级注入得切官方 Playwright,代价是暴露 CDP 调试器。

---

### G-04 `navigator` 属性定义在原型上

**现象**

注入脚本里写了 `Object.defineProperty(navigator, 'platform', {get: () => 'Linux armv8l'})`,
但页面读到 `navigator.platform` 仍是 `Win32`。

**根因**

`navigator.platform` / `languages` / `hardwareConcurrency` 等是 `Navigator.prototype`
上的访问器属性。在实例上定义同名属性,读取时仍会走原型链上的 getter。

**处置**

`patchNav()` 同时修改两处:

```js
const patchNav = (prop, value) => {
  try { Object.defineProperty(Navigator.prototype, prop, {get: () => value, configurable: true}); } catch (e) {}
  try { Object.defineProperty(navigator, prop, {get: () => value, configurable: true}); } catch (e) {}
};
```

**复核**

指纹探针里 `navigator.platform` 与所用档案的 `platform` 字段一致。

---

### G-05 无头 Chromium 的插件列表为空

**现象**

指纹探针里 `navigator.plugins.length === 0`。

**根因**

真实桌面 Chrome 默认带 5 个 PDF 相关插件(`PDF Viewer`、`Chrome PDF Viewer`、
`Chromium PDF Viewer`、`Microsoft Edge PDF Viewer`、`WebKit built-in PDF`)。
无头 Chromium 一个都没有。这是一条 JS 就能识破的硬特征。

**处置**

注入脚本构造 5 个插件对象和对应的 `mimeTypes`,挂到 `Navigator.prototype`。
只在原生列表为空时替换。

**复核**

```
OK   plugins 数量 = 5
```

---

### G-06 WebGL 掩码值不是注入失败

**现象**

注入脚本覆盖了 `WebGLRenderingContext.prototype.getParameter`,但读 `gl.VENDOR`
仍然返回 `WebKit`,据此判断注入失败并开始排查。

**根因**

判断错了。真实 Chrome 里 `gl.VENDOR`(0x1F00)与 `gl.RENDERER`(0x1F01)本来就返回
`WebKit` / `WebKit WebGL` —— 这是浏览器故意给的掩码值。真正能区分 GPU 的是
`WEBGL_debug_renderer_info` 扩展里的 `UNMASKED_VENDOR_WEBGL`(37445)与
`UNMASKED_RENDERER_WEBGL`(37446)。

改掩码值反而更假。

**处置**

注入脚本按 37445/37446 覆写,掩码值保持原样。探针同时输出两个值:

```
OK   WebGL 掩码 = WebKit | WebKit WebGL
OK   WebGL 未掩码 = Qualcomm | Adreno (TM) 740
```

**教训**

判断「生效与否」必须先确认「正确的期望值是什么」。期望值错了,会把正常当异常排查。

---

## 进程

### G-02 中文路径下 libcurl 加载不了 CA

**现象**

首次调用 curl_cffi 即失败:

```
curl_cffi.requests.exceptions.SSLError: Failed to perform, curl: (77)
error adding trust anchors from locations:
CAfile: C:\开发的工程文件\...\certifi\cacert.pem CApath: none
```

**根因**

Windows 版 libcurl 无法从含非 ASCII 字符的路径加载信任锚。

**处置**

`ensure_ascii_ca_bundle()`:检查 `certifi.where()` 是否纯 ASCII,不是则复制到
系统临时目录下的 `melab_cacert.pem`,并设置 `CURL_CA_BUNDLE` 环境变量。

**复核**

```
$ python -c "from curl_cffi import requests; d=requests.get('https://tls.peet.ws/api/all').json(); print(d['tls']['ja3_hash'])"
35c61a611436bd206b042c36810d36ad
```

顺带一个发现:工程书的验证命令写的是 `json()['ja3_hash']`,但 peet.ws 当前把该字段
放在 `tls` 子对象下,顶层没有这个键。按实际结构调整。

---

### G-03 `DuplicateHandle` 是假象,pickle 才是真因

**现象**

Windows 上启动 Worker 进程时报:

```
PermissionError: [WinError 5] 拒绝访问。
  File "multiprocessing\spawn.py", line 113, in spawn_main
    new_handle = reduction.duplicate(pipe_handle, ...)
```

看起来像权限或句柄继承问题,花了不少时间在权限上排查。

**根因**

翻完整 traceback 才看到真正的原因:

```
_pickle.PicklingError: Can't pickle local object <function ProxyHandler.__init__.<locals>.<lambda>>
when serializing dict item 'http_open'
when serializing urllib.request.ProxyHandler state
...
when serializing meilab.workers.http.HttpWorker object
```

`HttpWorker.__init__` 里存了 `urllib.request.OpenerDirector`,它内部含 lambda,
不可 pickle。父进程 dump 中途失败,子进程拿到残缺数据,于是报出**看似无关**的
句柄错误。

**处置**

三处修复:

1. `BaseWorker.__getstate__` / `__setstate__`:序列化时剔除运行时对象
   (`RUNTIME_ATTRS` = `_opener` / `_curl` / `_pw` / `_browser` / `_context`)
2. `HttpWorker` 的 urllib opener 改为惰性构造(`_get_opener()`),不进 `__init__`
3. 队列改用 `Manager().Queue()` 代理

顺带修了一个真 bug:`BaseWorker.run()` 里误用了未定义变量 `task`(应为 `item`),
导致每个任务都抛 `NameError`。

**复核**

```
started slots: [{'kind': 'http', 'alive': True, ...}, …]
dispatch ok: True
result: http True 200 curl_cffi 577
after kill alive: {'http': 1}
reaped: ['http#0(重启1次)']
after reap alive: {'http': 2}
```

**教训**

Windows spawn 的错误信息会指向错误的层。遇到 `DuplicateHandle` 先看**完整** traceback
里有没有 pickle 错误。

---

## 提取

### G-08 正文密度必须用子树文本

**现象**

单测 `test_find_main_block_picks_article` 失败:主区块返回 `None`。

**根因**

密度定义为「直接文本长度 ÷ 后代标签数」。而正文通常写成
`<article><p>…</p><p>…</p></article>` —— `article` 的**直接文本**只有空白,
密度恒为 0。所有正文容器的密度都是 0,自然选不出最佳区块。

**处置**

改为「子树文本长度 ÷ (1 + 后代标签数)」。这是业界常用定义,分母加 1 避免
叶子节点除零。

**复核**

`tests/test_extract_html.py` 12 项通过;真实页面上 HTML 通道提取 2,533 字符。

---

### G-09 骨架页会骗过密度算法

**现象**

JS 渲染页在 HTTP 通道上「提取成功」,但正文只有 29 字符(导航栏)。

**根因**

骨架页有完整的 DOM 结构,只是正文由脚本填充。密度算法能找到一个「最像正文」的块,
但那个块里什么都没有。仅靠密度无法区分「短文章」与「空壳」。

**处置**

两重判据:

1. `_looks_like_shell()`:页面里有 `<script>`,且剔除脚本与标签后**最长的一段可见文本**
   不足 200 字符
2. 质量闸:正文短于 200 字符判低质量

两者同时成立且来源是 HTTP 通道时,升级到浏览器通道重试一次。

**复核**

```
mel crawl --url https://quotes.toscrape.com/js/ --max 3
  /js/ 页:http 0 字符 → 升级 → 浏览器 1,429 字符
  通道:http 3 · browser 3
```

---

### G-12 Nuxt 的 hydration state 不能用正则取

**现象**

`window.__NUXT__` 解析不出数据,或只解析出前半截。

**根因**

最初用正则 `window\.__NUXT__\s*=\s*(\{.*?\})` 匹配。非贪婪匹配在第一个 `}` 就停,
嵌套 JSON 被截断。改成贪婪匹配又会吃进后续脚本。

另外 `window.__NUXT__=JSON.parse("…")` 形态里,字符串内的 `\"` 用 `json.loads` 解析会失败。

**处置**

- 只用正则定位 `window.__NUXT__\s*=\s*` 的**起点**
- 用 `json.JSONDecoder().raw_decode()` 从起点做括号感知解析
- `JSON.parse(...)` 形态另写一个 JS 字符串字面量读取器,手动处理 `"` `'` `\\` `\n` 等转义

**复核**

三种形态全部通过:

```
direct          -> nuxt 1
jsonparse_single-> nuxt 1
jsonparse_double-> nuxt 1
```

---

## 判定

### G-07 「验证码」出现在 HTML 里不等于有验证码

**现象**

抓 B 站页面时,浏览器通道返回的 `signals` 里有 `captcha`,Episode 被判 `blocked`,
但页面本身是正常的视频页。

**根因**

`detect_anti_bot_signals()` 只做裸字符串匹配。B 站每个页面都会**预加载**风控 SDK:

```html
<script src="https://s1.hdslb.com/bfs/seed/jinkela/risk-captcha-sdk/CaptchaLoader.js"></script>
```

这是预防性加载,不是真的弹了验证码。任何站点只要沿用同类 SDK,都会被误判。

**处置**

检测前先剔除 `<script>` 段,只对脚本之外的内容判验证码:

```python
without_scripts = re.sub(r"(?is)<script\b.*?</script>", " ", raw).lower()
if re.search(r"(recaptcha|hcaptcha|geetest|g-recaptcha|captcha-container|验证码)", without_scripts):
    signals.append("captcha")
```

**复核**

回归用例同时覆盖正反两面:

```
'<div class="g-recaptcha"></div>'                        -> ['captcha']
'<div id="captcha-container">请完成验证</div>'            -> ['captcha']
'<script src=".../CaptchaLoader.js"></script><p>正文</p>' -> []
'<script>var captcha=1</script>'                          -> []
'Just a moment...'                                        -> ['status_503', 'cloudflare_challenge']
```

**教训**

这是「判据必须查结构,不许查文本」最典型的一次。字符串出现在页面里,和它起作用,
是两件不同的事。

---

## 调度

### G-11 通道升级吃掉了 `--max` 预算

**现象**

`mel crawl --url <JS 站> --max 3` 跑完,统计显示 3 个任务「成功」,但仓库里没有正文,
队列里留着 `pending` 任务。通道分布是 `http 8 · browser 0`。

**根因**

升级流程是「HTTP 尝试 → 判空壳 → 回队 + 打标记」。但那次失败的 HTTP 尝试
已经让 `processed += 1`。`--max 1` 时第一次尝试就把预算用光,循环退出,
任务留在 pending 永远不动。

更隐蔽的是:统计上「派发数」看起来正常,只有对照通道分布才发现浏览器通道一次没跑。

**处置**

`_handle_result()` 的返回值改为「任务是否进入终态」:

- 升级、验证码回填重试 → 返回 `False`(任务会被重新领取,不消耗预算)
- 成功、真失败、拒收 → 返回 `True`

调用方只在 `True` 时 `processed += 1`。

**复核**

修复前后对比:

```
修复前:通道 {http: 8, browser: 0}    ← 升级从未真正发生
修复后:通道 {http: 3, browser: 3}    ← 升级生效,/js/ 页拿到 1429 字符
```

**教训**

这条恰好命中 kiana 整改记录的第四条纪律:**计数口径必须与被移动的状态同源**。
「已处理数」和「进入终态的任务数」是两个口径,混用就会得到假结论。

---

## 媒体

### G-13 yt-dlp 的中间态不是产物

**现象**

下载超时后,`files` 列表里出现了:

```
['master [master].f8366.mp4.part', 'master [master].f8366.mp4.ytdl']
```

按这个列表判断「已下载」,结果是错的——那两个是未完成的中间文件。

**根因**

产物认定用的是「目录快照差集」,只排除了 `.part`,漏了 `.ytdl`。

**处置**

两个都排除,并单独记录到 `partial` 字段,错误信息里说明「已保留半成品 N 个,重跑可续传」。

**复核**

```
ytdlp test-download ok: True
  files: ['master [master].mp4']   部分: []
```

---

### G-14 平台解析器不能是唯一路径

**现象**

最初的媒体通道设计里,平台解析失败就直接判任务失败。

**根因**

抖音、小红书、快手、网易云的直链需要请求签名(a_bogus / x-s / weijia),
签名算法随平台更新失效。自研签名器是持续逆向,且失效时是硬失败——用户拿到的是
「不支持」,而不是「换条路试试」。

**处置**

三条路径依次回退:

1. 平台原生解析(B 站有稳定公开 API)
2. 通用 yt-dlp 通道(它自己维护这四家的 extractor)
3. 明确报错,并说明是哪一步失败的

`resolve_media()` 返回 `(MediaItem | None, 说明)` —— 说明里写明用了哪条路径、
为什么失败。用户看得到原因,不用猜。

---

### G-16 B 站 CDN 支持 Range

**验证结果**

只取 1 KB 验证续传的前提:

```
bilibili CDN range: 206 | got bytes: 1024
content-range: bytes 0-1023/453914132
```

总长 453,914,132 与 playurl 返回的 `size` 一致,说明可以按字节续传。
这条不是坑,是**必须验证的能力**——如果 CDN 不支持 Range,续传就是空实现。

---

## 平台

### G-10 B 站评论匿名不可读

**现象**

`mel comments` 对任意视频都返回 0 条。

**排查过程**

1. 旧接口 `/x/v2/reply` → `code 0`,但 `replies` 恒为 0 条
2. 换成 `/x/v2/reply/wbi/main` → `code -403`,需要 WBI 签名
3. 实现 WBI 签名(nav 取 key → 置换表混合 → MD5 `w_rid`)→ 仍 `-403`
4. 用浏览器通道拿到会话 cookie(`buvid3` / `b_nut` / `user_site`)再请求 → 仍 `-403`

**结论**

平台已限制匿名读取。需要含 `SESSDATA` 的登录态 cookie。

**处置**

`mel comments` 支持 `--cookies`,并在匿名时**如实报出平台原因**:

```
评论接口返回 访问权限不足(平台已限制匿名访问;需要用户自备 cookie 才能读取评论)
```

不假装成功、不返回空列表让用户以为「这个视频没评论」。

**2026-10-31 补充:用真实登录态 cookie 验证后的最终结论。**

用户提供了含 `SESSDATA` 的 cookie(经 `x/web-interface/nav` 验证 `isLogin: True`,
cookie 本身有效)。但 `reply/wbi/main` 仍然 `-403`:

1. 正确的 WBI 签名 + 有效登录态 → `-403`;
2. 加上社区的 `dm_img_list` / `dm_img_str` / `dm_cover_img_str` / `dm_img_inter`
   风控参数 → 仍 `-403`。

结论:该接口的风控要求**真实浏览器渲染上下文**(`w_webid` 之类只能从渲染后的
页面状态拿到的字段),纯 API 端路走不通。评论采集的正确路径是浏览器通道
渲染评论区后从 DOM 提取——登记为媒体通道的工作项,不在 API 端继续追。

同一次验证的正面产出:cookie 转发接通了 `bilibili_resolve`(此前该函数收了
cookies 参数但没传给 API);DASH 模式(`fnval=4048`)下,带登录态的
`accept_quality` 解锁到 `[116, 80, 64, 32, 16]`(1080P60)。当前 720P 上限
是解析器用 `fnval=1`(mp4 durl)造成的格式选择,DASH 是媒体通道的工作项。

**2026-10-31 二次修正:上面的结论错了,真因是端点选错。**

DASH 收尾时做端点矩阵对照(签名 × cookie × 端点),一次探清:

| 端点 | 结果 |
|---|---|
| `/x/v2/reply/wbi/main` | 恒定 `-403`,签名、登录态、风控参数都不放行 —— **该端点被平台限制**,不是采集路径 |
| `/x/v2/reply/main`(WBI 签名) | `code 0`,20 条;但 `pagination_str.offset` 不生效,翻页拿到同一页 |
| `/x/v2/reply`(无签名,pn 翻页) | `code 0`,翻页正常:48 条评论 2 页取完、页间不重复、楼中楼内嵌 |

也就是说:根本不需要浏览器通道,甚至不需要 WBI 签名。上一轮把「`wbi/main` -403」
误读成「评论接口被限制」,实际只是**那一个端点**被限制;老接口一直活着。
「浏览器渲染上下文」的推断也在浏览器内实测被否定——headless 页面里 fetch
`wbi/main` 同样 -403,而老接口在 curl 里就通。

登录态的真实作用是**条数**而非可访问性:匿名 3 条/页,带 SESSDATA 20 条/页。

教训:换端点对照矩阵(自变量拆开逐个试)应该先于「推断需要更强环境」的假设。
上一轮在单端点上堆 cookie、风控参数、浏览器通道,是沿错误假设升级火力。

**处置**:评论采集改走 `/x/v2/reply` + `pn` 翻页;`page` 参数从原来的装饰品
变成真实翻页。`mel comments` 输出同步更正。

---

## 依赖

### G-15 opencv 与 numpy 的版本冲突

**现象**

按工程书钉死版本安装时报:

```
The conflict is caused by:
    meilab-orchestral-crawler 0.1.0 depends on numpy==2.5.2
    opencv-python-headless 4.12.0.88 depends on numpy<2.3.0 and >=2
ERROR: ResolutionImpossible
```

**处置**

查 PyPI 可用版本后,`opencv-python-headless` 由 `4.12.0.88` 改为 `5.0.0.93`
(与 numpy 2.5 兼容的最新 headless 版),`numpy` 保持 `2.5.2`。

**留档要求**

版本偏离必须原样记录,不能默默换版本还声称装的是原版本。这条回执同时写在
[构建与运行](BUILD.md) 与项目状态文件里。

---

## 真实样本

> 以下两条来自 2026-10-31 的真实样本验证。样本由独立的采集流程提供:
> 简书 robots.txt、浙江美术馆(gb2312)、建築研究所(Shift_JIS)、
> 四平台公开分享链接。验证方法:修复前抓一遍留缺陷回执,修复后复抓对照。

### G-17 非 UTF-8 页面按 UTF-8 强解,正文全是乱码

**现象**

`mel crawl` 抓 gb2312 / Shift_JIS 页面,"成功"入库,但正文不可读:

```
修复前:zjam.org.cn(gb2312)      title='�㽭������' len=461  U+FFFD=243
修复前:kenken.go.jp(Shift_JIS)  len=18478  U+FFFD=10091
```

统计上一切正常,只有打开导出文件才能看到全是替换符。**这类缺陷不会报警,
只会在你真正去读产物时爆炸。**

**根因**

提取管线的 `_decode()` 写死了 UTF-8。而这类页面的编码声明就摆在两处:
响应头 `Content-Type: text/html; charset=gb2312`、HTML 的 `<meta charset>`。
声明一直在,没人读。

**排查时的弯路**

样本报告先发现"curl 不加 `--compressed` 时 grep 不到 meta 标签",怀疑是 gzip 问题。
实测本框架的链路:curl_cffi 透明解压,拿到的是"编码错但结构完整"的文本——
不是 gzip 问题。教训同 G-06:**先确认正确的期望值,再判断生效与否。**

**处置**

`_decode(body, content_type)`:

1. 优先读响应头的 `charset=`;
2. 头缺失时读前 2KB 的 `<meta charset>`(与浏览器行为一致);
3. gb2312 / gbk 统一映射到超集 `gb18030`,避免声明 gb2312 实际含扩展字时解码失败;
4. 声明的编码器不存在时退回 UTF-8,不抛异常。

**复核**

```
修复后:zjam    gb2312      title='浙江美术馆'          U+FFFD=0
修复后:kenken  Shift_JIS   title='国立研究開発法人 建築研究所'  U+FFFD=0
```

单测:`tests/test_extract_html.py` 三个用例(gb2312 头声明 / Shift_JIS meta 声明 /
UTF-8 回归)。

---

### G-18 robots 检查静默失效

**现象**

简书 robots.txt 里明明写着 `Crawl-delay: 10`(Googlebot 组),
`RobotsChecker('Googlebot').crawl_delay()` 却返回 0.0。

**根因**

`_load()` 用的是 `RobotFileParser.read()`,它拿 **urllib 默认 UA**
(`Python-urllib/3.x`)去请求 robots.txt。拒绝该 UA 的站点返回非 200,
parser 解析到空内容,于是整个检查退化成"全部允许、无延迟"——
而且**没有任何告警**。

这条比 G-17 更危险:G-17 的后果可见(乱码),G-18 的后果是"以为在遵守规则,
实际什么规则都没读到"。

**处置**

`_load()` 改用本框架的抓取栈(curl_cffi + 浏览器 UA + CA 路径修复),
状态码非 200 显式返回 `None`;新增 `load_text()` 支持离线喂文本(单测用)。

**复核**

```
修复后:Googlebot crawl_delay = 10.0   Chrome-UA crawl_delay = 0.0(* 组无声明,正确)
```

单测:`tests/test_robots.py` 5 个用例,含豆瓣 robots 的反例——
注释行 `# Crawl-delay: 5` 不等于指令。

---

## 环境

### G-19 markdownify 缺失会静默退化为正则剥离

**现象**

`html_to_markdown` 对 `<h1>` 不再产出 `#`、换行全部丢失,表现与纯文本剥离无异;
`test_extract_returns_three_layers` 失败。而这套代码和测试几天前全绿。

**根因**

```
ModuleNotFoundError: No module named 'markdownify'
```

`html_to_markdown` 的 `except Exception` 把导入失败吞掉,走了正则回退——
回退路径工作正常,所以没有任何告警。真正的偏差在解释器:项目有 `.venv`,
但命令行里跑的是系统 Python(`Python314`),它没装 `markdownify`。
同一个测试文件,`.venv` 里通过,系统 Python 里挂。

**处置**

复核、测试、门禁一律显式走 `.venv/Scripts/python.exe`。
代码侧保留降级路径(依赖是可选的工程事实),但降级必须是**可见的**:
`extract_html` 的 notes 里记下了 strategy,静默的只是 markdown 转换层——
这层补了单测锁定「有 markdownify 时必须产出 ATX 标题」。

**教训**

- 「环境漂移」的故障面是整个工具链,排查顺序应在读代码之前:先 `sys.executable`
  和 `pip list`,再怀疑代码。
- 回退路径和无告警组合起来,会把配置错误伪装成行为变更。依赖探测要留在
  可观测的位置,不能全靠 `except Exception` 一吞了之。

---

## 媒体(续)

### G-20 优化路径的触发条件在主路径上永远为假

**现象**

并行 Range 下载器上线后,真实下载 1.25GB 仍然要 73–96 秒(单连接水平),
而独立探针测得 8 连接聚合 90 MB/s——并行路径理论上 14 秒,实测却没生效。

**根因**

触发条件链上有一个恒假环节:

```python
if have:
    headers["Range"] = f"bytes={have}-"     # 只有续传才带 Range
...
if response.status_code == 206 and total >= THRESHOLD:   # 200 进不来
    ...并行分块...
```

全新下载 `have == 0` → 不带 Range → 服务器回 **200**(不是 206)→
并行路径的门槛条件永远不成立。整条优化路径只在"续传且文件够大"时才活着,
而那条路此前从未被走过。

**处置**

Range 头无条件携带(`bytes=0-` 对服务端同样合法):支持 Range 的服务端回 206
并给出 `Content-Range` 总大小,并行路径有了触发条件;不支持的服务端照常回 200,
走原单连接路径。实测同一视频 207s → 41s,字节数与首轮完全一致。

分阶段打点定位的过程(resolve 0.4s / 测速 0.9s / 分块下载 12.4s /
合并 3.6s / 音频 2.3s / ffmpeg 合流 11.5s)同时暴露了第二个浪费:
`-movflags +faststart` 会让 ffmpeg 对整文件做第二遍重写(1GB 级 ~7s),
moov 前置只服务"边下边播"场景,本地播放用不上,去掉。

**教训**

1. **优化路径要有自己的可观测性。** "没报错、结果也对、就是慢"是优化路径
   被静默跳过的典型表现。给性能关键分支留一个可断言的痕迹(日志/计数),
   否则它是不是真的在跑,只能靠重新打点才知道。
2. **触发条件要在两条主路径上各验一遍。** 续传路径和全新下载路径的状态空间
   不同(`have` 为 0 与否),"在续传场景下测过"不等于"在全新下载场景下成立"。
3. 用户报告的"下载慢"值得逐层量化:先测单连接速度,再测多连接聚合,
   再对比端到端耗时——三层一对照,哪一层断了立刻现形。

---

## 横向纪律

从上面十六个问题里能提炼出四条,它们比单个修复更值得记住。

**1. 判据查结构,不查文本。** G-07 是原样案例。字符串在页面里出现,和它起作用,
是两件事。

**2. 「没有告警」不等于「通过」。** G-01 里如果只看日志没有异常,会以为注入生效了。
正确做法是在 `about:blank` 上实测并留下布尔状态。

**3. 复核命令必须真能跑出它上面的数字。** 本文件每条结论都附了命令或回执。
文档与命令不一致时,以命令为准。

**4. 计数口径必须与被移动的状态同源。** G-11 的根因就是两个口径不同源。

还有一条工程习惯:**同一能力只允许一份实现**。域名判断、URL 安全校验、
Referer 生成、脱敏规则都各自只有一处实现,其余位置调用它。
分叉的实现会在某次修改后行为不一致,而这类不一致很难被测出来。
