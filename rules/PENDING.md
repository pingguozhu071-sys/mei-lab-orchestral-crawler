# 站点规则待办

`rules/sites/` 下的规则按需增补。这里记录计划、已知失效与验收方式。

规则机制本身已完成:`mel rule validate` 校验结构,`mel rule test` 用真实页面试跑。
下面缺的是**规则资产**,不是能力。

---

## 当前状态

| 规则 | 站点 | 状态 |
|---|---|---|
| `bilibili-video.yaml` | B 站视频页 | 可用(实测抽出 title / author / description / thumbnail) |
| `zhihu-question.yaml` | 知乎问答页 | 已编写,未验证(本网络 robots 拦截,无法试跑) |
| `netease-song.yaml` | 网易云歌曲页 | 可用(实测:晴天 / 周杰伦 / 叶惠美 / 269s / 2003-07-31,全部来自 og meta) |
| `douban-book.yaml` | 豆瓣书籍条目页 | 可用(实测:小王子 / 作者 / 译者 / 出版社 / ISBN / 评分 9.1 / 简介 / 封面) |
| `thepaper-news.yaml` | 澎湃新闻文章页 | 可用(实测:title / summary / cover;正文容器是构建哈希 class,不写规则) |
| `sspai-post.yaml` | 少数派文章页 | 可用(rule test 实测:教育的下一步 · 其二,SSR 直出) |
| `36kr-article.yaml` | 36氪文章页 | 可用(渲染后 HTML 实测:title / author / summary / content / cover;HTTP 通道是壳) |
| `template.yaml` | 起手模板 | 模板,不参与匹配 |

schema 已扩展 `key` 取值:豆瓣 `#info` 这类「所有元信息挤在一个容器」的键值对页面,
`{sel: "#info span.pl", key: "作者"}` 可以精确拆字段(跳过孤立的冒号文本节点)。

---

## 2026-10-31 侦察结论(按站点)

| 站点 | 结论 |
|---|---|
| 36kr | **真 SPA**(HTTP 通道 25KB 全是壳,meta/正文全空);规则按渲染后 DOM 写,验证要用浏览器渲染的 HTML 直接喂 `rule.extract`,`mel rule test` 验不了 |
| 少数派 | **SSR**(HTTP 通道直出全量 HTML)——此前误判为 SPA,是样例 URL 无效(404 回首页)所致;真实 URL 下 `.article-body` / og meta 直接可抽 |
| 界面 | 样例 URL 404;规则待真实 URL |
| 贴吧 | HTTP 403(风控),HTTP 通道不可达 |
| V2EX / Hacker News | 本网络超时(环境限制,非站点问题) |
| 澎湃 | og meta 可靠;正文 class 全是 `content__bOXdU` 型构建哈希,**不写进规则** |
| 豆瓣电影 | 与书籍同构,但条目页当天被限流(2950 字节拦截壳),待冷却后验证 |
| 网易云 / 豆瓣书籍 | 全部信息在 og meta 与语义结构里,是规则覆盖的最佳形态 |

**教训:样例 URL 必须真实。** 编造的 ID 被 404 回首页后,渲染结果长得就像 SPA 壳,
据此会把 SSR 站误判成 SPA。判定 SPA 与否要用真实 URL 的 HTTP 响应字节数对照。

---

## 待补

按优先级排列。每条需要:选择器在真实页面验证过、`rule test` 通过。

### 优先级 1 —— 结构稳定、已有媒体通道覆盖

- [x] **网易云音乐页** —— 歌曲名、歌手、专辑、时长;`netease-song.yaml`,og meta 驱动
- [ ] **B 站评论区/动态页** —— 与现有 `bilibili-video` 共用域名,注意 `match` 冲突
- [ ] **豆瓣电影条目页** —— 与书籍同构(`v:itemreviewed` / `#info` / `.rating_num`),需要真实电影 URL 验证

### 优先级 2 —— 主流新闻

- [x] 澎湃(og 字段可用;正文哈希 class 放弃)
- [x] 36 氪(浏览器通道渲染后 DOM;`36kr-article.yaml`)
- [x] 少数派(SSR,`sspai-post.yaml`)
- [ ] 界面 —— 需真实 URL

### 优先级 3 —— 论坛与社区

- [ ] 贴吧 / V2EX / Hacker News —— 本网络不可达或 403,待环境
- [ ] 豆瓣电影(见优先级 1,限流冷却中)

### 优先级 4 —— 电商

- [ ] 商品详情页(价格、销量、SKU)—— 结构变动最频繁,放最后

---

## 已知失效与注意事项

**选择器要挑语义类名。** 带构建哈希的类名(如 `_nick_1a2b3c`)会在站点发版后失效。
优先选设计系统语义类(`woo-panel-main`)、属性选择器(`[itemprop]`)、
结构位置(`.post-content > p`)。澎湃的 `content__bOXdU` 是现成反面教材。

**分页字段还没进 schema。** 当前规则只覆盖详情页字段,列表页翻页没有配置项。
如果需要批量采集列表页,得先扩展 `site_rules.py` 的字段类型。

**容错段还没进 schema。** 多选择器降级(如 `div.a, div.b` 逗号分隔)可以用,
但"某个字段失败不影响其它字段"的语义目前是隐式的——`_extract_field` 返回 `None`
时该字段缺省,不影响其它字段。这个行为没有在规则文件里声明,文档化之前别依赖它。

**meta 的 `content` 属性只在值长得像相对路径时才补绝对链接。**
`og:title` / `og:description` 的 content 是纯文本,不能当 URL 拼;
以 `/` 开头的值(如 og:image 相对路径)才会被 `urljoin`。

**规则命中会覆盖启发式。** 如果规则的选择器写错(比如选到了广告位),
结果会比纯启发式更差。所以新增规则必须 `rule test` 验证,不能只看校验通过。

---

## 增补流程

```bash
# 1. 复制模板
cp rules/sites/template.yaml rules/sites/<站点>.yaml

# 2. 改 match / 选择器

# 3. 校验结构
mel rule validate rules/sites/<站点>.yaml

# 4. 用真实页面验证
mel rule test rules/sites/<站点>.yaml --url <真实 URL>

# 5. 确认抽到的是正文而不是广告/推荐位
```

第 5 步比前四步重要。校验通过只说明 schema 合法,`rule test` 能跑通只说明选择器
匹配到了元素——匹配到的内容是不是你想抽的,只能人看。
