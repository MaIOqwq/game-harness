# game-harness — 游戏社区问答 Agent

中文问句 → 冷启动判断 → 按需真爬社区（NGA + bilibili，关键词）→ 自动存档 →
检索合成 → **引原文作答**（`[id=N]` + 明说不确定性 + 末行「结论:」）。
定位：**问答类** Agent，舆情纵深是旗舰（非毕设那套「监测类」大屏）；答案口径 = 社区当下共识，
不是攻略站权威，必须 cite + 标滞后。正源：`CLAUDE.md` → `PROJECT_MEMORY.md` → `PLAN.md §10` → `docs/ARCHITECTURE.md`。

## 一句话卖点
`harness/` 核心是**纯 Python 标准库**实现（sqlite3 / json / re / urllib…），
零第三方依赖即可跑 demo、标定、自测与 guard。第三方依赖就 `requirements.txt` 里那两条：
`fpdf2` **必须装**（导出 PDF 用，不装 `/api/pdf` 直接报错），`tiktoken` 可选
（`harness/context.py` 精确 cl100k 计数用；不装自动退到 `len/1.7` 估算）。
安装：`pip install -r requirements.txt`。

## 工具与 MCP（双向）
自带工具（爬 NGA、爬 B站、官号探针、历史归档检索、情感打分）的正源是**一份**清单
`harness/tools/registry.py`，三个地方读同一份，故永不打架：引擎组工具面、MCP 服务端 `tools/list`、
网页「设置 → 工具」的开关。**关掉一件 = 它整个从工具面消失**（结构性防线，不靠提示词劝）。
可选工具（情感打分）默认关，本机没有打分服务时它也不会出现。

**两个方向都走 MCP 协议**（`harness/mcp/`，纯标准库，不引官方 SDK）：
- **我们对外是服务端** —— `python -m harness.mcp`（stdio），任何支持 MCP 的客户端都能连上来用这几件工具；
  网页后端另开 `POST /mcp`（HTTP），走的是同一套处理逻辑。
- **我们对内是客户端** —— 用户在网页「设置 → 外部工具」里填别人提供的 MCP 服务（本地命令或网址），
  它的工具就并进模型手边，名字带 `mcp__` 前缀。外部服务连不上/卡住不拖垮答题，只是那几件工具当下用不了；
  外部工具返回的内容**不是我们爬的样本**，引不成 `[id=N]`。
- 名字映射是**组名时留一张对照表**（`engine._ext_defs`），不是从压过的名字反推 —— 中文服务名会被压成一段
  哈希，反推不回来，硬换下划线还会让两个服务名撞成一个。

## 仓库地图（顶层）
- `harness/` 主链（数据层 + 引擎 + 会话 + runner 入口），含 `runner/session.py`（会话状态卡 L1/L2、记忆 L3/L4）
- `harness/tools/` 自带工具注册表（一份清单三处用）；`harness/mcp/` MCP 协议层（`server.py` 对外 / `client.py` 对外连别人）
- `harness/webapp.py` 网页后端（8780）+ `harness/webview.py`（把一题翻成页面认的形状）
- `harness/runner/` CLI 入口；`harness/data/` 会话记忆真源（SQLite + `flow/*.jsonl` 流水）
- `_syscheck/` 随包的离线自检（`verify_*`，被 `harness.test` 聚合）；源码仓库那侧的 `_tmp/` 是开发期脚本，不随产品走
- `docs/` PRD/架构/记忆笔记；`crawlers/`（爬虫副本工作区）、`nlp/`（评测工具链）、`labeler/`
- 说明：当前未 git 化（用户拍暂缓），文件仅落盘；发布物去敏后才考虑进 GitHub

## 怎么跑

```bash
# 1) 对话 demo —— 默认 mock 数据源, 断网可跑
python -m harness.runner                 # /help 看命令: /mode /game /correct /card /log

# 2) 真爬 demo —— 需本地隧道就绪(NGA 18770 / bili 18771, X-Token 在位)
python -m harness.runner --live

# 3) 一键自测 —— guard(数据层 append-only 扫描) + 随包的离线自检(_syscheck/); 退出码 0/1
python -m harness.test                   # 纯标准库、无网络
python -m harness.test --server          # 追加服务器门(需远端隧道 + LoRA token + DEEPSEEK 在位)
python -m harness.test --list            # 列分组与每脚本一句说明

# 4) 数据层只增守卫(单独跑)
python -m harness.guard

# 5) live 标定 —— 复用会话层逐题真爬; 每题独立 scope = exp:calib:<tag>:q<N>
#    隧道守卫起跑前探 18770/18771 /health, 全挂即拒跑
python -m harness.runner.calibrate --mock                 # 只验流程, 无需隧道
python -m harness.runner.calibrate --tag 20260908-xx --qsfile 题单.tsv --interval 360
#    题单 TSV: game<TAB>mode(快速|精准)<TAB>question  (支持 # 注释)

# 6) 跑批中间态观察(只读复盘)
python -m harness.runner.observe --help
```

## 凭证纪律（铁律级，别违反）
- **明文 cookie / token / 密码绝不进 chat、log、源码、git**；需要时只引用"环境里已存在"。
- bili 登录态 = 部署服务器上唯一 owner，**绝不拷到本地/仓库**。
- X-Token 只在本机运行着的机器上读；NGA config cookies 只在部署服务器上读。
- `DEEPSEEK_API_KEY` 可以**在页面上填** —— 那就落在**本机** `secrets.json`（与 `settings.json` 分开存）；
  页面上**永远只显示「已配置 / 未配置」，明文不回传**；**明文也不写进 `settings.json`**。没在页面填时仍按环境变量。
  发布物一律先脱敏。

## 数据与记忆纪律
- `harness/data/harness_dev.db`（`HARNESS_DB` 可换）是会话级记忆真源：L1 状态卡 / L2 checkpointer /
  L3 archive（crawl_run + observation + session_claim）/ L4 alias registry，全按 `scope` 隔离。
- 数据层**只增不改**：文件只读或 `"a"` 追加，SQLite 仅 CREATE/INSERT/ALTER —— 违禁原语由 `harness.guard` 扫描拦截。
- 主库 prod scope 留真实会话；实验/标定一律独立 namespace（`exp:calib:<tag>:qN`），防结论污染不可归因。
- 每 scope 一条 `flow/<scope>.jsonl` 流水：answer 全文 + 结论 + trace + usage，供复盘与黄金回放。

## 质量验证分层（对应 docs/ARCHITECTURE §10）
1. 确定性层（免费，不烧 LLM）：`_syscheck/verify_*.py`（随包那批）→ 已聚合进 `python -m harness.test`。
2. 黄金回放层（进行中 M1.2）：录真题 flow + 对应标定 DB → 逐 case 断言「结论在 / 引用对回本 scope 观察 / 诚实无据放行」。
3. live 认证层（M1.1）：标准题单 + 参数 + 判据，真网络窗口跑 —— 流程与题单见 `docs/CERTIFY.md`。

详细铁律（毕设原版一行不动、live 每题间隔 5-10 分钟、动服务器前先评估 OOM、单进程爬虫等）
见 `PROJECT_MEMORY.md §5` 与 `docs/mem/user-habits.md`。
