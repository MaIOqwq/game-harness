# B站（bilibili）爬虫说明书（从关键词进到样本出，全功能）

> 定位：B站侧负责「视频社区里玩家怎么聊」。它用关键词在 B站搜**视频**，取回**视频标题/简介/量化数据** +
> **每视频最多 10 条热评**；另有一条**官号探针**（官方账号的动态 + 各自评论），专供舆情类题。
> 代码分层：模型面工具 `crawl_bili` / `crawl_official`（`harness/tools/crawl_live.py`）→ 取数器
> `harness/sources/live.py` → 常驻服务 `crawlers/bili/bili_search_server.py` → 爬取库
> `crawlers/bili/media_platform/bilibili/core.py`（单查询逻辑）+ `client.py`（所有 API）+ `login.py`（登录态）。

---

## 0. 完整链路（关键词 → 样本）

```
模型给出 {game: 游戏名, query: 短关键词}
   │
   ├─ 引擎侧组词  live.crawl_bili          → 检索词 = 游戏名+关键词，全去空格直拼
   │
   ├─ POST /crawl 到常驻服务（端口 8771，X-Token 鉴权，单飞）
   │
   ├─ 服务端 search_one_query(query, max_videos=6, windows/since/until)
   │     ├─ 调 B站搜索接口（视频）→ 取前 N 个视频
   │     ├─ 逐视频取详情（标题/简介/播放/弹幕/点赞/标签）
   │     └─ 逐视频取热评（默认排序前 10 条一级评论）
   │
   └─ 返回 {query, time_windows, videos:[{…, comments:[…]}]}
         │
         └─ harness 转成 items（kind=post 视频 / kind=reply 评论）→ 落库 observation

另有两条独立端点（不走 /crawl）：
   POST /creator  → 某 UP 主基本信息 + 粉丝数 + 最近动态流（通用探针，标定用）
   POST /official → 某游戏**官方账号**在题目时段内的动态 + 各自评论（舆情专用，即模型面 crawl_official）
```

---

## 1. 关键词怎么进来的（入口层）

模型面参数：`game`（规范游戏名）+ `query`（**一个 3-6 字短关键词**，不写游戏名）。

`live.crawl_bili` 组词规则：
- 检索词 = **游戏名 + 关键词，去掉所有空格后直拼**。
- 若 query 里重复出现了游戏名，先剥掉（避免拼成「王者荣耀王者荣耀孙膑」）。
- **为什么要去空格**：B站搜索对空格敏感，带空格反而漏匹配；一长串（「男干员待遇强度争议」）只会命中泛话题热门视频，命中面很差。

---

## 2. 服务端（HTTP 层）

`crawlers/bili/bili_search_server.py`

- 常驻进程，监听 `127.0.0.1:8771`，`X-Token` 鉴权。
- 端点：
  - `GET /health` → `{ok, busy, queries_done, time}`
  - `POST /crawl` → 体 `{"query": 检索词, 可选 "since"/"until", 可选 "windows"}`
  - `POST /creator` → 体 `{"mid": 数字, 可选 "max_dynamics"(默认10), 可选 "debug"}`
  - `POST /official` → 体 `{"game": 游戏名, 可选 "since"/"until"}`
- **单飞**：同时只跑一个任务，忙时立刻回 `429`。
- 每 25 次 `/crawl` 把浏览器 context 的最新 cookie 同步回客户端。
- **不做自动重建浏览器 context**：B站登录靠配置里的 COOKIES 驱动，重建要重新过登录（有风控），收益小于风险；真崩了靠 systemd 兜底重启。
- 超时：普通 300s；分段窗 600s；`/creator` 240s；`/official` 600s（窗口按时间算，一个版本周期可能几十条动态、每条都要取评论）。

---

## 3. `POST /crawl`：视频搜索（主力）

单查询逻辑 `core.search_one_query`（`max_videos` 由服务端取 `BILI_MAX_VIDEOS`，默认 **6**）：

### 3.1 搜视频
- 调 `client.search_video_by_keyword`：`/x/web-interface/wbi/search/type`，`search_type=video`，`page=1`，`page_size=20`，`order=综合`。
  - **只搜第 1 页**（top 20），从里面取前 `max_videos` 个。
- **时间窗**（历史题/对比题）：带 `since`/`until` 或 `windows` 时改成按**发布时间**过滤搜（`pubtime_begin_s`/`pubtime_end_s`）——
  - 单窗：搜该时段一页，取前 `max_videos`。
  - 分段窗（对比题「今年 vs 去年」）：每段各搜一页，配额摊到各段（每段至少 2），按 aid 去重合并。合成单窗会被最新一截吃满、早那段全丢。
  - 半开窗兜底：缺起点取 B站上线日 2009-01-01，缺终点取今天。

### 3.2 取视频详情
逐视频调 `/x/web-interface/view/detail`，取标题、简介（截 200 字）、UP 主、发布时间、播放/弹幕/回复/点赞、自带标签。

### 3.3 取评论
逐视频调 `/x/v2/reply/wbi/main`（默认排序，`type=1` 视频），**只取第一页前 10 条一级评论**（`max_count=10` 达到即停，不翻页）；单视频评论 30s 封顶（风控退避不应拖垮整 query）。
- **不取二级评论**（`is_fetch_sub_comments=False`）。
- 实现在 `client.get_video_all_comments`。

---

## 4. `POST /official`：官号探针（舆情专用）

模型面工具 `crawl_official` 打这个端点。**这不是「取最近 N 条动态」，而是「取题目时间段内的动态」**：

- **账号解析** `_resolve_mid`：先查 `OFFICIAL_MIDS` 表（原神/明日方舟/明日方舟：终末地/崩坏：星穹铁道/鸣潮，含崩铁·星铁别名）；
  表里没有就按名搜 `bili_user`（**`/x/web-interface/wbi/search/type` + wbi 签名**——非 wbi 的旧端点会被风控甩 HTML），
  候选过 `_pick_official`：**只认 `official_verify.type==1`（机构认证）且 名字/认证描述含游戏名的号**，
  有多个则优先「归一后名字全等」、并列取粉丝最多；**一个官方候选都没有就拒答**（`ambiguous` + 候选名单，不瞎认号——认错=整题证据全废）。
  - 判据要点（2026-09-13 定，正源 PLAN §10.45）：**不看认证的「uname 全等」是旧 bug**（蹭名个人号会赢）；**不设粉丝硬门槛**（金铲铲之战官号仅 75 万粉）。
- **时段** `_day_bounds`：`since/until` = `YYYY-MM-DD`（题里的时间锚点）；
  since 缺省 = 近 `OFFICIAL_DEFAULT_DAYS=42` 天（约一个版本周期）；until 缺省 = 现在。
  解析不了的日期当没给（时段只是缩小范围，不该让整次探针失败）。
- **动态流** `get_creator_dynamics`：`/x/polymer/web-dynamic/v1/feed/space`，按发布时间**倒序翻页**，
  遇到早于 since 的动态即停（`window_reached=True`）；晚于 until 的跳过；时间缺失的照收。
  - 保险丝：`OFFICIAL_SCAN_MAX=300`（最多扫 300 条）+ `OFFICIAL_MAX_DYN=100`（窗内最多收 100 条）。**这俩不是口径，是防翻页/评论失控的保险丝**；真撞上返回体标 `truncated`。
- **高峰判定** `_peak_flags`：基准线取窗内评论数的**中位数**（不是均值——高峰自己会把均值抬上去，峰越大越认不出）；某条评论数 ≥ 基准线 `PEAK_FACTOR=3.0` 倍且 ≥ `PEAK_FLOOR=100` → 标 `is_peak`（疑似节奏发酵处）。
- **深采评论** `_dyn_comments`：高峰动态采 `CMT_PEAK=25` 条、平峰只 `CMT_NORMAL=5` 条，按热度序（`CMT_MODE=0`；实测 2=时间、1=综合返回 0 条不可用）。
  - **评论锚点按动态类型取不同 oid/type**（传错就是「啥都木有」）：图集→`draw.id`+type11 / 投稿→`archive.aid`+type1 / 其余（转发/纯文字/直播）→动态号+type17；且**不能带 pagination_str**。
- **动态正文提取** `_dyn`（有的类型没有正文，要兜底）：
  - 图集类上游**根本没有正文**（desc 键都不存在）→ 只记「[图集 N 张]」；
  - 直播推流类正文在 `major.live_rcmd.content` 的 JSON 串里 → 取标题拼「[直播] …」；
  - 投稿类标题在 `major.archive` → 取标题；纯转发看原文。
- **附属信息**：官号名称（`get_creator_info`）、粉丝数（`/x/relation/stat`）；任一失败只记错误，不整体崩。

---

## 5. 取回来的字段（一条样本 = 一个 observation）

**视频（`kind=post`）**：`bvid`、`title`、`desc`（简介）、`author`、`publish_time`、
`view_count`（播放）、`danmaku_count`（弹幕）、`reply_count`（回复）、`like_count`（点赞）、`video_tags`（自带标签）、`url`
**评论（`kind=reply`）**：`content`、`author`、`rpid`、`like`（点赞）、`time`
**官号动态（`kind=official_post`）**：`id`、类型、发布时间、正文、`like`/`comment`/`forward`、投稿视频的 `bvid`/`play`
**官号评论（`kind=official_reply`）**：作者、正文、点赞、时间（title 里带「哪条动态 + 是不是高峰」，落库后按时间倒序也不串味）
**响应体**：`/crawl` → `{query, time_windows, videos[]}`；`/official` → `{game, window_since, window_until, scanned, window_reached, truncated, dynamics_count, baseline_comments, peak_count, follower, dynamics[]}`

> **量化字段是 B站 的强项**：播放量、弹幕数、回复数、点赞数、视频自带标签——采集当时快照，可当「热度/声量」论据、可供下游画图表。
> 字段为 `null` 表示该条平台没这个数（老归档行也没补录），是「无此数据」不是 0。

---

## 6. 能回答什么 / 答不了什么

**能**：
- B站当下的风评声音、视频标题/简介里的话、评论区热评态度；
- **播放量/弹幕/回复/点赞**作为热度论据（**弹幕数**有，但弹幕**内容**没有）；
- 视频自带标签（可判断视频归类）；
- 带时间锚点的历史视频（有 `pubtime` 窗）；
- **官号探针**：官方账号在某时段发了什么（动态正文）+ 评论区态度，按评论量找**节奏高峰**（仅限账号表里的 6 个游戏）。

**答不了（固有的，不是 bug）**：
- **弹幕内容**——只取弹幕数，不取弹幕文字（「弹幕都在刷什么」这类题取不到）。
- **视频正文/字幕/口播内容**——只拿标题+简介，拿不到视频里讲了什么（攻略视频的干货在视频里，取不到）。
- **评论全量**——每视频最多 10 条一级评论、默认排序；取不到「第 50 条评论」或二级评论。
- **历史评论**——按热度/默认序取「现在」的前 10 条，不能按时间筛「去年的评论」。
- **账号表外的游戏官号**——`OFFICIAL_MIDS` 里没有的游戏，按名搜有歧义就报错（不瞎认）。
- **官方公告的明细文本**——很多动态正文是图集/短链，公告细节在图片或跳转链接里，探针取不到。
- **B站之外的平台**（抖音/小红书/微博/知乎/贴吧）——本项目只接了 NGA + B站。

---

## 7. 关键参数速查

| 参数 | 值 | 位置 | 作用 |
|---|---|---|---|
| `BILI_MAX_VIDEOS` | 6 | server | 每 query 取前几个视频 |
| `page_size` | 20 | core | 搜索每页返回数（**只搜第 1 页**） |
| 评论条数 | 10 | core | 每视频一级评论上限（`max_count=10`） |
| 评论超时 | 30s | core | 单视频评论爬取封顶 |
| `OFFICIAL_DEFAULT_DAYS` | 42 | server | 官号探针无锚点时的默认窗口（≈一个版本周期） |
| `OFFICIAL_MAX_DYN` | 100 | server | 窗内动态收集保险丝 |
| `OFFICIAL_SCAN_MAX` | 300 | server | 翻页扫描总量保险丝 |
| `PEAK_FACTOR` / `PEAK_FLOOR` | 3.0 / 100 | server | 高峰判定（≥基准线 3 倍 且 ≥100 条） |
| `CMT_PEAK` / `CMT_NORMAL` | 25 / 5 | server | 高峰/平峰动态各采几条评论 |
| `CMT_MODE` | 0 | server | 评论排序（0/3=热度，2=时间，1=不可用） |
| `COOKIE_REFRESH_EVERY` | 25 | server | 每 N 次查询同步一次 cookie |
| 端口 | 8771 | server | 常驻服务端口 |
| 超时 | 300s / 分段窗 600s / creator 240s / official 600s | server | 各端点超时 |

---

## 8. 部署与风控（重要）

- 服务器上以 systemd 常驻（`/opt/<bili-crawler-copy>` 副本，**毕设原版 `/opt/<bili-crawler-copy>` 之外一行不动**）。
- **登录态是服务器唯一 owner**：靠 `config.COOKIES` + 浏览器 profile 驱动，**绝不拷回本地/仓库**；游客态取动态必 412，全靠登录态。
- 风控与退避：
  - API 层 `request`：5 次重试；遇到「降级过滤」这类拦截 **暂停 30 分钟**再重试。
  - 引擎层（`harness/engine.py`）：跨轮/跨 ask 的 B站最小间距 `BILI_GAP` 快速 60s / 精准 120s；**同一轮内同平台**多次短词调用只留 `SAME_ROUND_GAP=12s`，且服务端单飞、同平台串行（并发会被 429 挡回）。一次 CrawlError 熔断该平台本 ask，并叠会话冷却（5min 起、连熔断翻倍封顶 15min，成功归零）。**NGA 只熔断、不冷却**。
- 本地经 ssh 隧道 `-L 18771:127.0.0.1:8771` 访问；harness 用 `BILI_HTTP`（默认 `http://127.0.0.1:18771`）。
- 错误语义（`live` 层）：传输失败/超时/429 → **抛 CrawlError**（超时/429 打 `throttle` 标记供引擎退避）；**空列表 = 真没搜到**。
  官号探针另有：账号没认准 / 窗内一条动态都没翻到 → CrawlError（`throttle=False`，**不算风控事件、不熔断平台**）；只有传输超时/429 才 `throttle=True`。
