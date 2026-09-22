# 引擎闸门 / 上下文治理

真源：`PLAN.md §10.2 / §10.4`；`harness/engine.py`；`PROJECT_MEMORY.md §2`。改动 engine 前先读。

## 常量正源（§10.4，别拍新值；09-08 语义改：预算按轮、达标软参考、46开删）
| 常量 | 快速 | 精准 |
|---|---|---|
| `CRAWL_BUDGET` 爬轮硬上限（按「轮」：同轮 NGA+bili=1） | 3 | 5 |
| `SAMPLE_TARGET` 参考样本量（软，不硬停） | 120 | 500 |
| `BILI_GAP` B站爬距 | 60s | 120s |
| `COOL` 冷却 | 300→900s，成功归零 | 同左 |
| `IN_CEILING` 护栏 | 160000 | 320000 |
| ~~`MIN_SHARE` 46开~~ 已删 | — | — |

## 引擎主循环闸门（09-08 改版）
① 爬轮预算 CRAWL_BUDGET 硬上限、按「轮」计（一条 assistant 消息含 ≥1 **成功**真爬 = 1 爬轮；同轮 crawl_nga+crawl_bili 双平台各爬一次 = 1；用尽 block 强制收口；全被平台闸门挡(熔断/冷却/真爬失败)的轮不耗预算）；② 达标不硬停：SAMPLE_TARGET 只作参考量，每次真爬返回 `ask_total` 供 LLM 比对自判；③ 平台状态机：ask_fail 断路器 + bili 会话冷却(300→900 归零) + bili 爬距(BILI_GAP 键在真实请求时刻)；④ 上下文护栏。46开/MIN_SHARE/`_balanced`/`_split_str` 硬 gate 已删；快速/精准两套 `_base_system` prompt 分开。验证 `_tmp/verify_budget_rounds.py` 10/10（脚本内工具名已随拆分改为 crawl_nga/crawl_bili，未重跑）。

## 工具拆分 + 并发（09-08，待功能验证）
单 crawl_live → **crawl_nga / crawl_bili 两独立工具**（`tools/crawl_live.py` CRAWLERS 注册表 → engine `TOOL_DEFS`；平台由注册表固定，模型不再传 platform 参数）。工具实现拆两段：`fetch`（只调 provider 抓 items，不碰 DB，可并发，CrawlError 上抛）→ `commit`（`archive.record_crawl` 落库 + 组 summary/evidence）。engine 同一轮内对通过 `_precheck` 的 crawl 用 `ThreadPoolExecutor` 并发 fetch，落库/`_plats` 计数/熔断/冷却/翻篇全回主线程串行 —— sqlite conn 单写不跨线程。`_precheck`(断路器/冷却/bili 爬距) 在调度前主线程顺序预检；`_mark_fail`(真爬抛错 → 熔断+冷却)；`_after_crawl`(成功 → 计数+ask_total)。预算仍按轮（成功轮末 +1）。工具返回按原 tool_call 顺序回填（DeepSeek 需一一对应）。

## 上下文治理（9/7 定论）
**单 ask 证据保真不折叠**（折叠断 `[id=N]` 链）；护栏只收异常风暴；省 token 的正解 = 少绕圈（轮数），不是截原文。`harness/context.py`（cl100k 计量）**已写未接线**，待升组件。

## DSML 泄漏（9/8 根治，单测 22/22）
症状：强制收口时模型仍处搜索模式，tools 禁用后仍写"预算用完去 search_archive"散文+XML 当答案、无「结论:」行。
修法：`_scrub` 删工具标签(含孤儿/未闭合)；`_final_answer`（无干净结论 → 失败草稿回喂强制重写一次）；`_conclusion`/`_tool_intent`/`_drop_empty_concl`；`_materialize` 兜底改诚实占位"(未收口)"，绝不拿残句当历史结论。

## 平台占比入参（09-09，正源 PLAN §10.12-§10.14）
单标量 `nga_ratio` = NGA 占比%，余量=bili（100=只NGA / 0=只bili / 50≈现状双平台都爬）。语义 A=样本证据配比。三态：**端点 0/100 硬压**（`_precheck` 顶部：`>=100` 禁 crawl_bili、`<=0` 禁 crawl_nga，返回 platform_error 让模型转边；熔断/冷却仍在各自下方生效）；**内部 1..99 软引导**（`_ratio_nudge()` 每轮算本 ask NGA/bili 累计新增占比，偏离目标 >`RATIO_TOL`=5 且欠配边未熔断/冷却才追一句系统提示让模型下轮优先补，只提示不改调度）；熔断/冷却永远优先于比例。入参链：`calibrate --nga-ratio` / `Session.nga_ratio` → `engine.ask(nga_ratio=)` → `clamp_ratio` 钳制（非法回落 50）。留痕 = flow trace `ratio_nudge` 事件 + 报告 header 平台新增计数，不加 DB 列。

## append-only 守卫（09-09，正源 PLAN §10.16 / ARCHITECTURE §5.1）
`harness/guard.py` 把"数据层只增不改"固化成可执行检查：静态扫 harness/*.py，命中即列违规 exit 1 —— 破坏性文件原语（os.remove/unlink/rmdir/rename/replace/truncate、shutil.rmtree/move/copy*、.unlink/.truncate/.write_text/.write_bytes）、破坏性 SQL（DELETE/DROP/TRUNCATE/UPDATE..SET）、`open(...)` 写模式（首字母 w = 截断/覆盖；r/a/x 放行）。guard 自身豁免。运行 `python -m harness.guard`（改 engine 后跑当回归）；自检 `_tmp/verify_append_only_guard.py` 14/14。可删性语义：会话层(历史/StateCard/session_log/flow)可软删(tombstone+7天)，证据层(L3 archive+报告)禁删。

## 同轮双 bili 并发写坏 bili 侧（09-12 实测，待修）
**症状**：对比题同一轮内模型发两个 `crawl_bili`（q12 两游戏对比，trace 第 12/13 条就是同轮双 bili）→ 后到者撞 bili-tool 单飞锁报 429 → 该 ask 剩余轮次 bili 侧连续失败（q12 第 8、12、13、22 条 `blocked: bilibili crawl fail: busy 重试 3 次仍 429`；前 3 轮 bili 还是正常的 run#74/76/78）。
**机制**：`engine.py` `_precheck` 的 bili 爬距只在**预检阶段** `time.sleep`，两个 bili 请求随后仍一起进 `ThreadPoolExecutor` 并发下发 → 撞服务端单飞；`live.py` `_post` 对 429 只重试 3×12s≈36s，单次长爬取超过 36s 即永久失败，且 `throttle=True` 把它记成"风控退避"。原注释自认「同轮罕见双 bili 不额外串, 接受」—— 对比题让"罕见"变常态。
**修法方向（未实施，批在跑不动码）**：同轮同平台只放行一个（第二个 defer 到下一轮，比并发更贴单飞语义），或 bili 真改成主线程串行 + 单飞重试预算放大到长于爬取耗时。回归看 `_tmp/verify_budget_rounds.py` 与 `_tmp/chk_q12_trace.py` 那类同轮双 bili 场景。

## search_archive 的 query 是字面子串（09-12 实测，待修）
`archive._where` 把 `words` 里**每个元素**各拼一个 `(title LIKE %w% OR text LIKE %w%)` 再用 `OR` 相连 —— 不做分词、不做多词与。模型按自然语言传 `query` 时整串当一个子串匹配，**静默 0 命中**（q12 第 17/18 条 `total=0`；同 scope 同 game 无词过滤 = 104 条、词「终末地」= 25 条、词「终末地 绝区零 对比」= 0 条）。工具 schema 里 `query` 只写 `{"type":"string"}`，没说它是子串。修法方向：schema 说明写成"单个关键词"，或服务端对多词拆开做 AND。
