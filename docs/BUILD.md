# 构建与运行

本文件说明环境准备、平台差异、常见构建问题与发版流程。

---

## 1. 环境

| 项 | 要求 |
|---|---|
| Python | 3.12 以上(开发与实测均在 3.14.6 上完成) |
| 操作系统 | Windows 10/11 x64、Linux、macOS |
| 磁盘 | 引擎与依赖约 1 GB(不含浏览器二进制) |
| 网络 | 首次安装需要能访问 PyPI 与 GitHub Releases(浏览器二进制) |

### 1.1 安装

```bash
python -m venv .venv

# Windows
./.venv/Scripts/python.exe -m pip install -e ".[dev]"

# Linux / macOS
pip install -e ".[dev]"
```

`.[dev]` 会安装 pytest、ruff、mypy、pip-audit。只要运行时依赖就去掉 `[dev]`。

### 1.2 浏览器二进制

浏览器不由 pip 安装,需要单独一条命令:

```bash
# Windows
./.venv/Scripts/patchright.exe install chromium

# Linux / macOS
patchright install chromium
```

Playwright 的浏览器在部分环境下也需要:

```bash
playwright install chromium
```

Skip 掉这一步的后果:浏览器通道被整体禁用,动态页面进死信队列,其余通道照常运行。

### 1.3 可选组件

| 组件 | 安装 | 缺失时的行为 |
|---|---|---|
| PDF 支持 | `pip install -e ".[pdf]"` | PDF 任务以「不支持 PDF」入失败队列 |
| JS 运行时 | `winget install --id DenoLand.Deno`(或 Node.js ≥ 22) | yt-dlp 的 EJS 挑战降级,部分站点取不到流 |
| ffmpeg | 系统包管理器 | m3u8 只保留分片,不合并 |

引擎在启动时不强制检查这些组件,而是在用到时明确报错或按降级策略处理。
降级路径写在 [架构 · 错误处理](ARCHITECTURE.md#12-错误处理与降级)。

---

## 2. 平台差异

### 2.1 Windows

**中文路径下的 CA 证书**

libcurl 无法从含非 ASCII 字符的路径加载信任锚,首次调用 curl_cffi 会得到:

```
curl: (77) error adding trust anchors from locations:
CAfile: C:\…\certifi\cacert.pem CApath: none
```

引擎在启动时把 certifi 的 CA 复制到系统临时目录下的纯 ASCII 路径,
并设置 `CURL_CA_BUNDLE`。这一步在 `workers/http.py::ensure_ascii_ca_bundle()` 里,
不需要手动配。

**spawn 启动方式**

Windows 用 `spawn` 而非 `fork` 启子进程,要求整棵对象树可 pickle:

- Worker 实例通过 `__getstate__` 剔除运行时对象
- 队列用 `Manager().Queue()` 代理,不用裸 `mp.Queue`
- 任何 `multiprocessing` 调用都放在 `if __name__ == "__main__":` 守卫内

细节与踩坑过程见 [工程经验记录 G-03](ENGINEERING-LESSONS.md#g-03-duplicatehandle-是假象pickle-才是真因)。

**浏览器**

Patchright 默认装 Chromium。`patchright install chrome` 会要求先卸载系统已有的
Chrome,这一步不要执行——它会影响你的日常浏览器。引擎默认用 Chromium。

### 2.2 Linux

- 无头运行需要 `libnss3`、`libatk-bridge2.0-0`、`libgbm1` 等系统库。
  缺库时 Playwright 启动会明确报缺哪个,按提示装即可。
- `--no-sandbox` 已写在启动参数里(容器环境常见需要)。若在受限环境仍无法启动,
  检查 `/dev/shm` 大小。

### 2.3 macOS

- Apple Silicon 与 Intel 均可用,Chromium 会装对应架构。
- 首次运行可能触发 Gatekeeper 提示,允许即可。

---

## 3. 运行

```bash
mel crawl --url https://example.com --max 10
mel shell
mel stats
mel export --format markdown --output ./data
```

产物目录结构:

```
data/
├── queue.db          任务队列 + Episode + 媒体记录
├── vault.db          内容仓库(可选加密)
├── stats.jsonl       逐事件统计
├── checkpoint.json   进度快照
├── deadletter.jsonl  批量落库失败的兜底
├── markdown/<domain>/
├── jsonl/<domain>.jsonl
├── csv/<domain>.csv
├── xlsx/<domain>.xlsx
├── sqlite/<domain>.sqlite
└── media/<domain>/   媒体产物与 .part / .state.json
```

### 3.1 加密

内容仓库支持 AES-256-GCM,密钥来自环境变量:

```bash
export MELAB_KEY='任意口令'        # 或 64 位十六进制 / base64
```

密钥统一 SHA-256 派生为 32 字节主密钥,每条记录独立随机 nonce。
未配置时明文存储,`mel stats` 的 `vault.encrypted` 字段会如实显示 `false`。

媒体通道的自备 cookie 用 Fernet 加密,密钥优先级
`MELAB_COOKIE_KEY` > `MELAB_KEY`。

### 3.2 cookie

```bash
mel media --url <URL> --cookies ./cookies.txt
mel comments --url <URL> --cookies ./cookies.txt
```

格式为 Netscape cookie 文件(浏览器插件导出的默认格式)。
文件内容不会出现在日志或导出产物里。

---

## 4. 测试

```bash
python -m pytest tests/ -v
```

178 个用例,全部离线。不依赖网络,不启动浏览器(浏览器相关能力通过能力探测与
本地构造样本验证)。

| 文件 | 覆盖 |
|---|---|
| `test_queue.py` | 幂等入队、4 进程并发领取、状态机合法性、Episode 往返 |
| `test_queue_lease.py` | 租约写入、心跳续租、过期回收、CAS 双写拦截 |
| `test_sniff.py` | URL 模式 → 内容类型/通道、媒体识别、反爬信号正反例 |
| `test_router.py` | 任务类型优先级、媒体路由、决策可序列化 |
| `test_extract_html.py` | 密度、去噪、连续扩展、三层 residual、链接抽取 |
| `test_profiles.py` | 遍历全库一致性(locale↔时区↔平台↔触摸) |
| `test_media.py` | m3u8 解析、AES-128 本地加解密往返、平台识别、cookie 加密 |
| `test_ssrf.py` | 26 条对抗性样本、数字字面量主机名、私网段判定 |
| `test_captcha.py` | 能力探测、空输入降级、滑块缺口、本地 PNG |
| `test_privacy_quality.py` | 三层脱敏边界、SimHash 距离、质量闸 |

### 4.1 冒烟

```bash
mel crawl --url https://quotes.toscrape.com/ --max 5
mel crawl --url https://quotes.toscrape.com/js/ --max 3
python fingerprint_probe.py
mel stats
mel export --format markdown --output ./data
mel resolve --url https://www.bilibili.com/video/BV1Dvpg6BEt5/
mel rule validate rules/sites/bilibili-video.yaml
```

`quotes.toscrape.com/js/` 这一条专门验证分层升级:第一条通道应该 0 字符,
升级后浏览器通道应该拿到千字以上正文,通道分布里必须出现 `browser`。

`fingerprint_probe.py` 是外部指纹检测的本地替代。`bot.sannysoft.com` 在部分网络
不可达,探针用同一套生产代码在本地页面上逐项检查。

---

## 5. 发版

### 5.1 门禁

```bash
python tools/release_check.py
```

六项,任一失败即阻止发布:

| 检查 | 内容 |
|---|---|
| 版本一致性 | `pyproject.toml` / `meilab.__version__` / README(存在时)三者一致 |
| 工作区干净 | 没有未提交改动 |
| 全量测试 | pytest 全绿 |
| 静态基线 | ruff 错误数不超过基线,基线只允许下降 |
| 依赖漏洞 | pip-audit 无已知漏洞 |
| 密钥泄漏 | 源码里没有疑似密钥 |

**得不出结论的检查记为 SKIP,不记为 PASS。** 这条比任何单项检查都重要——
把「没检查」当成「检查过且没问题」,门禁就失去了意义。

### 5.2 文档数字

```bash
python tools/doc_numbers.py --check    # 校验
python tools/doc_numbers.py --write    # 写回
```

README 里的规模数字由 `<!-- AUTO:NUMBERS -->` 块承载,由脚本生成。
手工抄录的数字会互相矛盾,这是从姊妹工程整改记录里学来的教训。

### 5.3 版本号

三处必须同步:`pyproject.toml` 的 `version`、`meilab/__init__.py` 的 `__version__`、
README 顶部出现的版本号。门禁第一项就是查这个。

---

## 6. 已知构建问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `ResolutionImpossible`(numpy / opencv) | `opencv-python-headless 4.12.0.88` 要求 `numpy<2.3.0` | 用 `5.0.0.93`,详见 [G-15](ENGINEERING-LESSONS.md#g-15-opencv-与-numpy-的版本冲突) |
| `curl: (77)` | CA 路径含非 ASCII 字符 | 引擎自动处理;若自写脚本调用 curl_cffi,需手动设 `CURL_CA_BUNDLE` |
| `DuplicateHandle [WinError 5]` | 父进程 pickle 失败,不是权限问题 | 见 [G-03](ENGINEERING-LESSONS.md#g-03-duplicatehandle-是假象pickle-才是真因) |
| 浏览器通道全部失败 | 未执行 `patchright install chromium` | 跑那条命令;或接受降级 |
| `mel media` 报「未安装 yt-dlp」 | 依赖未装全 | `pip install -e .` |
| 视频只有分片没有成品 | 未装 ffmpeg | 装 ffmpeg,或接受分片产物 |
| pytest 在 Windows 报 `PermissionError: pytest-current` | pytest 清理符号链接的权限问题 | `pyproject.toml` 已设 `--basetemp=.pytest_tmp` 规避 |
