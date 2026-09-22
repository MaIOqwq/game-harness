# 架构设计文档 — 游戏社区问答 Agent

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.1（2026-09-07 落成；2026-09-08 同步 scope 隔离 / correct 接线；2026-09-09 同步 NGA 检索三连修、`nga_ratio` 平台占比入参、§5.1 数据可删性契约 + §5.2 scope 段重排、guard.py append-only 守卫、context.py 计量接线、L2 checkpointer + L4 alias registry 转活、§10 标定实况） |
| 对应需求 | `docs/PRD.md`（功能与非功能口径） |
| 代码真源 | `harness/`（本仓库）；爬虫源归档在 `crawlers/` |
| 决策依据 | `docs/TECH_SELECTION.md`（各选型独立论证，含触发更换条件） |

---

## 1. 设计目标与约束

1. **单机可演示、零依赖可跑**：Python 标准库为主的问答主链，无构建步骤，断网有 mock 兜底。
2. **数据精准度最高**：证据走原文保真链路，可溯源、可回查，架构不为省 token 牺牲证据。
3. **诚实可审计**：答案带 `[id=N]` 引用；全程行为留日志，能复盘"它为什么这么答"。
4. **成本可控**：双档采样上限、窄工具面防跑偏、专用小模型做软信号预筛（不把所有判断压给大模型）。
5. **工程化 + 可去敏发布**：凭证与登录态与代码物理隔离；发布物不含密钥/vendor 源/本地记忆库；结构为未来部署留口（选型 D8）。

## 2. 设计原则（六条，架构上不可违背）

| # | 原则 | 含义与原因 |
|---|---|---|
| P1 | **窄工具面** | 模型可见工具 = `{crawl_nga, crawl_bili, search_archive}` 白名单（function-calling 声明的）。数值参数（视频数/评论数/排序）由引擎定，不放给模型——放权后 LLM 会乱调，风控与成本失控。两 crawl 工具按平台分开、各固定一平台，由 `tools/crawl_live.py` 的 CRAWLERS 注册表驱动（装/卸注册项 = 增删平台能力面）。 |
| P2 | **落档是副作用** | 每次真爬的原文自动落 L3 存档（append-only + 去重），**不由模型决定存不存**。若让模型决定，会漏存关键证据。 |
| P3 | **gather-then-compose** | 先采全证据（多轮爬取攒快照），后一次合成答案。不边爬边写结论；证据在单 ask 内**保真不折叠**。 |
| P4 | **materialize-then-prune** | 记忆裁剪只动"已物化结论"（L1 StateCard），结论/引用不属于可裁剪区；数据本体不压缩，只压上下文。 |
| P5 | **护栏优先于裁剪** | 上下文治理用**上限护栏收口异常**（`IN_CEILING`），不是对正常证据做启发式截断——截断会断引用链。 |
| P6 | **诚实契约** | 能答则答、不确定明说、结论置顶、引用逐条可回查；"这是社区口径非权威"随答案可见。 |

## 3. 逻辑架构

```
┌────────────────────────────────────────────────────────────────┐
│  前台 / 会话层（⏳ 待建）                                        │
│  双模式自选（快速/精准） · 会话历史 · 聊天栏内嵌图表 · 卡片出图      │
└───────────────┬────────────────────────────────────────────────┘
                │ 问句 + 档位
┌───────────────▼────────────────────────────────────────────────┐
│  编排层  Engine（✅ harness/engine.py）                          │
│  function-calling 循环（≤6 轮） + 门控闸门                       │
│   · 爬轮硬上限（CRAWL_BUDGET，按轮计，同轮双平台=1）               │
│   · 参考样本量（SAMPLE_TARGET，软不硬停，ask_total 供 LLM 自判）   │
│   · 平台状态机：断路器 + 冷却（COOL）+ B 站爬距（BILI_GAP）         │
│   · 上下文护栏（IN_CEILING）· L1 物化（materialize-then-prune）   │
│   · 快速/精准两套 prompt（_base_system）                          │
└───┬───────────────┬───────────────┬────────────────────────────┘
    │ 工具白名单     │               │
    ▼               ▼               ▼
┌───────────┐ ┌───────────────┐ ┌──────────────────────────────┐
│*crawl_live│ │search_archive │ │  记忆层 L1-L4（✅/⏳，单 SQLite）│
│ (✅ tools/)│ │ (✅ tools/)    │ │  L1 StateCard(内存) ✅          │
│ 真爬两平台  │ │ 查 L3 历史      │ │  L2 checkpointer   ✅ (09-09)  │
│ 副作用→L3  │ │ 存档只答过去    │ │  L3 archive+session_log ✅    │
└─────┬─────┘ │               │ │  L4 alias_resolution ✅ (09-09)│
      │ HTTP   │               │ └──────────────────────────────┘
┌─────▼─────────────────────┐   ▲
│  采集层（✅ crawlers/，常驻） │   │ verbatim 原文 / 检索命中
│  nga-tool · bili-tool      │   │
└───────────────────────────┘   │
┌───────────────────────────────▼──────────────────────────────┐
│  语言/分析层（✅ nlp/ + measure.py + judge）                    │
│  小模型软信号=初筛/图表（情感连续分+反讽可疑门→可疑交 judge 覆写）   │
│  舆情纵深 determiner = 主链 LLM 判定（引原文；判不了明说）         │
│  铁律 09-10 改写：小模型不接主链硬判定、不追数值指标，gold 不扩     │
└──────────────────────────────────────────────────────────────┘

治理横切：token 计量（cl100k 已接线：engine 侧 `_est_input_tokens` 累计+护栏双保险，09-09） · 门控闸门（engine） ·
          降级链路（服务挂→mock/关键词法，主流程不炸） · 凭证隔离（见 §8）
```

`*crawl_live`：原单 crawl_live 已拆成 **crawl_nga + crawl_bili 两独立工具**（`tools/crawl_live.py` CRAWLERS 注册表 → engine `TOOL_DEFS`；各固定一平台、不暴露 platform 参数，模型工具面 = {crawl_nga, crawl_bili, search_archive}）。同轮两工具并发 fetch（ThreadPoolExecutor，provider 抓取不碰 DB）、record_crawl 落库回主线程串行 → sqlite 单写不跨线程。

## 4. 端到端一次问答时序（两档分支）

```
用户：问句 + 选档(快速/精准)
  │
  ▼
1 冷启动：识别游戏/主题 → 是否社区可答 → 需要哪些关键词
  │
  ▼
2 循环取证据（每轮 NGA+bilibili 各爬一次，不二选一；遵守平台状态机）：
   crawl_nga(query, game) + crawl_bili(query, game) ×2（同轮两工具并发各一发，算 1 爬轮）
     → 平台返回样本 → 自动落 L3（副作用，append-only+去重）→ 返回 ask_total
     → 直到 (a) 爬轮预算用尽强制收口 / (b) 样本参考量达标 LLM 自判够 / (c) 冷却熔断则转侧或注偏收口
   （样本参考量 SAMPLE_TARGET 为软：够不够由 LLM 每轮比对 ask_total 判断，不再硬停）
  │
  ▼
3 search_archive：回顾类问题按 as_of 命中历史存档（只答过去）
  │
  ▼
4 gather：攒本轮全部证据原文（id 连续编号，单 ask 内保真不折叠）
  │
  ▼
5 合成（LLM）：按证据作答 → 逐条 [id=N] 引用 → 末行"结论："
  │   可选：measure.py 小模型软信号（初筛/图表）→ 反讽可疑 → judge 覆写；
  │   舆情纵深 determiner = 主链 LLM 判定（引原文；判不了明说）
  ▼
6 返回：结论 + 引用 + 抽样/偏置声明 + 可嵌入图表（前台渲染）
```

**关键取舍**：快速档允许"先给能看的初答"，精准档下探 500+ 样本再答。证据在单 ask 内永不折叠——实测精准档最终答案引用跨多轮爬取的 id16-60，折叠会断引用链。

## 5. 记忆分层 L1-L4

| 层 | 名称 | 作用 | 状态 | 落点 |
|---|---|---|---|---|
| L1 | StateCard | 单会话内状态卡：anchor/active/corrections/shown，materialize/correct/close/render | ✅ `state_card.py`（`correct()` 已接反馈回路 v1：`Session.correct()` + CLI `/correct`，supersede-not-overwrite；`snapshot()/restore()` 供 L2 检查点，09-09） | 内存；close → 会话翻篇 |
| L2 | checkpointer | 会话状态卡跨进程续接（每 scope 一份当前进度：卡 JSON + 结论编号） | ✅ `checkpoint.py` + schema `checkpoint` 表（save/load/clear 全 `INSERT OR REPLACE`，无破坏性 SQL）；Session 每轮 ask_turn 后落、flush/切 scope 清、冷启动自动续 | 同一 SQLite |
| L3 | archive | `crawl_run`(verbatim) + `observation`(去重 append) + `session_claim`(翻篇日志)；话题命中按需回捞 | ✅ `archive.py` + `schema.sql` | SQLite |
| L4 | alias_resolution | 黑话/别称 → 游戏的版本化账本（supersede-not-overwrite：**生效=该 alias 最新行，id 序替代，不 UPDATE**） | ✅ `registry.py`（seed/seed_builtin/resolve/find_in_text，账本式幂等播种）；Session 建库后播种、engine `_alias_context` 黑话还原进 game_hint | 同一 SQLite |

**为何单文件 SQLite**：量级（单会话几十~几百条、积累到数千条）远未到需要独立库服务；零安装随项目走；已封 `db.py`（`connect/init` 统一入口，`HARNESS_DB` 可换路径），未来换 MariaDB/PostgreSQL 只换驱动层（选型 D3）。
**边界**：本机向量记忆库（对话侧的 `add_turn`）与 harness 会话记忆（StateCard/session_log）是**两套系统**，互不混淆。

### 5.1 数据可删性契约（前端「清除对话记忆」预留，2026-09-09 定）

数据分两层，**删/不删在架构上分家，不靠前端按钮自觉**：
- **会话层 = 可删**：前端聊天历史、该用户 L1 StateCard（本就随 close/会话翻篇弃置）、`session_log`、该用户 ask 的 flow jsonl。用户「清除对话记忆」只触及这层。
- **证据层 = 禁删**：L3 archive（`observation`/`crawl_run`/`session_claim`）+ 报告文件。跨 scope 共享、是 `[id=N]` 引用对回的正源、append-only。**删除入口在数据层够不着它**——这是防误删的根：按钮代码即使把 scope 传错也碰不到证据层。

现状：全仓**零删除原语**（2026-09-09 grep 佐证：无 `os.remove/unlink/rmtree/truncate` / `open("w")` / SQL `DELETE/DROP/UPDATE`；文件写 = flow/report 追加 `"a"`；sqlite 仅 `CREATE TABLE IF NOT EXISTS` + `INSERT` + `ALTER ADD COLUMN`；`observe.py` 库只读 `mode=ro`）。契约先落文档，将来接前端按钮时**按三闸实现，禁止散落 rm**：
1. **范围隔离**：未来删除 API 签名强制 `scope + kind∈{conversation}`；证据层表带 `protected` 禁删标记（接口预留：删除函数先校验 kind 非证据层才放行；动证据层须管理员级单独入口，不存在"一次调用两层都删"的路径）。
2. **软删优先**：删除只打 tombstone `deleted_at`（保留期默认 7 天可恢复）；物理清除是独立延迟任务（二期），不放进用户按钮。
3. **单点 + 留痕**：所有删除走一个带 `--dry-run` 的 CLI / 一个 endpoint；`type-to-confirm` 由前端做；每次删除写 append-only 审计日志（谁/何时/scope/条数）。

> **守卫（09-09 落地）**：契约已固化成**可执行检查** `harness/guard.py` —— 静态扫 harness/*.py，命中删除/覆盖/截断文件原语、破坏性 SQL、`open` 写模式即列违规并 exit 1（guard.py 自身豁免）。运行 `python -m harness.guard`，改 harness 代码后跑一遍当回归；自检 `_tmp/verify_append_only_guard.py` 14/14。

### 5.2 scope / namespace 隔离（2026-09-08 落地，隔离键不是新层）`crawl_run` / `observation` / `session_claim` 三表各加 `scope` 列（`db.py` 运行时 ALTER 迁移旧库——先补列再跑 schema，否则 `idx_obs_scope` 建索引先炸）。值前缀隔离：`session_claim` 主键落库前缀 `scope::`、`observation.dedup_key` 前缀 `scope|` → 跨 scope 永不撞 PK/UNIQUE。语义：`prod` = 真实会话；`exp:<tag>:<case>` = 实验/标定（逐题 `exp:calib:<tag>:q<N>` 互不可见）；legacy 残留归 `exp:legacy:*`。Engine/Session 带 scope + `set_scope()`：换 scope 先 `_flush_active()` 把旧 scope 仍活跃结论翻篇进**旧**日志（scope 边界 = 提交点），平台冷却 `_plats` 刻意保留（跨题沿用 B 站退避）。读写（archive / session_log / crawl_nga / crawl_bili / search_archive）全按 scope 过滤。动机：9/8 run2 双进程并发事故暴露 L1-L4 记忆设计缺隔离维度 → 题间串题、无法归因到具体标定。

## 6. 门控与上下文治理

### 6.1 引擎闸门（常量一处改全局生效；09-08 平台不二选一、预算按轮、达标不硬停）
| 闸门 | 快速 | 精准 | 触发结果 |
|---|---|---|---|
| CRAWL_BUDGET 爬轮硬上限 | 3 | 5 | **按「轮」计**：同轮 NGA+bili 各爬一次=1 轮；用尽 block 强制收口（全被平台闸门挡的轮不耗） |
| SAMPLE_TARGET 参考样本量（软） | 120 | 500 | 不硬停；每次真爬返回 `ask_total`，够不够由 LLM 每轮比对自判 |
| BILI_GAP 爬距 | 60s | 120s | B 站两轮间隔（键在真实请求时刻） |
| COOL 冷却 | 300→900s | 同左 | 平台触发风控后升温，成功归零；NGA 只熔断不冷却 |
| IN_CEILING 护栏 | 160000 | 320000 | 异常工具风暴时提前收口（09-12 重定档，按模型窗口留余量；正常问答护栏休眠） |

> 09-08 删除 ~~MIN_SHARE 46 开配比硬 gate~~（双平台收量天然不均，作答如实注偏即可）；平台由「二选一」改「每轮 NGA+bilibili 各爬一次」。快速/精准用**两套** `_base_system` prompt（前端可选项，行为/预算/收口口径不同）。
> 09-08 追加：单 crawl_live 工具拆成 **crawl_nga / crawl_bili 两个独立工具**（注册表 CRAWLERS → TOOL_DEFS，各固定一平台）；同轮两工具并发 fetch（provider 不碰 DB）、record_crawl 落库与平台计数/熔断回主线程串行 —— sqlite conn 单写不跨线程。

### 6.2 上下文治理模型
- **不做**：对正常证据的启发式裁剪/折叠（会断引用链）、QA 收口、答案深度门（刻意不加，避免过度约束）。
- **做**：护栏收异常 + token 计量 + 证据按轮组织。
- **省 token 的正解 = 少绕圈（轮数），不是截原文**。

## 7. 组件与代码映射

| 目录 | 职责 | 状态 |
|---|---|---|
| `harness/engine.py` | 编排循环 + 门控闸门 + 护栏（TOOL_DEFS / `_base_system` 快速·精准两套 prompt 在此）；空 query 守卫（model 缺 query → 受控 tool-error 不炸，回爬轮见 §6.1） | ✅ |
| `harness/tools/crawl_live.py`（CRAWLERS 注册表 → crawl_nga/crawl_bili，fetch/commit 两段） `search_archive.py` | 模型可见工具实现 | ✅ |
| `harness/sources/live.py` `mock_nga.py` | 平台接入（真源/断网 mock 兜底） | ✅ |
| `harness/archive.py` `session_log.py` `state_card.py` | L1/L3 记忆（state_card 含 `snapshot()/restore()`） | ✅ |
| `harness/checkpoint.py` | L2 检查点：每 scope 一份状态卡 JSON，`INSERT OR REPLACE` save/clear（无 DELETE）；Session 每轮落、flush/切 scope 清、冷启动 `_restore_checkpoint()` 自动续 | ✅ 2026-09-09 |
| `harness/registry.py` | L4 黑话注册表账本：seed/seed_builtin/resolve/find_in_text（幂等播种、最新行生效、无表安全回落）；Session 建库后播种，engine `_alias_context` 黑话→规范名 | ✅ 2026-09-09 |
| `harness/measure.py` | 软信号：nlp-tool HTTP + 反讽可疑（prob≥0.4）→ DeepSeek judge 覆写 + keyword 兜底降级。本地跑经隧道必设 `NLP_TOOL_URL`（如 `http://127.0.0.1:18772`）+ `NLP_TOOL_TOKEN_FILE`（`harness/data/.nlp_token`，默认 Linux 路径仅 演示服务器 本机可用）；不设即静默降级 keyword | ✅ |
| `harness/context.py` | token 计数（cl100k）；engine 侧 `_est_input_tokens` 累计 + 护栏双保险（自检 `_tmp/verify_context_wiring.py` 8/8） | ✅ 2026-09-09 |
| `harness/db.py` `schema.sql` | SQLite 封装 + 建表 + 运行时 ALTER 迁移（scope 列） | ✅ |
| `harness/guard.py` | append-only 守卫：静态扫 harness/*.py 命中删除/覆盖/截断原语、破坏性 SQL、open 写模式 → 列违规 exit 1（guard 自身豁免）。运行 `python -m harness.guard`，改动 harness 代码后跑一遍当回归（自检 `_tmp/verify_append_only_guard.py` 14/14） | ✅ 2026-09-09 |
| `harness/flow.py` | per-scope jsonl 逐答流水（append-only，engine 每轮/收口落 trace 事件，如 `ratio_nudge`）；`HARNESS_FLOW=0` off | ✅ 2026-09-08 |
| `crawlers/nga` `crawlers/bili` | 采集源（源码归档；bili 为 MediaCrawler fork，血缘见 README） | ✅ |
| `nlp/` | LoRA 训练 / 评测 / nlp-tool（部署） | ✅ |
| `labeler/` | 标注 UI + gold001/gold002 + 蒸馏集 | ✅ |
| `docs/` | 本文 + PRD + 选型表 | ✅ |
| `harness/context/`（计划） | 把 context.py 升为组件（Builder/Meter/CeilingGuard/EvidencePolicy/Trace） | ⏳ 待建 |
| `harness/runner/` | 会话层：持住 Engine 跨轮让 ask() 对话化。`session.py`(Session/ask_turn/**correct** 反馈) + `shell.py`(CLI 对话壳, `/correct`) + `__main__.py` + `calibrate.py`(live 标定脚本, `--nga-ratio`) + `verify.py`(跑后逐 scope 对账 archive/session_claim) + `observe.py`(只读观察器: 收口/采集中/疑卡)；flow 流水为顶层模块见上行 | ✅ 2026-09-07 / 09-08 扩 |
| `frontend/`（计划） | 双模式 + 图表嵌入 + 卡片出图 | ⏳ 待建 |

## 8. 凭证与去敏（物理隔离）

- 明文 cookie/token **绝不**进对话/日志/仓库；爬虫 X-Token 存在运行机本地（0600）。
- bili 登录态（SESSDATA/bili_jct 等）**只在服务器持有**，绝不拷回本地/进仓库/进对话——采集层归档时已把真 cookie 与 browser_data 排除。
- 模型密钥走环境变量（存在性检查，不打印值）；`data/`、密钥文件一律进 `.gitignore`。
- GitHub 发布物：**自己的服务层 + 文档**，不含 vendored 的 MediaCrawler 源码树（许可口径见选型 D9）。

## 9. 部署拓扑

```
本地演示（面试/自用）                内网采集机（常驻，唯一持有登录态）
┌──────────────────┐   HTTP    ┌───────────────────────────────┐
│ 前台 + Engine     │ ────────► │ nga-tool · bili-tool · nlp-tool│
│ + SQLite 存档     │   隧道     │（systemd 常驻 + 断路器）       │
└──────────────────┘            └───────────────────────────────┘
      ▲
  可选：公网演示机反代（部署留口，选型 D8）
```

本地问答主链零依赖可跑（mock 兜底断网）；要真数据时经隧道打常驻采集服务。未来多用户/公开演示再整体上生产部署。

## 10. 演进路线（引用 PLAN §10.6 遗留待办）

1. **runner/会话层** ✅（2026-09-07 已落：`harness/runner/`，Session 会话层 + CLI 对话壳 `python -m harness.runner [--live]`）。**反馈回路 v1 已立项** ✅：`Session.correct()` + 壳内 `/correct`，走 `StateCard.correct()` supersede-not-overwrite，原结论留痕 corr 行随状态卡带给模型。`calibrate.py`（live 标定）脚本已备，真爬跑需本地隧道 + 题单确认。
2. **live 标定**（进行中）：9/7 run1（6 题 5/6）结论受 bili 全超时污染 → bili 修复（`asyncio.wait_for 30s`、server `BILI_MAX_VIDEOS=6`）；9/8 run2 双进程并发同隧道触发 NGA 429 + Q3/Q4 DSML 泄漏 → **已根治**：scope 隔离 + engine 收口加固（`_scrub` / `_final_answer` / `_conclusion` / `_tool_intent` / `_drop_empty_concl`，单测 22/22）+ bili 关键词空格 fix（`sources/live.py`）。**单进程真打已跑通（9/8 起强制单进程**——双进程并发同隧道曾触发 NGA 429）：9/8 run2b/live3b 单进程、逐题 scope=`exp:calib:<tag>:q<N>` 隔离；9/9 加 **NGA 检索三连修**（逐词单搜 + 两段式主板 fid 解析 + 正文兜底，真源在 演示服务器 `/opt/<nga-crawler-copy>`，仓库镜像 `crawlers/nga/nga_search_demo.py` 同版）→ NGA-only 补跑（tag `bngaonly-20260909`，`nga_ratio=100`）绝区零 42 / 崩铁 90 全 NGA，NGA 修复在明日方舟/金铲铲/绝区零/崩铁**泛化实证**。双模式采样区间定稿 **120/500**。隧道纪律：本地 ssh -N 隧道周期性自行掉（exit 255，演示服务器 侧恒 active），每次 live 跑前先 /health 探活、掉了就地重挂。
3. **前台**：双模式 + 图表协议 + 卡片出图 + 结论置顶。
4. **context 组件化**：把 `context.py` 升为 `harness/context/`（Builder/Meter/CeilingGuard/EvidencePolicy/Trace）。
5. ~~L2 checkpointer + L4 alias registry 播种~~（**2026-09-09 已转活**：`checkpoint.py` + `registry.py` 上线，Session 每轮落/冷启动续检查点、黑话幂等播种 + engine `_alias_context` 还原；自检 `_tmp/verify_l2_l4_activation.py` 19/19）。
6. **舆情纵深迭代（09-10 改写定案，PLAN §10.20）**：determiner = 主链 LLM 判定（黑话 referent/反串阴阳/自基线，引原文、判不了明说）；小模型仅初筛/图表，不扩 gold、不追 F1、不接主链硬判定。
7. **去敏发布**：补 `.gitignore`/README/许可声明，结构已留口。

---
*本文与代码冲突时以代码为准；决策脉络见 PLAN.md §10.1。*
