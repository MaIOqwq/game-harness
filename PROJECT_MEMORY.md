# 项目记忆：游戏社区问答 Agent（game-harness）

> 2026-09-07 精简重排版。**正源链**：代码 > `PLAN.md`（§10 决策脉络日志）> `docs/` 三件套（正式文档）。
> 本文件只保留"一页看清全貌"的锚点，细枝一律指向正源。路径以 `<项目根>` 为准（2026-09-07 迁址，旧 `<本机目录>\opinion-agent` 已废）。

---

## 0. 这是什么

游戏社区（NGA + 哔哩哔哩）**问答 Agent / harness**：中文问句 → 冷启动判断 → 按需真爬社区（关键词）→ 原文自动存档 → 检索合成 → **引原文作答**（逐条 `[id=N]` + 明说不确定性 + 末行结论）。
**对外定位（已拍 2026-09-06 23:05）：游戏社区问答 Agent（舆情 = 旗舰纵深，不是全貌）**。
毕设大屏是「监测类」，本 harness 是「问答类」，两者干净区分。

**一句话架构**：DeepSeek function-calling 循环（`harness/engine.py`）编排；模型可见工具面收窄 = `{crawl_nga, crawl_bili, search_archive}`（两 crawl 工具独立、平台由注册表固定；同轮并发抓取、落库单写）；爬取自动落 L3 存档（落档是副作用，非模型决定）；materialize-then-prune 记忆；双模式用户自选（快速/精准）。

## 1. 文档与正源地图

| 文件 | 职责 |
|---|---|
| `PLAN.md` | 施工主计划 + **§10 决策脉络日志（逐日/快照/常量正源/遗留，权威）** |
| `docs/PRD.md` | 产品需求（背景/边界/FR-NFR/数据需求/验收/打开问题） |
| `docs/ARCHITECTURE.md` | 架构设计（原则 P1-P6/分层/时序/记忆 L1-L4/门控/组件映射/演进） |
| `docs/TECH_SELECTION.md` | 技术选型对比 D1-D9（独立论证，含触发更换条件） |
| `PROJECT_MEMORY.md` | 本文件（一页锚点） |
| `CLAUDE.md` | 开机协议（新会话先读 docs/mem 再动代码） |
| `docs/mem/RECALL.md` | 关键词→话题索引（正源在 PLAN/docs/本文件） |
| `docs/mem/HANDOFF.md` | 现态 / 进行中 / 待拍（Stop 钩子维护 last-msg.md） |
| `docs/mem/user-habits.md` | 协作个人规则（沟通/量级/记忆纪律） |
| `docs/mem/topics/*.md` | 跨主题结论+教训（memory-scope/crawler/labeling/engine/process） |

**身份分层一句话**：①通用中段（攻略/配队/入坑/近期动态，冷启动+搜索就覆盖，几乎不加专用工具）；②舆情硬尾（黑话解码/反串阴阳/自基线爆发/带节奏溯源——才需要专用 determiner = 护城河）。诚实边界 = 答案是**社区当下口径**非攻略站权威，必须 cite + 标滞后。
冷启动 probe 失败在硬尾（"膨胀神游" referent 不稳），不是中段；攻略配队类冷启动实证可答。

## 2. 关键量化事实（防口径漂移，代码正源）

### 引擎门控常量（`harness/engine.py`，09-08 改：平台不二选一、预算按"轮"、达标不硬停）
| 常量 | 快速 | 精准 |
|---|---|---|
| `SAMPLE_TARGET` 参考样本量（软，不硬停） | 120 | 500 |
| `CRAWL_BUDGET` 爬轮硬上限（同轮 NGA+bili=1 轮） | 3 | 5 |
| `BILI_GAP` B站爬距 | 60s | 120s |
| `COOL` 冷却 | 300→900s，成功归零 | 同左 |
| `IN_CEILING` 上下文护栏 | 160000 | 320000 |
| ~~`MIN_SHARE` 46开~~ | ~~0.4~~ | — 已删：两侧收量天然不均，只作答如实注偏 |

> **09-08 引擎闸门改版（B）**：`BASE_SYSTEM` 拆成快速/精准两套 `_base_system(mode,...)`（前端可选项，行为不同）；平台由"二选一"改"同一轮 NGA+bili 各爬一次（算 1 爬轮）"；预算按「轮」计（一条 assistant 消息含 ≥1 真爬 = 1 爬轮，用尽 block；全被平台闸门挡的轮不耗预算）；达标不再硬停，`SAMPLE_TARGET` 只作参考量，每次真爬返回 `ask_total` 供 LLM 每轮比对自判；46开/`MIN_SHARE`/`_balanced`/`_split_str` 硬 gate 删除。平台状态机（断路器 + bili 会话冷却 + bili 爬距）保留。验证 `_tmp/verify_budget_rounds.py` 10/10。

- **NGA 检索语义 + 主板解析（09-09 三连修，正源 PLAN §10.11）**：NGA fid 结构 = **正 fid 可发帖正板**（金铲铲510461 属 LOL 负区下；终末地846 独立正板）/ **负 fid = 版区聚合页**（明日方舟本体 -34587507「大使馆」下辖多子版，`fid=负&key=长草` 跨子版命中）。版内搜只匹**标题**且空格=AND → searchin 落地 = 先 `_resolve_board_fid` 两段式（①独占正板占比≥0.30 直接返回；②不足 = 多子版游戏取标题含游戏名的负版区作锚）+ 逐词单搜合并 + 0 命中时正文兜底 `_board_body_fallback`（主板最近 2 页、正文 grep、详情封顶 15）。真打：金铲铲/明日方舟「长草 产能 复刻 节奏」均 0→10 帖。过滤 helper `_keep_item`：admin/#SYSTEM# 砍；**fid 相等校验只对正板强制**。

- **平台比例旋钮入参（09-09，正源 PLAN §10.12/§10.13）**：前端将设旋钮让用户选**平台比例**（已入 PRD FR1/§6/#8），语义 A=样本证据配比；口径 = 单标量 **NGA 占比%**（100=只NGA / 0=只bili / 50=均等≈现状），旋钮将来直接映射。落地两段：阶段一穿针 `calibrate --nga-ratio 0..100`（默认50）→ `Session.nga_ratio` → `engine.ask(...nga_ratio)` → `clamp_ratio` 落 `self._nga_ratio`；**阶段二端点硬压已同日落地**：`_precheck` 顶部 `>=100` 禁 `crawl_bili` / `<=0` 禁 `crawl_nga`（熔断/冷却仍在各自下方生效），`ask()` 单平台前置提示。B 实证 = `nga_ratio=100` live 补跑 q8绝区零（42 obs）/q9崩铁（90 obs）**全程只 NGA**、verifier OK、崩铁 10/10 引用全 NGA → NGA 修复泛化成立。**内部 1..99 软引导已落地（09-09 §10.14，用户拍 option1 误差≤5%）**：`RATIO_TOL=5` + `_ratio_nudge()` 每轮算本 ask NGA/bili 累计新增占比，偏离 >5 且欠配边未熔断/冷却才提示模型下轮优先补（只提示不改调度）；留痕 = flow trace `ratio_nudge` 事件。验证：单元 8/8 + mock ratio=70 全链 ratio_nudge ×2。最终语义 = 0 只bili/100 只NGA 端点硬压，1..99 软 ±5，50≈现状。

- **数据可删性契约（09-09，正源 ARCHITECTURE §5.1 / PLAN §10.15）**：harness/*.py 全仓**零删除原语**（append-only 数据层，误删不可能来自 harness 自身）。用户要防的场景 = 将来「清除所有对话记忆」按钮 → 契约 = **会话层可删（软删 tombstone `deleted_at` + 7 天保留可恢复）/ 证据层禁删**（L3 archive observation·crawl_run + 报告，`[id=N]` 引用正源，删除入口在数据层够不着）。删除 API 单点 + scope+kind∈{conversation} + protected 禁删标记 + `--dry-run` + 审计留痕；前端按钮入 PRD FR9/FR1/§8#9。**守卫已固化（09-09，用户「要的」）**：`harness/guard.py` = append-only 静态守卫（扫 harness/*.py 的删除/覆盖/截断原语、破坏性 SQL、open 写模式），`python -m harness.guard` 运行、自检 `_tmp/verify_append_only_guard.py` 14/14；§10.15 契约本身接口预留仍文档级（无删除路径可守）。
- 上下文治理定论（09-07）：**单 ask 证据保真不折叠**（折叠断 `[id=N]` 链）；护栏只收异常风暴；省 token = 少绕圈非截原文。`context.py`（cl100k 计量）已写**未接线**，待升 `harness/context/` 组件。
- 打标链路：LoRA StructBERT 双头（emotion 连续分 + sarcasm 可疑门）；可疑 ≥0.4 攒批交 LLM judge（temp0+few-shot）覆写；服务挂→降级关键词。
- **measure 接线铁点（09-08 踩坑）**：nlp-tool 是 **HTTP**（演示服务器:8772，ThreadingHTTPServer POST /measure），**非 MCP**（MCP 形态只有爬虫 = `<MCP服务目录>`）。measure 默认连 `127.0.0.1:8772` + 读 `<打分服务目录>/.tool_token`（演示服务器 跑 harness 才生效）；**本地跑 harness 不设 env 就静默降级 keyword**（词表 0/1，无 sarcasm_prob → judge 永远不触发，跑批 DB 里看不出降级）。本地要真度量：`NLP_TOOL_URL=http://127.0.0.1:18772`（隧道→演示服务器:8772）+ `NLP_TOOL_TOKEN_FILE=<repo>/harness/data/.nlp_token`；`DEEPSEEK_API_KEY` 在 env 则可疑反讽真走 judge。离线链路证明 `_tmp/verify_nlp_judge_chain.py` PASS5/5。
- **评测现状（09-10 改写定案，正源 PLAN §10.20/§10.21）**：gold400 情绪 Spearman 0.389；反讽 F1 **0.368@th0.6**（gold001-only，>teacher 0.303）→ **定格为历史记录，不再扩标（gold003 砍）、不再人工复核、不追小模型数值指标**（用户「不想人工标注了」+「judge-primary 不就是 LLM 做 NLP 吗」→ 拍 M2 改写）。**定位分离（用户 09-10 点明「NLP 用于告诉 LLM 每条爬回数据是正面还是负面」）**：小模型 StructBERT = **每条样本的软信号章（senti 负/中/正 + sarcasm 可疑门，可疑≥0.4 交 judge 覆写 + topic_tags），随 crawl/search evidence 行喂给主链 LLM 判向**（engine 系统提示已加【软信号提示】字段解读：sarcasm=1 的正面措辞别当采信、senti=负 提示负面；但软信号本身可判错 → 判向最终以原文为准，见 §10.21）；情绪分布另喂图表 argmax 负面。**不追求 F1 达标、不接硬判定闸、不因不准摘除**。舆情纵深 determiner（黑话 referent/反串阴阳/自基线**最终判定**）= **主链 LLM 引原文判定**，能判给证据判不了明说——主链本来就是 LLM，C 方案尽头即此。

### 记忆框架 L1-L4（09-07 定稿，单 SQLite 文件 `harness/data/harness_dev.db`，`HARNESS_DB` 可换）
L1 StateCard（内存，materialize-then-prune；`correct()` 已接会话层反馈回路 v1，supersede-not-overwrite；snapshot/restore 供 L2）✅ ／ L2 checkpointer **✅ 09-09 转活**（`checkpoint.py`：每 scope 一份卡 JSON，save/clear 全 INSERT OR REPLACE 无 DELETE；Session 每轮 ask_turn 后落、flush/切 scope 清、冷启动 `_restore_checkpoint()` 续接）／ L3 archive（crawl_run verbatim + observation 去重 + session_claim 翻篇）✅ ／ L4 alias_registry **✅ 09-09 转活**（`registry.py`：黑话→规范名账本式 seed/resolve/find_in_text，生效=最新行 id 序替代不用 UPDATE；Session 播种 + engine `_alias_context` 黑话还原）。自检 `_tmp/verify_l2_l4_activation.py` 19/19。
> **09-08 namespace 记忆隔离落地（全表 scope 键）**：scope=`prod`(真实会话) / `exp:<tag>:<case>`(实验·标定·legacy)。三表各加 `scope` 列(db.py 运行时 ALTER 迁移)；session_claim 主键值落库前缀 `scope::`，observation.dedup_key 值前缀 `scope|` → 跨 scope 永不撞键；读写全按 scope 过滤。Engine/Session 带 scope + `set_scope()`（换 scope 先 `_flush_active()` 把旧结论翻篇进**旧** scope 日志，平台冷却保留）。calibrate 逐题 scope=`exp:calib:<tag>:qN`（每题记忆互不可见），analyze_calib 按 scope 精确回捞。测试 `_tmp/test_scope_iso.py` 25/25 + calibrate --mock 端到端冒烟过。旧标定残留已改归 `exp:legacy:harness-dev`/`exp:legacy:calib-live2`，主库 prod 空出。
> 本机向量记忆库（对话侧 add_turn）与 harness 会话记忆（StateCard/session_log）是**两套系统**，别混。

## 3. 状态盘点（2026-09-07）

**✅ 已完成**：真爬 NGA/B站 + X-Token + 本地隧道；退避+断路器+冷却+Bili 爬距（46开/MIN_SHARE 已于 09-08 删除）；引擎+门控+IN_CEILING；L1/L3 记忆；nlp-tool 部署+软信号+judge 导流；labeler UI+gold001/002+蒸馏 3594；迁移 gameharness+源码并入+血缘文档；双模式/SQL作废/存档只答过去等产品决策。

**⏸ 搁置**：反馈回路（无对话 shell 附着点）／ 官号监控（parking 二期，游客 −412 需登录，UID 表人工维护）。

**⏳ 待办（顺序）**：① runner/会话层 ✅ + feedback v1 ✅（9/7：`harness/runner/` Session+CLI `python -m harness.runner`、`/correct` 纠正、`calibrate.py` 标定脚本已备）→ ② live 标定**实跑**：**9/7 首跑 6 题（题单抽自生产服务器 standardized_data 真 NGA 帖，`harness/data/calib_qs_db.tsv`），得 5/6 答案；结果受 bili 全超时污染（见下）→ 修完后**重跑待定**（用户: 修了别跑）→ ③ 前台双模式+图表+卡片出图 → ④ 样本/标注扩容冲指标 → ⑤ L2/L4 播种 → ⑥ 去敏发布（gitignore/README/许可）。

**9/7 live 首跑实测记录（供重跑对照）**：
- DB 读径打通：`<DB用户>@standardized_data` 真密码在演示服务器?否→生产服务器 `<既有服务目录>/predict/predict_service.py`（jar yml 里那份已过期 = 之前 Access denied 根因）；库静态 NGA 到 6/26、253305 行 22 游戏；platform=0=post/reply(NGA)。
- 首跑问题（已修，本地+演示服务器）：① **bili 每次查询 5min+**（search_one_query 取 15 视频 × 逐个详情+评论爬，评论 DataFetchError 退避 5/10/20s）→ 本地 300s 超时全废、答案 NGA 单平台、46开没实现；**已修**：`search_one_query(max_videos=15默认)` + 每视频评论 `asyncio.wait_for 30s`，server 传 `BILI_MAX_VIDEOS=6` → smoke 13s/6视频×10评论，内容恰好命中"鸣潮深塔膨胀"话题。② **engine trace 混字符串**（翻篇 close 事件）→ calibrate 解析崩丢报告行；已改 dict 事件 `{"event":"thread_close","cid"}`。③ **Q4 强制收尾泄漏 `<tool_calls>` 残留且无"结论:"行** → 加 `_scrub` 清 XML + 强制收尾前系统提示禁工具意图。④ `Session()` db_path=None 冲掉默认 → `db.connect(db_path or db.DEFAULT_DB)`。
- 结论口径注意：首跑"NGA 未见该话题讨论/证据不足"结论受 bili 缺位放大，不可全信 → bili 修好后重跑才可信。

**9/8 live run2（calib_live2.db 独立库，跑完待复盘）**：
- **事故**：23:56 重跑双进程并发同隧道（report2 两 header 差 3s），两轮互相挤带宽/触发 NGA 429 → 无干净标定 run。Q1/Q6 首次 46开=True，Q1"未见膨胀讨论"结论反转（bili 进来后：主流认为数值膨胀、老角色被淘汰、有反讽对立）；Q2/Q5/Q6 有干净结论，Q3/Q4 又中 DSML 泄漏。
- **DSML 泄漏根治（engine.py）**：根因=强制收口时模型仍处搜索模式，tools 禁用后仍写"预算用完去 search_archive"散文+XML 当答案、无结论行。三层修：`_scrub` 正则删工具标签(含孤儿/未闭合)；新增 `_final_answer`（无干净『结论:』行→把失败草稿回喂强制重写一次，再失败清空悬空标记）+ `_conclusion`/`_tool_intent`/`_drop_empty_concl`；`_materialize` 兜底改诚实占位"(未收口)"绝不拿残句当历史结论。单测 22/22 + mock 冒烟过。
- 遗留：run1 污染残留仍在 harness_dev.db（M1-M5+c1-c3+19 crawl_run），待清；主库应只留真实会话。单进程重跑一轮才可信（待用户拍）。

**9/8 补记 — 串题/污染怎么修的（namespace 隔离，现态已落地）**：
- **根因复盘**：① run2 双进程并发打同库同隧道是操作事故；② 更要命的是 L1-L4 记忆设计**没有隔离维度**——同一 Session/DB 下题目间串题（Q2 搜归档能见 Q1 爬的样本）、run1/run2/冒烟残留全堆在 prod，结论只随翻篇落库（最后一题留内存），出了岔无法归因到具体那次标定。
- **修法（逐层）**：① 加隔离键 = 三表各增 `scope` 列，db.py `_ensure_column` 运行时 ALTER 迁移旧库（先补列再跑 schema，否则 `idx_obs_scope` 建索引先炸）；② 键隔离：`session_claim` 主键值前缀 `scope::`、`observation.dedup_key` 值前缀 `scope|` → 跨 scope 永不撞 PK/UNIQUE；③ 读写过滤：archive/session_log/tools(crawl_nga/crawl_bili、search_archive 经 `ctx["scope"]`) 全按 scope 读写；④ 边界提交：Engine/Session 带 scope + `set_scope()`，换 scope 先 `_flush_active()` 把旧 scope 仍活跃结论翻篇进**旧**日志（scope 边界=提交点，否则逐题隔离下前一题结论丢内存），平台冷却 `_plats` 保留跨题沿用 B站退避；⑤ 标定逐题 `exp:calib:<tag>:qN` + 收尾 flush（最后一题也落库），analyze_calib 改按 scope 精确回捞；⑥ 清残留：run1→`exp:legacy:harness-dev`、run2→`exp:legacy:calib-live2`（非破坏，行保留只移 scope），主库 prod 空出留真实会话。
- **验证**：`_tmp/test_scope_iso.py` 25/25（迁移加列/跨 scope 去重不串/PK 前缀/换 scope 翻篇提交/工具按 ctx scope）；`calibrate --mock` 两题端到端：每题各 1 claim 落自己 scope、q2 搜不见 q1 爬的（crawl_run 3/2、obs 15/10 隔离）；finalize 回归 22/22（DSML 修复没被破坏）。
- **现态**：主库 prod 空（留真实会话）；live 标定重跑仍待用户拍（单进程、每题自动分 scope，不再互相污染）。

**9/9 夜 M0 基建 + 验证层（最新现态：HANDOFF 顶行；决策正源 PLAN §10.18）**：git 全线暂缓（用户「都先别进 git」）；M0 文件 = `requirements.txt`（核心纯 stdlib，仅可选 tiktoken）+ 根 `README.md` + `harness/test.py` 一键自测聚合器（guard + gold replay + 15 离线回归全绿；服务器门 2 个 `--server` 单列）。M1.2 黄金回放 v0 = `harness/runner/gold_cases.json` + `evalgold.py`（6 expect_pass + 1 known_issue「bngaonly q1 实指 0 引」；C1 结论在 / C2 引用对回 scope / C3 无引即避 / C4 有真爬；首跑 6 PASS + 1 KNOWN + 0 意外 = 隔离实证）。M1.3 收口证据校验 = engine `_sanitize_cites` + `_seal_answer`（直答/强制收口两出口剔编造/跨 scope `[id=N]` + trace `cite_drop`，回归 12/12）。顺带治理 prod flow 污染：根因 = verify 单测在临时库 scope=prod 跑 ask，flow 按 scope 落盘进 prod.jsonl；5 个 verify 脚本加 `HARNESS_FLOW=0`，删 17 行测试残行。**测试分层已成型**：确定性 verify_*（harness.test）→ 黄金回放（evalgold，离线）→ live 认证 runbook（M1.1 待演示服务器 窗口）。**待决**：prod.jsonl 剩 3 行 09-08 旧行去留；崩铁·本版本强度膨胀 fresh 已登记 pending_live 待 live 录得后入 gold cases。

**⏳ 待拍**：前端技术栈（docs D7，动工前台时定）。~~舆情纵深 gold003/C 方案~~ **已结案（09-10，PLAN §10.20）：M2 改写，免人工/不扩标/不追小模型指标，determiner = 主链 LLM 判定**。
**🅿 停车（用户拍 9/8）**：**问答→PDF 报告** —— **已结案（2026-09-16，PLAN §10.62）**：做成了，但**语义改了**：
不是"一次会话汇总成报告"，而是**每一题右上角一个开关，打开则这一题答完自动导出一份 A4 PDF**
（题头 / 样本数 / 正文〔`[id=N]` 排上标〕/ 引用清单 / 页脚），`harness/pdfout.py` 用爬虫同一份系统 Chrome 渲染，不引新依赖。
用户主动要求才出（非每次自动）、形态=PDF；内容=本次问答/某主题的结论+证据引用+平台/情绪/话题统计+用时 token。技术注记（届时用）：**server 端渲染，非模型工具**（模型不需要拿文件工具）；中文 PDF 需 CJK 字体嵌入（reportlab/fpdf2+雅黑，或 weasyprint/HTML→PDF）；数据源=单次 ask 返回值 + scope 下 observation 聚合，无需新落库。有真实用户场景再做。

**9/8 晚 — 复盘地基四件（A1-A4）+ 空 query 炸题修复 + 度量接线（细枝见 PLAN §10.10）**：
- 大标定 big10（tag `big3f7p-20260908`，`harness/data/calib_big_20260908.db`）3快7精 10 题：q1/q2/q4/q6/q7/q10 干净收口、**q3+q5 空 query 炸整题**（旧码引擎无守卫）、**q8+q9 收口失败 `(未收口)`**（正文没落盘查不到细节）→ 全留给新码补跑。
- 空 query 修复：engine 守卫挡成受控 tool-error（提示补词）+ schema required 加 query；离线 verify_crawl_noquery PASS6/6。**只对新码生效**。
- A1 `harness/flow.py`（engine 每 ask 收口自动落 per-scope jsonl，answer全文+trace+concl+ev+usage，HARNESS_FLOW=0 关）；A2 `harness/runner/verify.py`（确定性 verifier：结论行/cite对回scope/--check-run，退出码 0/1/2）；A3 engine 强制二次收口端到端回归（意图残句→重写落结论、悬空标记→诚实(未收口)，verify_a3 PASS9/9）；A4 `harness/runner/observe.py`（跑批中间态观察器：收口/采集中/疑卡）。回归 verify_flow 9/9、verify_a2 11/11。
- 补跑中：tag `bigfix-20260908`（`calib_bigfix_20260908.db`）金铲铲/明日方舟/绝区零不驻场/崩铁立绘 4 题，新码+真度量。

## 4. 基建与凭证事实（不写明文值）

- 部署机：采集/推理实机（NGA 8770 / bili 8771 / nlp-tool 8772，systemd 常驻）+ 公网跳板/演示机（可选）。本地隧道连实机。
- 凭证：爬虫 X-Token 只在运行机本地（0600）；**bili 登录态 cookie = 服务器唯一 owner，绝不拷回/进仓库/进对话**；模型密钥走 env（只做存在性检查）。
- 血缘纪律：毕设原版一行不动，改动只落副本；`crawlers/bili` 为 MediaCrawler fork（NON-COMMERCIAL），**发布物只发自写 service 层+文档指向上游，不发 vendor 树**。

## 5. 铁律（持续生效）

1. 明文 cookie/token 绝不进对话/日志；X-Token 只在服务器本地读。
2. 毕设原版一行不动；只改 `<NGA爬虫目录>`、`<B站爬虫目录>` 副本。
3. 未经明确允许不 git push；性能数字可溯源、绝不编造。
4. 采集量级先对齐；LoRA/训练须用户明确指令（禁自动跑）；密钥只做存在性检查。
5. bili 登录态 = 服务器唯一 owner，绝不拷回。
6. 数据精准度最高约束；图表口径 argmax 负面，不看分数符号；评测不达标不接硬判定。
7. live 校准每题间隔 5-10 分钟（B站风控）；所有产物去敏后再进 GitHub。
8. **动演示服务器/生产服务器 前先评估 OOM**（用户：每次操作前必须考虑会不会把服务器干崩）：生产服务器=2c4g 满载生产别碰现服务、演示服务器=4c8g 也常驻爬虫+nlp；先 `free -h` 看余量、单进程、长任务 timeout、量级先对。

## 6. 相关外部记忆（<本机>\.claude\projects\<旧项目名>\memory\）

- `opinion-agent-plan.md` / `opinion-agent-memory-framework.md` / `opinion-harness-player-perspective.md` / `opinion-harness-front-mode.md`
- `feedback-collect-scale-align.md` / `feedback-realtime-ingest.md` / `feedback-code-provenance.md`
- `game-timeline-official-account.md` / `rag-technical-lessons.md`（D4 检索历史教训）
