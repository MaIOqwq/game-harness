# RECALL.md — 话题记忆索引

> 读法：用户话里命中任一行关键词 → 跳去对应正源读。本文件只告诉你"在哪"，不装内容。
> 正源链（PROJECT_MEMORY.md）：代码 > git > `PLAN.md §10` > `docs/` > `PROJECT_MEMORY.md`。任何复述与此链冲突，以链上为准。

## 定位 / 产品决策
| 关键词 | 跳去 |
|---|---|
| 游戏社区问答 / 舆情 / 定位上修 | `PLAN.md §10.1`(9/6 23:05) + `PROJECT_MEMORY.md §0` |
| 双模式 / 快速精准 / 150 / 500-700 | `PLAN.md §8.10` + `§10.2` + `§10.4` |
| SQL 作废 / 存档只答过去 / 快照落档 | `PLAN.md §8.10` + `§10.1`(9/4) |
| 循环攒样本 / 太线性 / 8.8 | `PLAN.md §8.8` + `§8.9` |
| 问答→PDF（停车，主动要求才出） | `PROJECT_MEMORY.md §3` 🅿 |
| harness 八件套 / 系统工具刻意不做 | `PLAN.md §10.3` |

## harness / 引擎 / 记忆
| 关键词 | 跳去 |
|---|---|
| 46开(已删) / 闸门 / 爬轮预算按轮 / 双平台不二选一 / 冷却 / 爬距 / 护栏 | `PLAN.md §10.2` + `§10.4` + `§10.9`(09-08) + `topics/engine-gates.md` |
| 记忆 L1-L4 / StateCard / session_log / L2 检查点 / 断点续接 / checkpoint | `docs/ARCHITECTURE.md §5` + `harness/checkpoint.py`(09-09 转活: Session 每轮落/冷启动续) + `PROJECT_MEMORY.md §2` + `topics/memory-scope.md` |
| scope 隔离 / 串题 / namespace | `PROJECT_MEMORY.md §3`(9/8 补记) + `topics/memory-scope.md` |
| DSML 泄漏 / 收口 / _final_answer | `PROJECT_MEMORY.md §3`(run2 段) + `topics/memory-scope.md` |
| 上下文护栏 / 不折叠 / IN_CEILING | `PLAN.md §10.2` + `topics/engine-gates.md` |
| context.py token 计量 / _est_input_tokens / est_input_tokens | `engine.py`(09-09 接线: engine 侧 cl100k 累计 + 护栏双保险) + 自检 `_tmp/verify_context_wiring.py` 8/8 |
| 平台比例旋钮 / nga_ratio / NGA占比 / RATIO_TOL / 样本证据配比 | `PLAN.md §10.12-§10.14` + `engine.py`（端点 0/100 硬压 + 内部软引导）+ `calibrate.py --nga-ratio` |
| 数据可删性契约 / 防误删 / append-only 守卫 / 清除对话记忆按钮 | `docs/ARCHITECTURE.md §5.1` + `harness/guard.py`（`python -m harness.guard`）+ `PLAN.md §10.15/§10.16` + `PRD.md FR9` |
| live 标定 / calibrate / 5-10min / 单进程强制 | `harness/runner/calibrate.py` + `PROJECT_MEMORY.md §3` |
| 工具注册表 / tool / 工具开关 / MCP / 外部工具 / mcp__ 前缀 / 情感打分当工具 | `harness/tools/registry.py` + `harness/mcp/` + `INSTALL.md 第七节` + `HANDOFF.md`(09-15 06:1x) |
| 爬虫状态灯 / 冷却中倒计时 / 正在使用 / busy / cool_left / 冷却期强制不可使用 | `PLAN.md §10.52` + `harness/webapp.py`(_cool_secs/_cool_left/_cool_guard) + `web/index.html`(crawlerState) + `_syscheck/verify_status*.{py,js}` |

## 爬虫 / 数据
| 关键词 | 跳去 |
|---|---|
| 去空格 / 关键词拼接 / 游戏名+词条 | `harness/sources/live.py:154` + `topics/crawler-pipeline.md` |
| NGA 检索三连修 / searchin / 逐词单搜 / 两段式主板 fid / 正文兜底 / 明日方舟长草 | `HANDOFF.md`(09-09 02:2x，明日方舟 0→10 实证) + `crawlers/nga/nga_search_demo.py`(仓库镜像; 真源 演示服务器 /opt/<nga-crawler-copy>) + `PLAN.md §10.9`(09-08 前身) |
| 别名 / 三蹦子 / 星铁 / 黑话 / 指代消解 / alias | `harness/registry.py`(09-09 L4 转活: 账本式播种, Session 播种 + engine `_alias_context`) + `PLAN.md §10.1`(9/4) |
| bili 3条/页 / QR 扫码 / 登录态 | `PLAN.md §9` |
| 采集量级 / 1600 教训 | `topics/crawler-pipeline.md` + 记忆 `feedback-collect-scale-align` |
| crawler-mcp / bili-tool / nga-tool | `PLAN.md §8.2` |

## NLP / 打标 / 评估
| 关键词 | 跳去 |
|---|---|
| gold400 / 反讽 F1 / 蒸馏噪声 | `PROJECT_MEMORY.md §2` + `topics/labeling-nlp.md` |
| LoRA / StructBERT / gold002 / teacher | `topics/labeling-nlp.md` |
| gacha 领土 / 反讽可疑门 / judge | `PLAN.md §10.1`(9/5) + `topics/labeling-nlp.md` |
| nlp-tool / 8772 / measure | `PROJECT_MEMORY.md §2` + `PLAN.md §10.2` |

## 流程 / 协作
| 关键词 | 跳去 |
|---|---|
| 先架构后编码 / 复制改造 / 别偷懒 | `topics/process-lessons.md` + `docs/mem/user-habits.md` |
| 个人规则 / 沟通格式 | `docs/mem/user-habits.md` |
| 最近决策点 / 现态 | `docs/mem/HANDOFF.md` 尾段 |
