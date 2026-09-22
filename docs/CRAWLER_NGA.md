# NGA 爬虫说明书（从关键词进到样本出，全功能）

> 定位：NGA 侧负责「游戏论坛里玩家怎么聊」。它用关键词在论坛里找**帖子**（标题命中为主、正文兜底），
> 再把命中帖的**主楼正文** + **最多 8 条热门回复**取回来，落成一条条样本（observation）供作答引用。
> 代码分层：模型面工具 `crawl_nga`（`harness/tools/crawl_live.py`）→ 取数器 `harness/sources/live.py`
> → 服务器 `crawlers/nga/nga_search_server.py` → 爬取逻辑 `crawlers/nga/nga_search_demo.py`
> → 浏览器/登录基座 `crawlers/nga/nga_crawler_playwright.py`。

---

## 0. 完整链路（关键词 → 样本）

```
模型给出 {game: 游戏名, query: 短关键词}
   │
   ├─ 引擎侧组词  live._nga_key(game, query)      → 检索串 "模式|游戏名|词"
   │
   ├─ POST /crawl 到常驻服务（端口 8770，X-Token 鉴权，单飞）
   │
   ├─ 服务端 run_query(line, since, until, windows)
   │     ├─ 解析模式（board / search / searchin）
   │     ├─ 定位主板 fid（动态、无硬编码表）
   │     ├─ 检索（有窗走翻页导航；无窗取最新一页）
   │     └─ 逐帖进详情，取正文 + 热评
   │
   └─ 返回 {query, mode, board_fid, time_window, window_capped, posts:[…]}
         │
         └─ harness 转成 items（kind=post 主楼 / kind=reply 热评）→ 落库 observation
```

---

## 1. 关键词怎么进来的（入口层）

模型面参数只有两个：`game`（规范游戏名）与 `query`（**一个 3-6 字短关键词**，不写游戏名）。
引擎把它们拼成 NGA 能认的检索串，规则在 `live._nga_key`：

| 情况 | 检索串 | 含义 |
|---|---|---|
| 有游戏名 + 有话题词 | `searchin\|游戏名\|话题词` | 先定该游戏主板，再在版内按词搜（最常用） |
| 只有游戏名（query 空） | `board\|游戏名` | 拉该游戏主板的最新帖（笼统问） |
| 只有话题词、没游戏名 | `search\|话题词` | 全站搜（容易跨游戏漂移，兜底用） |

要点：
- query 里若重复出现了游戏名，会先被剥掉，避免拼成「明日方舟明日方舟…」。
- query 里的多个词按标点/空白拆成多个 token。
- **一次调用只发一个检索串**。所谓「同一平台一轮内发多个短词」，是引擎连发多次调用实现的——每次仍是一个短词，不是把整句塞进一次 query。

---

## 2. 服务端（HTTP 层）

`crawlers/nga/nga_search_server.py`

- 常驻进程，监听 `127.0.0.1:8770`，所有请求要带 `X-Token` 头（token 在服务器本地文件，不外传）。
- 端点：
  - `GET /health` → `{ok, busy, queries_done, time}`
  - `POST /crawl` → 体 `{"query": "检索串", 可选 "since"/"until": "YYYY-MM-DD", 可选 "windows": [["起","止"], …]}`
- **单飞**：同一时刻只跑一个查询，忙时立刻回 `429`（不排队）；这个 429 会被 harness 当成「平台忙」短重试。
- 请求经由后台线程投递到持有浏览器的 asyncio 主循环执行；**每 25 次查询重建一次浏览器 context**（防长跑内存累积/脏状态）。
- 超时：普通 300s；带分段窗（对比题要翻两段）放宽到 600s。

---

## 3. 检索模式（三选一，由检索串前缀决定）

实现在 `nga_search_demo.run_query`：

| 模式 | 检索串 | 行为 | 取帖上限 |
|---|---|---|---|
| `board` | `board\|游戏名` | 动态找该游戏主板，拉主板最新帖 | `BOARD_N = 20` |
| `search` | `search\|词` | 全站关键词搜 | `SEARCH_N = 10` |
| `searchin` | `searchin\|游戏名\|词` | 先定主板 fid，再在**版内**按词搜 | `SEARCH_N = 10` |

所有列表来自 `thread.php` 的 `__output=11`（JSON，比抓 HTML 快），关键接口：
- 全站搜：`/thread.php?key=词&page=N&__output=11`
- 版面列表：`/thread.php?fid=板&page=N&__output=11`
- 版内搜：`/thread.php?fid=板&key=词&page=N&__output=11`（实测认 fid+key 交集，只看**标题**）

### 3.1 版面定位（没有硬编码映射表，动态解析）

`_resolve_board_fid(game)` 两段式：

1. **独占正板优先**：全站搜游戏名 1-4 页，统计返回帖的主流 `fid`。
   - 先看「版名与游戏名**完全同名**」的板——这是最强信号，不受占比门限。
     （原因：泛用综合板会把真主板的占比稀释到 0.30 以下；实测"原神"全站搜里综合板 428 占 0.30 > 主板 650 占 0.20，看着像没有专属板，其实有。同名判定一举解掉。）
   - 同名不成，退「版名含游戏名且占比 ≥ `BOARD_CONF_MIN_RATIO=0.30`」（命中数需 ≥ `BOARD_CONF_MIN_HITS=8`）。
2. **版区聚合锚**（多子版游戏）：有些游戏的帖子散在各子版、子版名又不带游戏名（如明日方舟本体 = 版区 `-34587507`「罗德岛大使馆」下辖问答室/酒吧/图书馆…）。这时从候选板的面包屑里取「标题含游戏名的负 fid 版区聚合页」当锚——版内搜在其下可跨子版命中。

定位失败时：
- `searchin` 无专属板 → 退「游戏名+词」全站兜底（不裸搜词，防跨游戏漂移），并标记 `board_fid=None`（提示调用方：证据零散、引用谨慎）。
- `board` 无板 → 退全站搜热门帖。

### 3.2 版内逐词搜 + 正文兜底

- `_board_search_tokens`：把 query 拆成单 token，**每词各发一次版内 title 搜**，按 tid 合并去重。
  **不做多词空格 AND**——NGA 版内搜对空格是「标题全词与」，多泛词整串几乎必然 0；单短词（德玛/盖伦/长草）才最可能落标题。
- 版内 title 搜**全 0 命中**时 → `_board_body_fallback`：拉主板最近 `FALLBACK_BOARD_PAGES=2` 页帖，标题含词的直接收；其余候选进详情 grep 正文，命中才收（进详情封顶 `FALLBACK_DETAIL_MAX=15` 帖，防 429/慢）。
  → 这层是为了「长草/产能/复刻/节奏」这类**只出现在正文、不进标题**的词。

---

## 4. 时间窗导航（历史题 / 时效题 / 对比题）

无窗时直接取最新一页；**有窗**时走 `_page_to_items` 翻页导航：

- 从第 1 页逐页往回翻：整页最早末回时间 < 窗口下界 → 停（确认到头）；空页（NGA 回 HTTP 410）当到底。
  之所以能这么做：版面列表、版内搜、全站搜三个接口的返回页**都按「末回时间」降序**，翻页即时间轴。
- **双闸**（先到先停）：`max_pages`（版内搜 40 页≈翻一年 / 全站搜 25 页）+ `budget_s`（120s）。
  闸不能小：版内搜一页只跨 2-4 周，翻一年要 ~20 页；闸小了会在窗口之前被截断，把「0 命中」误读成「那会儿没讨论」。
- 被闸截断（没确认翻到窗口起点）→ 返回体标 `window_capped=True`，提醒模型：**0 命中 ≠ 那会儿没讨论**。
- 命中判定 `_in_window`：帖的**发帖时间**或**末回时间**任一落在窗内即算命中。
- **分段窗**（对比题「今年 vs 去年」）：`windows=[[起,止], …]`，每段各翻一窗，按 tid 合并去重。
  合成一个大窗会因翻页按末回降序而**只取到最新一截**，早那段全丢——所以要分段。

---

## 5. 帖子详情（正文 + 热评）

`_parse_detail` 用 playwright 渲染后取 DOM（不是抓接口）：

- **正文**：`#postcontent0` 或 `.postcontent` 的纯文本。
- **热评**：`div.comment_c` 逐条取——
  - 作者：`a.userlink`（剥掉等级首字徽章）
  - 内容：`[id^=postcomment__]`（去掉「Reply to…」引用头、去掉末尾「……[原帖]」）
  - 时间：`span[title="reply time"]` / `.postdatec`
  - 点赞：`.recommendvalue`
  - **上限 `HOT_REPLIES_N = 8` 条**，按页面给的热度序，空内容丢弃。
- 帖间 `wait_for_timeout(400 + tid%600)` ms 防节流（实测 0.4s+ 够）。

> 注意：原版毕设的 `_extract_hot_replies` 在真实 DOM 里提 0 条（它找 `.ubbcode/.thumbsup`，那是纯 JS 才有的）；
> 本 demo 改成取 playwright 渲染后的 DOM，才拿得到真用户名/时间/点赞。

---

## 6. 取回来的字段（一条样本 = 一个 observation）

**主楼（`kind=post`）**：`tid`、`fid`、`title`、`author`、`reply_count`（回复数）、`post_time`、`url`、`content`（正文）
**热评（`kind=reply`）**：`author`、`content`、`time`、`like`（点赞）、挂在主帖 `tid` 下（raw_id = `tid_h序`）

**服务端返回体**：`query`、`mode`、`board_fid`、`time_window{since,until,windows}`、`window_capped`、`posts[]`

harness 侧（`live._nga_items`）再转成统一 observation 形状，量化字段：主楼带 `reply_count`，热评带 `like_count`。
（NGA 无播放量/弹幕/自带标签——那些是 B站 独有字段。）

---

## 7. 落地前的过滤

- **已删/过期帖**：标题以「帖子发布或回复时间超过限制」开头，或发帖时间 `1970` → 丢（`live._nga_items`）。
- **噪声作者**：`admin` / `#SYSTEM#` / `#ANONYMOUS#` → 丢（`_keep_item`）。
- **正板归属**：正 fid 板内搜时要求帖 `fid == 主板fid`；负 fid（版区聚合锚）不做此校验，免得把区内子版的帖误删。
- **兄弟作污染**：检索词越界到同 IP 衍生作（如「明日方舟终末地」跑到「明日方舟」题里）→ 引擎在落库时丢弃样本，并在返回里说明拦了几条。

---

## 8. 能回答什么 / 答不了什么

**能**：
- 论坛当下的讨论焦点、帖子怎么说、热评态度；
- **回复数**作为热度论据；主楼正文里的原话可 `[id=N]` 引用；
- 带时间锚点的历史时段讨论（有窗翻页）；
- 机制 / 强度 / 节奏这类口水战（论坛最擅长的就是吵这个）。

**答不了（固有的，不是 bug）**：
- **播放量 / 弹幕 / 视频**这类 B站独有数据——NGA 没有这些字段。
- **帖子的全部回复**——只有最多 8 条热评，**不是全量楼层**；想问「这帖第 50 楼说了啥」取不到。
- **图鉴/维基式的精确数值**——NGA 是论坛不是数据站，数值只能靠玩家在帖里写的。
- **冷门游戏无专属板**时证据会很零散（全站兜底命中面窄），且可能被标 `board_fid=None`。
- 帖子被删/过期就取不到（NGA 自己的时限）。

---

## 9. 关键参数速查

| 参数 | 值 | 位置 | 作用 |
|---|---|---|---|
| `SEARCH_N` | 10 | demo | search / searchin 模式取帖上限 |
| `BOARD_N` | 20 | demo | board 模式取帖上限 |
| `HOT_REPLIES_N` | 8 | demo | 每帖保留的热评条数（硬上限） |
| `BOARD_CONF_MIN_HITS` | 8 | demo | 判专属板的最少命中数 |
| `BOARD_CONF_MIN_RATIO` | 0.30 | demo | 判专属板的占比下限 |
| `FALLBACK_BOARD_PAGES` | 2 | demo | 正文兜底拉主板最近几页 |
| `FALLBACK_DETAIL_MAX` | 15 | demo | 正文兜底最多进几帖详情 |
| `max_pages` | 版内 40 / 全站 25 | demo | 翻页导航页数闸（≈一年） |
| `budget_s` | 120 | demo | 翻页导航时间闸（秒） |
| `RECYCLE_EVERY` | 25 | server | 每 N 次查询重建浏览器 context |
| 端口 | 8770 | server | 常驻服务端口 |
| 超时 | 300s / 分段窗 600s | server | 单次查询超时 |

---

## 10. 部署与调用

- 服务器上以 systemd 常驻（`/opt/<nga-crawler-copy>` 副本，**毕设原版 `<原版爬虫目录>` 一行不动**）。
- 本地经 ssh 隧道 `-L 18770:127.0.0.1:8770` 访问；harness 用 `NGA_HTTP`（默认 `http://127.0.0.1:18770`）指向隧道口。
- 登录态：服务器本地 `config.json` 的 cookies 注入浏览器 context；harness 只读 token 文件，不碰 cookie。
- 错误语义（`live` 层）：服务/传输失败 **抛 CrawlError**（引擎据此熔断该平台）；**空列表 = 真没搜到**（平台是好的）——引擎靠这个区分「平台坏了」和「社区没这个料」。
