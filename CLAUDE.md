# CLAUDE.md — 游戏社区问答 Agent（game-harness）

本仓库 = 中文问句 → 冷启动判断 → 按需真爬社区（NGA+bili，关键词）→ 自动存档 → 检索合成 → **引原文作答**（`[id=N]` + 明说不确定性 + 末行「结论:」）的 harness。DeepSeek function-calling 循环在 `harness/engine.py`。

## 开机协议（新会话必做，做完再动代码）
1. 读 `docs/mem/RECALL.md`（话题索引，短）——后续命中关键词即跳正源。
2. 读 `docs/mem/HANDOFF.md` **尾部「现态」** → 继续上次任务 / 查阻塞。
3. 读 `PLAN.md` §10 尾部（决策日志最新段）+ `PROJECT_MEMORY.md`（一页锚点）。
4. 开工前过 `docs/mem/user-habits.md` 与 `PROJECT_MEMORY.md §5 铁律`。

## 正源链（权威顺序，冲突时以上为准）
代码 > git > `PLAN.md §10`（决策日志）> `docs/`（PRD/ARCHITECTURE/TECH_SELECTION）> `PROJECT_MEMORY.md`。本文档及 topics/ 复述只作入口。

## 记忆地图
| 文件 | 角色 | 时机 |
|---|---|---|
| 本 CLAUDE.md | 开机协议 | 每次会话自动加载 |
| `PROJECT_MEMORY.md` | 一页全貌锚点（量化事实/铁律/待办） | 每次会话 |
| `PLAN.md §10` | 决策时间线正源 | 每次会话读尾 |
| `docs/mem/HANDOFF.md` | 现态 / 进行中 / 待拍 | 每次会话 + Stop 钩子提醒 |
| `docs/mem/RECALL.md` | 关键词→话题索引 | 每次会话 |
| `docs/mem/user-habits.md` | 协作个人规则 | 开工前 |
| `docs/mem/topics/*.md` | 跨主题结论+教训（真源在别处） | 命中话题时 |
| harness scope DB（`harness/data/harness_dev.db`） | 会话级记忆 L1 StateCard / L3 archive | 运行期 |

## 关键数字速记（正源 §10.4 + §10.33 / PROJECT_MEMORY §2，别拍新值）
快速/精准：参考量 `SAMPLE_TARGET 120/500`（软，不硬停）、爬轮硬上限 `CRAWL_BUDGET 3/5`（按「轮」：同轮 NGA+bili 各爬一次=1，**同一平台一轮内发多个短词仍只算 1 轮**，见 §10.37）、`BILI_GAP 60/120`（跨轮/跨 ask；**同轮同平台只留 `SAME_ROUND_GAP 12`**）、冷却 `COOL 300→900 成功归零`、护栏 `IN_CEILING 160000/320000`（09-12 重定档：爬到的证据**全量**进上下文、不再按 5/15 条截断，见 §10.33）；反讽可疑门 `0.6`、judge 导流 `0.4 cap 40`。09-08：平台不二选一（每轮双平台各爬一次），46开/MIN_SHARE 已删。模型工具面 = {crawl_nga, crawl_bili, search_archive}（两 crawl 独立、平台由注册表固定；**跨平台并发、同平台串行**、落库单写）。`observation` 量化热度列 `view_count/like_count/reply_count/danmaku_count/video_tags` 可空（老行无值；图表/论据用，见 §10.37）。

## 铁律速记（详见 PROJECT_MEMORY §5）
毕设原版一行不动（改 <NGA爬虫目录>、<B站爬虫目录> 副本）；凭证不落地、bili 登录态服务器唯一 owner；采集量级先对齐；数字不编、可溯源；不 git push；评测不达标 nlp 只软信号；live 标定每题间隔 5-10 分钟。

## 定位一句话（已拍 2026-09-06 23:05）
**游戏社区问答 Agent（舆情 = 旗舰纵深，不是全貌）**；毕设大屏是「监测类」，这是「问答类」。答案 = 社区当下口径非攻略站权威，必须 cite + 标滞后。
