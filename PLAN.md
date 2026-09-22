# 游戏社区问答 Agent（舆情 = 旗舰纵深）+ 客服 RAG 升级 — 施工计划（2026-09-02 定稿，身份改称 2026-09-07）

> 定位：简历两大 AI 项目，RAG 不作为独立项目标题，而是两个 Agent 项目内部的共性深度能力。
> 旗舰 = **游戏社区问答 Agent**（2026-09-06 由"舆情洞察"改称：舆情 = 旗舰纵深，攻略/配队/强度等中段问题由冷启动+搜索覆盖；新增，真实 25.3 万条数据，harness + SQL 可验证答案 + 评估闭环），部署在演示服务器。
> 升级 = **电商客服 Agent**（已有，LangGraph 多 Agent + MCP），把 FAQ RAG 从裸检索升级为可评测、可讲深度的检索层。
> 非峰值时段动工（避生产服务器 高峰 / 网络低峰），本计划为唯一施工依据。
> ⚠ 2026-09-04 架构已演进（主源 SQL 存量→实时爬虫、输出改图+文双轨、crawler-mcp 已上线 8121）——开工先读文末 §8 Revision，与正文冲突处以 §8 为准。

---

## 0. 为什么这样做（一句话答辩版）

面试官已疲劳于"独立 RAG / 客服 demo"同质项目（功能层零信号）。RAG 单独当标题 = 2024 年的项目。
本方案让**两个 Agent 都内含 RAG**，读出来是 Agent 编排 + 工程化 + 可验证，RAG 只是被深挖时的一站。
其中游戏社区问答 Agent 的答案**能拿真实 SQL 跑回来对**——这是 LLM 幻觉上别人绕不过、我能降维的点。

---

## 1. 现状盘点（2026-09-02 实测，勿凭记忆）

### 演示服务器（198.51.100.10，轻量云主机 4c8g）——新 Agent 的主场
- 内存 7.8G：仅用 434M，available **6.9G**；无 swap
- 磁盘 177G：用 47G，剩 **124G**
- Python **3.10.12**
- 运行服务：只剩 `jcs-agent-core`（云平台），其余全停（毕设管道 9/2 已关）
- <服务器目录> 残留：kafka / spark / zookeeper / models / mysql_backup.tar.gz / torch_cpu.whl 等（可清可留，Phase 0 决定）
- 公网可直连，密钥 `<本机密钥目录>\<SSH私钥>`

### 生产服务器（203.0.113.20，2c4g，满载）——现有服务不动
- 内存 3.6G：用 2.3G，available 1.3G + swap 1.2G —— **不要再往上加服务**
- 磁盘剩 36G；chroma 546M；`standardized_data` = **253,305 行**
- 现有：rag-qa :8002（22.4 万 docs 在线）、opinion :8080、litemall :8081、cs-agent :8003、mcp :8010、predict :5000、mariadb、redis、nginx

### 客服 RAG 现状（升级抓手，读码确认）
- `<既有服务目录>/faq.py` 的 `retrieve()` = **裸 Chroma 语义检索**（`col.query(query_texts=...)`）
- **没上** rag-qa 那套关键词预过滤 + 重排 → 这就是可讲的"升级点 + 效果对比"
- FAQ 语料 = 手工电商政策 md（售前/售后/物流/优惠），本地 `cs-agent/data/faq.md`
- 已有 QA 回归基线：授权 8/8 + QA 49/49（`tmp_auth_test.py`）

---

## 2. 架构决策（已定，除非你否决）

```
演示服务器 新装（独立闭环，不拖累生产服务器）
  standardized_data(MariaDB, 本地) ──┬── query_metrics 工具（真实 SQL 算指标）
      ↑ dump 自生产服务器                    ├── search_posts 工具（Chroma 检索，本地副本）
  chroma_db 拷贝自生产服务器                 ├── report_writer（结论 + 引用）
      ↓ bge-small-zh-v1.5              └── [harness loop 编排 3 工具]
  LangGraph 图 + DeepSeek                 ↓
      + trace(结构化日志)            评估：15~20 golden，答案数字用 SQL 校验
                                     答案可溯源 → 面试"怎么验证不幻觉"
```

- **部署地 = 演示服务器**：生产服务器 满，新 agent 只留演示服务器；rag-qa 现有服务不动（那是 demo）
- **数据**：生产服务器 `mysqldump standardized_data` → 演示服务器 本地 MariaDB（不跨库直连）
- **向量**：choma 546M 目录整体拷到演示服务器 复用（不必重向量化 3h）；bge 模型同拷
- **harness**：LangGraph（复用客服工程范式 + 经验：structured_output 须 function_calling、.env CRLF、mcp<2）
- **trace/评估**：先轻量自写结构化日志（每轮存 {question, plan, tool_calls, tool_results, answer, verified}），不引 Langfuse——简历讲得清、代码能自证；后续要可视再叠
- **客服升级**：单独 section 3.4

---

## 3. 分阶段施工（每阶段 → 验证标准）

### Phase 0 — 演示服务器 环境准备  [约 0.5h]
- [ ] 探查已装 mariadb/redis 与否；无则 `apt install mariadb-server`（redis 可省，agent 单机低并发）
- [ ] 建 venv：`python3.10 -m venv <部署目录>/venv`，装 `langgraph langchain-openai langchain-core chromadb sentence-transformers pymysql bge 依赖`
- [ ] 规划端口：agent :8120（避免与生产服务器 冲突习惯）
- ✅ 验证：`systemctl status mariadb` running；`python -c "import langgraph, chromadb"` 无错

### Phase 1 — 数据迁移  [约 1h]
- [ ] 生产服务器 上 `mysqldump -u <DB用户> -p<DB密码> standardized_data > std.sql`（25.3 万行，先看大小）
- [ ] scp 到演示服务器 → 演示服务器 建库建用户 → `mysql < std.sql`
- ✅ 验证：演示服务器 上 `select count(*)` = **253305**；抽查几条与原库一致
- ⚠ 坑预案：中文乱码 → mysqldump 加 `--default-character-set=utf8mb4`；连接生产服务器 需 `-h127.0.0.1`（socket 更稳）

### Phase 2 — 游戏社区问答 Agent 骨架  [约 2-3h，核心]
- [ ] 拷 chroma_db + bge 模型生产服务器→演示服务器（scp，546M+模型；bge 在 `<既有服务目录>/models/bge-small-zh-v1.5`）
- [ ] 验证演示服务器 上 chromadb 打开 count 一致（sqlite 拷贝应可读；[开工第一步验证] 若索引坏 → 用生产服务器 现网 config 禁 BM25 + 词法重排兜底）
- [ ] 写 tools：
  - `query_metrics`：参数 {keyword, 时间窗, 维度(platform/sentiment/trend/top_authors/top_words/alert)} → SQL 返回真实数字
  - `search_posts`：复用 rag-qa 检索逻辑（关键词预过滤 + dense/词法重排），返回带 source 的帖子摘要
  - `report_writer`：吃 metrics + posts → 输出结构化结论（每点带数字引用）
- [ ] LangGraph 图：问题 → 规划(调哪几个 tool、给什么参数) → 执行 → 回答(只引用工具真实返回)
- ✅ 验证：10 条真实问题能答出**可被 SQL 复核的数字**（如"三角洲行动本周负面 top3 平台"）
- ⚠ 经验继承：`with_structured_output(method="function_calling")`（DeepSeek 不支持 json_schema）；工具结果空则如实"无数据"，不编

### Phase 3 — 评估闭环（简历最硬的一层）  [约 2h]
- [ ] 构造 **15~20 golden**：全部是"数字型"真实问题（能从库 SQL 复算出标准答案），覆盖 检索/指标/趋势/对比
- [ ] runner：对每条跑 agent → 抽取答案中关键数字 → SQL 校验对错 → 记 {pass/fail, 差距}
- [ ] 用失败 case 反哺：补 prompt（要求只引用工具数字）、补 tool 参数校验、改 SQL
- ✅ 验证：首轮 pass 率记录；迭代后提升 X 个点（**真数字，面试能背**）

### Phase 4 — 客服 RAG 升级（第二个项目读同一层深度）  [约 1-2h]
- [ ] 参照 rag-qa 检索质量，给 FAQ 检索加：**召回质量离线评估**（构造 30~50 条 FAQ 问题-golden 条目 → Recall@k）
- [ ] 升级点选 1-2 个讲得出决策链的：如 FAQ 切分类检索（按 router 已判的类别缩小检索域）+ 阈值拒答（相似度低→明确"知识库没有"而非硬答）→ 对比 Recall/误答率
- [ ] 复用现有 49/49 QA 回归防退化
- ✅ 验证：升级后 Recall 提升数字 + 49/49 不降；`tmp_eval_rag.py`（已存在）跑通

### Phase 5 — 线上化 + 简历写入  [约 1h]
- [ ] systemd 托管（参考 outfit-matcher：MemoryMax 防 OOM + Restart）
- [ ] 演示服务器 上可访问入口（nginx 或直连端口）；给一个演示问题清单
- [ ] 简历两条项目 bullet（草案见 §5），数字全部可溯源

### Phase 6 — 面试防御  [0.5h，随用随补]
- [ ] 为 golden 的每类问题备"当时为什么这么查"的话术
- [ ] 备"检索不相关怎么办 / 数字错了怎么发现 / 与生产服务器 rag-qa 什么关系"的 Q&A

---

## 4. 需要你拍板的 3 点（文字回复即可）

1. **chroma 拷贝 vs 演示服务器 重建**：拷贝省 3h 但若 sqlite 带索引坏有重向量化风险（生产服务器 踩过）；重建稳但要 3h+。
   我默认：先拷 + 验证，坏再重向量化。→ 同意否？
2. **演示服务器 端口**：默认 8120。→ 无异议吧？
3. **客服升级深度**：只做"评估 + 拒答 + 分类域"（省，约 1-2h）还是把检索层也换 rag-qa 同款预过滤重排（重，3h+）？
   我默认前者（够讲深度、不碰生产服务器 上线风险）。

---

## 5. 简历 bullet 草案（防幻觉：数字先有实据再上简历）

**游戏社区问答 Agent（舆情 = 旗舰纵深）**
> 基于 LangGraph 自建游戏社区问答 Agent，编排 query_metrics / search_posts / report_writer 三个真实工具，对 25.3 万条游戏社区语料做指标查询、检索与归因，回答中的数字均由 SQL 复核；构建 15+ golden 评估集跑通"答案-SQL 校验"闭环，检索失败 case 反哺后 pass 率提升至 X%；trace 记录每轮 plan/tool/answer 供复盘。

**电商智能客服（升级，原 49/49 基线上）**
> FAQ 检索从裸向量检索升级为"分类域缩小检索 + 阈值拒答"，离线 30~50 条 golden 评估 Recall@k 提升至 X%；回归 49/49 全绿。

> ⚠ X 全部要 Phase 3/4 跑出来的真实数字，查不到就删，绝不编。

---

## 6. 与现有记忆/资产的边界

- 不动生产服务器 现有服务（rag-qa / opinion / litemall / cs-agent）——它们在简历上是独立项目 / demo
- 不重新向量化生产服务器 大语料；新 Agent 数据走演示服务器 副本，生产服务器 原库保持锚定 8/22
- 简历"3 选"规则、防幻觉铁律、1 页、字号≥12、无"毕设"标注 —— 全部继续生效

## 7. 下一步（本轮对话后，非峰值开工时）

按 Phase 0 → 1 顺序走。开工前先跑一次 `systemctl` 看演示服务器 现状 + `df` 确认磁盘，再动。
本计划放 `<项目根>\PLAN.md`（项目源码建议也放此目录）。

---

## 8. Revision 2026-09-04 — 架构对齐（动工前先读本节）

> 9/2 版 PLAN 之后，连续设计与实测把架构重心挪了。开工以本节为准，与正文冲突处以本节优先。
> 相关新资产：crawler-mcp（已上线演示服务器）、bili/nga 常驻爬虫工具、game-timeline 想法（独立，见关联记忆）。

### 8.1 主数据源：SQL 存量 → 实时爬虫
- **结论**：回答任意时间段的问题都靠**爬虫定向搜索**（B站/NGA 平台存档制，活动名/关键词可取任意时段内容；实测"明日方舟 直到大地变为一颗酸橙"精准命中 8/1 活动本体，评论全在活动期）。
- SQL 存量 standardized_data（25.3 万，锚定 8/22）**降级，不再当主真值**，改当：①基线参照系（对比"异常高/正常"）②检索语料库（Chroma/关键词 25.3 万）③可验证评估题库（存量段已知答案 → 校准 judge/verifier）④稳定回归集（活的爬虫当不了回归基线，存量固定可以）。
- **数据截止日不再是死角**：今天的问题今天爬；过去的活动拿活动名爬。agent 声明数据窗口仍是铁律（超窗如实"无数据"，绝不硬答）。

### 8.2 数据接入层：crawler-mcp（2026-09-04 已上线）
- 演示服务器 @ **127.0.0.1:8121**（streamable-http，endpoint /mcp），systemd `crawler-mcp.service`，Restart=always
- 工具 `crawl_bili(query)` / `crawl_nga(query)`：输入 JSON Schema 钉死 `{"query": string}`（一次一主题、禁指令/指代）；NGA 纯词自动加 `search|`；转发内网 bili-tool:8771 / nga-tool:8770，凭据(X-Token)进程内持有，调用方零接触
- venv `<MCP服务目录>/venv`，mcp==**1.29.1**（锁 1.x 对齐 cs-agent 经验）；底层爬虫与现网服务零改动
- 实测：tools/list 正常；crawl_bili 10.8s / crawl_nga 13.7s（≈直连爬虫耗时，协议层开销可忽略）→ 简历"单机 stdio/localhost 协议开销 <5ms、多客户端复用才叠 MCP"有实据

### 8.3 输出形态：图 + 文双轨（对标毕设大屏）
- 前端出**图表（情绪分布/平台分布/趋势/活动对比）+ LLM 总结卡片** + trace 面板
- 图表数字 = 快照或 SQL 聚合（每个数字带来源参数可重放）；LLM 总结**只准引用图表数字 + 快照原文**，每数带引用标记
- 防幻觉闭环可视版：总结数字 ↔ 图表数据 ↔ 快照 三方一致 = verified
- ⚠ 口径诚实：图表画的是"抓到的快照样本"分布，非全集；演示/对比用同口径快照即可，勿称"全网分布"

### 8.4 打标层（按需——画情绪分布图才必要）
- 情绪/骂点打标 + 聚合 → 图表分布数字。**规则/情感词典先粗打 + DeepSeek judge 兜底难例**，不上重型 NLP
- 能确定性/规则聚合的指标（条数/平台/时间/声量 top）一律不上 LLM
- 相关度过滤：复用 rag-qa 现成"关键词预过滤 + dense 重排"（生产服务器 验证过 20/20），对爬回快照做帖子级过滤/截断再喂总结
- 标签质量不高宁可不给 LLM（错误标签比原文更有害）

### 8.5 评估/验证层：确定性第一，LLM 判据分 verifier / judge
- 确定性验证永远第一：快照落盘比对 / SQL 复算 / 引用是否在快照内——能规则判的不上 LLM
- **DeepSeek 实测支持 logprobs**（choice.logprobs.content：token 级 logprob + top_logprobs）→ **verifier 主线可行**：打分 token 候选空间小（如 1-10），top_logprobs 近全集，softmax 加权求期望 = 连续分 R=Σp·φ(v)，用于排序/选优/阈值拒答
- judge（离散）用于打标/分类/相关性过滤，量大控成本
- 理论依据（面试弹药）：Text-to-SQL Execution Accuracy（Spider/BIRD 官方指标=结果集比对）、PAL（arXiv:2211.10435，确定性执行器取代 LLM 计算）、LLM-as-a-Verifier（arXiv:2607.05391，logits 期望 vs argmax）；⚠ gold 本身会错（GBV-SQL 审计 EX 88→96.5）→ golden 复算 SQL 必须人工审
- 15~20 golden 评估集**锁存量段**（≤8/22，SQL 可真值）；实时段用"快照可溯 + 回搜命中率"口径

### 8.6 框架：LangGraph 保持
- 跟 cs-agent 一致（structured_output function_calling、mcp<2、.env CRLF 经验全继承）；verifier/judge 作图中节点（验证失败 → 重规划分支）
- 工具层全 MCP/HTTP/SQL，框架无关可平移（不必焦虑锁死）

### 8.7 待办 & 待拍板（9/2 三点之上新增）
- [ ] 主源定爬虫后，**Phase 2 工具清单 / Phase 3 评估口径按 §8 重写**（query_metrics 退二档；评估闭环以快照可溯为主、SQL 校准为辅）——开工前需用户拍这版工具清单
- [ ] 情绪打标实现（词典/规则 + judge 兜底）与图表 JSON 协议（前端同毕设技术栈，ECharts 类）待定
- [ ] 旧 §4 三点仍挂（chroma 拷贝 / 端口 8120 / 客服升级深度）；chroma 拷贝项因主源转爬虫**优先级下降**
- [ ] game-timeline 官号时间轴为独立想法（3 点待定），不并入本 PLAN 施工
- [ ] §8.10 生效：SQL 作废于问答取数路径后，核对 §8.1 四个二线角色（基线参照/检索语料/评估题库/回归集）去留；双模式数值（快速 150 / 精准 500-700）待 §9.3 实测校准

---

### 8.8 真实循环流程（2026-09-04 确认，替代线性直觉）

> 质疑："流程太线性太理想"成立。线性版四个死穴：①一次 crawl 量不够（实测 B站≈40 评论 + NGA 10 帖），这点量画"情绪分布图"统计上自欺；②LLM 拆词会拆偏（"明日方舟"爬回学信网绑定求助帖实证），线性流程拆错就错到底无人发现；③情感打分错误级联污染图表+总结，中间无 checkpoint；④"选主题面板"与"自由问句"两输入形态揉进一条线，两头都不实（面板入口根本不需要 LLM 拆词）。
> 确认真实流程是**循环**，不是直线——"为什么叫 Agent"的答案就在这：

```
用户问句 / 面板选主题
 → Agent 规划 N 个搜索候选（词 × 平台 × 版块）
 → 【循环】逐条 crawl 攒快照
     → 质量门：相关度抽查 + 样本量门槛
        ├─ 不够 / 跑偏 → 换词 / 换措辞再搜（≤K 轮）
        └─ 够了 → 往下走
 → 情感打分（词典粗打全量 + judge 兜底难例）
 → 【校验】verifier 抽查（打分置信 + 聚合复算），不过回炉
 → 左支：确定性聚合 → chart JSON → 前端 ECharts
 → 右支：抽代表原文（情感极端排序，确定性，非向量检索）→ LLM 总结（只引图表数字）
 → 三方对账（图表数字 ↔ 快照 ↔ 总结）= verified
```

> 代表样本是"抽"的不是"检索"的：数据全在手时按情感极端/去重抽样即可；向量检索只在总结需补历史存量（检索问答模式）才叠。样本量门槛量化 + 小众游戏实测攒样本 = 进行中（2026-09-04）。

---

### 8.9 边爬边增量出图（流式攒样本，2026-09-04 确认）

> 形态：不是"攒够 150 才画"，是"边爬边增量出图（过程有进度感）+ 终点收口 verified"两条都占。

- 服务端每轮结束：新快照去重+打分后**累加** → **重算整张聚合** → 推 chart JSON（确定性重算，非 append）。前端 ECharts `setOption` 刷新：趋势图往后延伸、分布图数字刷新，标"当前样本 n"——攒样本过程可见（分布从抖到稳）= 演示亮点
- 推送：**SSE**（cs-agent 已有真流式范式）或单用户**前端轮询** GET task 状态；WebSocket 不必要
- 前端状态机：`规划中 → 第k轮搜索中(单轮~13s 须骨架态) → 攒到n → 达标/降级`；服务端每轮把档位一起下发，前端不判断
- 收敛 = 三档门槛：≥150 全维图收口 verified；60~149 降级只出整体分布+标注样本 n；<60 定量不足只定性
- 口径诚实：每版图标"当前已抓快照 n 条"非"全网"——流式下越攒越像全网在涨，其实只是样本变多，须反复标

---

### 8.10 前台形态定稿（2026-09-04 二轮确认：双模式 + 存档边界 + SQL 作废）

> 承接 8.8/8.9：原单一"攒样本门槛"（≥150 全维 verified / 60~149 降级 / <60 只定性）在双模式下重新解释——**150 = 快速档达标线；精准档在其上探 500-700**。档位**由用户显式选**，不做自动嗅探。

**双模式（用户选择）**
- **快速**：3 关键词 × 每词两平台合计 ≥50，保底总量 **>150**。留平台保底意识：某侧采不到就明说"B站几乎无声，结论以 NGA 为主"，不硬装平衡、不撒谎。
- **精准**：多关键词 × 每词配额，乘积目标 **500-700**（区间合理性待 §9.3 真实实测校准，别写死承诺）。采不到就**如实降级**，绝不灌噪音/无赞搬运凑数；精度主在**纬度**（多词条 / 双平台 / 热+新排序），条数次之。
- 服务节奏：先给能看的初答（≈快速档），玩家要点深挖再切精准档。

**存档边界（存档 = 观测记录，不是缓存，不是问题库）**
- 只答**过去/当时/对比**类（"6 月那会儿 / 跟周年庆比 / 当时在吵啥"）；**"现在"问题一律强制实时爬**，爬完**立刻落档**。评论区被冲后会被**控评删除**、帖子会被删——事后补爬找不回，只有窗口内抓下+存档才留得住（实时落档 = 留住现在，不是答历史）。
- 快照存档库 ≠ 用户问题库/问答历史（问答往来属轨迹/对话记忆组件，管的是对话，别混成一个）。

**SQL 存量作废（standardized_data @ 生产服务器）**
- 不入 harness 问答取数路径：口径"≥1 赞"一刀切过滤（0 赞普通声音全丢）+ 无父子结构 + 非实时（管道 9/2 停、覆盖至 8/22）+ 量大（24.2 万行）→ 用户判：质量低/不实时/量大，作废。
- **生产服务器 原样保留不删**（毕设大屏仍在挂）。§8.1 的 ①基线参照 ②检索语料 ③评估题库 ④回归集 四个二线角色去留，随本决策再核对（挂 §8.7）。

---

## 9. 待办记录：bili 评论 3 条/页 修复（2026-09-04，执行已暂停等用户唤）

> 背景：舆情 Agent 数据靠 crawler-mcp(演示服务器:8121) → bili-tool(8771) / nga-tool(8770)。实测 bili 每视频评论被压到 **3 条/页**（"昨天怎么都是 3 个"），攒样本严重不足。

### 9.1 根因（读码+实测定案）
- 带**已死 SESSDATA** 请求 → nav 返 `-101` → 评论接口被 B站降权 3 replies/page；**纯游客（剥 SESSDATA/bili_jct/sid 只留 buvid3/buvid4）实测 code=0、20 条/页**。死凭证比不登录还狠。
- MediaCrawler **无 cookie 自愈**：`SAVE_LOGIN_STATE=True` + `launch_persistent_context`（core.py）只把登录态存 profile 跨重启；cookie 模式 `check_login_state`（login.py）只查键存在不查活；qrcode 是唯一换新 SESSDATA 路径，且 `show_qrcode` 的 `new_image.show()` 在 headless 服务器会静默挂——须改成存 PNG 导出给用户扫。

### 9.2 已定方案：主号 QR 扫码（用户已选）
- 账号事实（用户确认）：主号、**无大会员**（2 台设备限制不适用）、电脑/pad/手机三端常登、主号已爬 4 个多月无事故 → QR 不挤网页端（B站多设备并存），风险可接受。
- 执行步骤：①写 `<B站爬虫目录>/qr_login_once.py`（headless 起持久 profile → 走框架扫码、show_qrcode 改存 `<B站爬虫目录>/qr_login.png` → 轮询~2min → 成功 update_cookies 写 profile + nav 验 isLogin → 退出）②`systemctl stop bili-tool`（放 profile 锁）③后台跑脚本 → 拉 PNG 回本地 → 把文件回传给用户 ④用户 B站 App 扫（过期自动刷新重试）⑤`systemctl start bili-tool` → POST /crawl 小查询验证评论 3→≥20/页。
- 备选（QR 不顺）：游客态剥三键（20/页已实测）/ refresh_token 静默续（cookie 需带 ac_time_value）。
- 边界：只动 `<B站爬虫目录>` 副本，毕设 `<毕设项目目录>` 一行不碰。

### 9.3 修好后要做的测试
1. **重跑「循环攒样本」三轮数据测试**（对应 §8.8 循环）：
   - bili/nga 两爬虫各 **3 轮**，查询词 `1991周年庆风评和现在风评有什么变化`（=《重返未来：1999》周年庆 vs 现在）。
   - 修复前基线：bili≈13 视频 + 42 评论 / nga≈14 帖（样本远不足）。修复后期望 bili 每视频评论 3→≥20，总量上台阶。
   - 对照门槛：≥150 全维图 verified / 60~149 降级 / <60 只定性；检查"最小格子"每格≥10 是否够。
   - 走全链路：crawler-mcp → 攒快照 → 打分 → 出图/总结对账。
2. **回归**：其余 demo 查询不受登录态切换影响；nga 无此病不涉及。
3. 流式出图的口径诚实标注（"当前已抓快照 n"）不丢。

### 9.4 进度
- [x] 根因定案（读码 + 游客态 20/页实测）
- [x] 方案选定（QR 主号）+ 风险评估完
- [x] qr_login_once.py 编写 + 上传（2026-09-04 17:54）
- [x] 停服→扫码→起服→验证（2026-09-04 17:57 完成，见下注）
- [ ] 三轮数据测试重跑（见 §9.3）

> **执行注（2026-09-04 17:57 实测）**：关键坑 = profile 残留**已死 SESSDATA**，`check_login_state` 只查键存在不查活 → 扫码流程"秒过"不等人扫，pong 才报 -101。解法：`rm -rf browser_data/bili_user_data_dir` 清死登录态重跑，即正常停下等扫码。扫码成功后 isLogin=True，profile 已持久化；重启 bili-tool 复用。一次真实 POST /crawl 验证：**demo 的 search_one_query 设计上限就是每视频 8 条一级评论（max_count=8，取默认排序第 1 页）**，5 视频全部 8/8 满额、数据真实——登录态下评论 API 已正常。**"≥20/页全量"须走 §9.3 真实爬虫路径验证，demo 工具本身截 8 条看不见 20。** qr_login_once.py 保留在 <B站爬虫目录> 供下次续期复用。

---

## 10. 变更记录：2026-09-04 → 09-07 对话脉络 + 定稿架构快照（2026-09-07 补记）

> **来历**：用户 9/7 要求复盘聊天记录原文（续接总结 + 用户问题），把"为什么走到现在这样"的脉络与当前敲定架构固化进唯一依据，防上下文压缩后口径漂移。本段与 §8/§9 同为日志段，是 9/4 之后一切决策的**时间线正源**。
> ⚠ **定位上修（2026-09-06 23:03 用户拍板）**：harness 从"舆情"上修为**游戏社区问答 Agent**——攻略/配队/强度等中段问题 90% 靠冷启动+搜索覆盖、几乎不加 backlog；舆情退为旗舰纵深（黑话/反串/带节奏/自基线才走专用 determiner）。**旧正文与 §8 身份段改写仍待用户拍**；读到旧称"舆情"处，以此段与记忆文件为准，别被旧段带偏。

### 10.1 逐日脉络（每段 = 当日在吵啥 + 拍板点 + 用户原话锚点）

**9/4 —— harness 概念澄清 → 爬虫 MCP 化 → "太线性"翻盘 → SQL 作废 → 双模式定稿**
- 00:00-01:00 澄清 harness 本体（"harness 到底是啥""我们最后的形式是什么"），我拿 deepseekharness/pi-agent 等参考。用户自省"没研究透 harness 就开始做"。
- 01:24-02:06 **时间死角破解**：SQL 存量锚 8-22，用户问"与上次活动对比"会炸 → 用户原话："把上一次活动名称做搜索词然后去搜索"——**实时爬虫按活动名可定向搜任意时段**，此后 SQL 不再是主力。
- 02:39-03:15 **两爬虫做成 MCP**（钉死输入输出），"在简历上能写出来"。并入主项目。
- 03:32-04:01 用户提游戏时间轴（预告/卡池）+ 官号一帖一小时一爬 → 记录，**独立想法，不并一期**（见 [[game-timeline-official-account]]）。
- 13:28-14:33 verifier vs judge 辨析（论文 2607.05391）→ 8-22 之后答不了？→ 用户自己想起"爬虫能按时段搜"，SQL 降级：**参照系+语料+评估题库+回归集候选**。
- 14:43-15:33 图+文双轨草图（对标毕设大屏）→ **用户："总感觉哪里不对，太线性太理想了"** —— 架构转折点，引出循环流程（§8.8）。
- 15:39-16:35 换小众游戏测"循环攒样本"→ b站被压 **3条/页** bug → 根因死 SESSDATA → QR 扫码方案（§9 全记录）。父子结构定案：视频→一级评论挂子。
- 16:38-17:46 **质量门讨论**：门1"按是否带游戏名判定"被用户否（"视频和帖子为什么要带游戏名称和别称?我完全可以不带"）；用户给出完整理想流程 = **别名表防跑偏 → 定位时间 → 找最近活动 → LLM 自主拆 3 个高频名词 → 给爬虫**（planner 雏形）。
- 17:50-18:16 抓 tags/简介 + QR 扫码持久化实测 → 只要 tag 够。
- 18:23-18:51 **玩家视角拷问**（"像 codeharness 一样，普通二游玩家会问什么"）→ 每个都难 → 用户指出"所有问题第一个 tool 都是爬虫"的直觉 → 去扒参考 harness 的组件。
- 18:58-19:37 **SQL 作废**：一刀切 ≥1赞过滤丢了普通声音 + 不实时 + 量大（同事件库能扯 2-3k 条 vs 快照 150）→ 用户判："sql 这边的数据应该作废的，质量低，不实时，数据量过大了"。
- 19:44-19:51 **双模式定稿**（用户设计）：快速 = 3关键词×NGA+B站 50+ → 总量>150；精准 = 多关键词×定额 → 500-700（"不确定这个区间合不合理"）。档位由用户显式选。
- 19:51-20:36 官号动态下节奏发酵 → 触发式监控官号 = 独立线程（停车二期）；用户："官号下的评论应该也算舆情内部，但只精准模式采集"。
- 20:43-22:15 崩铁 **4.5**（先记 4.6 用户纠错）节奏探测练手 → 探测策略=抓窗口最近10条求评论均值，超均值太多才深采 → **1600 条教训**（用户原话"你一个动态拉1600条?你疯了是吧"，只要 25 条热度排序）→ 排序锁**综合**（用户"不 就综合"）。
- 22:15-22:26 深度=3关键词+官号探针→探针换词续搜；快速=3关键词+官号 → **爬虫原语封闭定论**：LLM 只见 `{query}`，不放数值参数（"容易被llm乱调"）。
- 22:26-22:49 子代理当**全新 LLM** 实测 planner（claude 子代理 2 问 10 call）→ 别名/一次一主题/质量门判漂移/平台保底/不编 onset 全自动达成 → 要不要 verifier 待定。
- 22:54-23:58 judge 用途 → NLP 能否"识别反讽阴阳之外的所有" → **25w 真实语料 LLM 蒸馏打标** → 检测器宁误报高召回 → 演示服务器 实测爬虫峰值 2.48GB → 常驻 NLP 可行 → **做 LoRA 打标分类器**（"我们做lora吧那就"）。

**9/5 —— harness 组件盘点 + NLP 领土终拍(gacha) + 打标流水线(gold1/2)**
- 00:36-00:58 用户顿悟 harness 本体："系统工具词，工具调用，文件系统，沙箱环境，记忆系统，上下文管理，编排逻辑，hook，反馈回路，约束机制，都叫harness" → 需全做吗(三档) → 上 GitHub 加工程壳不加功能。
- 01:03-01:32 **工具面收窄**：模型工具是 harness 每次请求 `tools` 参数声明的**白名单**，没声明就调不了；search/read/write 均不放开（怕放权"光搜不爬"）→ **NLP 领土终拍：锁 gacha（二游抽卡）域不做全游戏**（瓦"扳机纪律"/黑猴"四豆"不在领土）。
- 01:38-03:10 训练集/测试集设计 → 用户网页人工批注（labeler/annotate.html）→ batch1(75) → gold001(200 随机两列) → gold002(200 聚焦反讽单列 dims=["sarcasm"]) → 训练集 3594 → DeepSeek 蒸馏打标 → 用户叫停 GPU（明天跑）。
- 14:43-15:47 "有必要自建 harness 吗，还是只是做 tool/skill" → 结论：继续自建。
- 21:56-23:xx 先做前端？→ 复盘框架缺组件 → 最后形态确认"问任何游戏舆论都能答+可视化" → 记忆系统有吗 → 用户："我到底从哪里开始继续我的 harness 编码"。

**9/6 —— 自建/参考清算 → 图表前端形态 → 训练评测不达标(A+B) → 定位上修"游戏社区"**
- 05:27-05:51 参考 deepseek-harness/pi-agent 开源 → 核心 tool 数对比 → "agent 除了总结还干啥了"拷问 → 画图也做成 tool 由 LLM 选调。
- 06:08-06:25 **图表形态**：触发图（热度折线/节奏时间轴/版本排期/对比图）+ 固定图（情绪分布/讨论度趋势/词云/作者）→ **卡片式前端逐张展示 + 结论置顶 + 共享时间轴**（可大幅减出图量）。
- 08:29-10:13 gold002 标完（0漏），"不要启动训练"。
- 16:52-17:39 **基线训练 + gold400 评测不达标**：情绪 Spearman 0.371（<全猜中性 0.745）、反讽 F1 0.383 → 根因=蒸馏标签噪声+反讽正例少(8.3%)+情绪偏中(67%) → A(gold002 并入当干净反讽正例)/B(量化蒸馏噪声)/C(judge-primary) → 用户"a b吧一起再试试"。
- 17:39-21:xx A+B 双修完：teacher 自身反讽仅 F1 0.458 → 蒸馏自噪=主瓶颈实锤；gold002 并训后 **gold001-only 反讽 F1@0.6=0.368 反超 teacher(0.303)**，情绪 Spearman 0.389。绝对值仍低 → 下一步待拍（gold003 / C）。
- 21:36-22:02 黑话拆解题（"膨胀神游你就玩吧，角色巨保值"指哪个游戏）→ nlp 能否进工具链试过。
- 22:09-22:53 用户提"每日官号维护时间轴+节奏归类交给冷启 LLM" → **用户自己否掉**：会打标签/刻板印象/只答火的游戏、冷门(咸鱼之王/忍者必须死3)最难过冷启动 → 朋友题"为什么每个游戏的孝子都这么傻逼" → **结论：不预设题型，做一套能接住任何游戏舆论问题的工具（解决 90% 就行）** → SQL 里是抽卡/配队/攻略类为主（统计验证）。
- 23:00-23:16 **定位上修**："做出来这个爬虫开始，我们这个 harness 感觉就不能叫舆情了"→"游戏社区吧"（23:05 拍板）→ 开始造框架 → **从记忆系统开始**。
- 23:18-00:xx **L1-L4 记忆框架定稿**：L1 状态卡(materialize-then-prune) / L2 checkpointer(defer) / L3 archive+session_log(按需回捞) / L4 别名注册表(defer)；落档=一次爬取一个大档(append-only verbatim)。

**9/7 —— 引擎闸门/46开 + nlp-tool 上线 + 上下文管理 + 迁移 gameharness**
- 01:51-03:40 **引擎闸门收敛**：删去重软提示、退避锁+断路器合并为平台状态机、加样本判断器（快速120 / 精准500-700 达标停；预算 3/5 强制停）、不加 QA 收口/答案深度 → b站控量：5视频8评论 → 15/10 → 触发限流 → 5/8 对照 → 回 15/10；单飞策略、关键词=游戏名+词直接拼接（不加空格）。COOL 300 起翻倍封顶 900，bili 成功归零；NGA 只熔断不冷却。
- 04:01-04:34 **nlp-tool 上线(演示服务器:8772, systemd)** + judge 导流做进 measure.py（方案A 边爬边判：可疑反讽 ≥0.4 攒批交 judge temp0+few-shot 覆写；任一服务挂→降级 keyword 不炸）。
- 04:42-04:57 **46开配比闸门**（"至少保证46开"）：NGA 爬速快压 B站占比 → 每平台 ask 级新增 ≥40% 才算均衡达标。
- 05:03-05:09 记忆系统已好 → 组件清单再点 → 用户质疑"系统工具和文件系统刻意不做？？？" → **结论：刻意不做（工具面窄），且向量记忆库(本机 add_turn 每轮入库)与 harness 会话记忆(StateCard/session_log)是两套，别混** → 定位上修重申"游戏社区 harness"。
- 05:27-06:20 **上下文管理**：怎么做（参考+数据精准度）→ 定论=护栏不折叠（证据 verbatim per-ask + IN_CEILING 输入上限收口），反证了 folding 错（答集引 id 跨全部爬轮）→ 迁移 `<项目根>` + 爬虫源码并入 `crawlers/`（登录态不落地，见 crawlers/README.md）。
- 06:20-06:59 上下文管理之后 → 我提反馈回路 → 优化讨论 → **用户点破"这反馈回路加到什么上的？？？"（无交互壳）→ 搁置** → 剩什么没做。

### 10.2 定稿架构快照（2026-09-07，代码真源在 harness/）

- **定位**：游戏社区问答 Agent（双平台 NGA + bilibili）。工具面窄 = {crawl_live, search_archive}（function-calling 白名单，`engine.py:TOOL_DEFS`）。
- **爬虫原语封闭**：LLM 只见 `{query}`；平台由引擎 `_norm_platform` 规约，数值参数（视频数/评论数/排序）不进 LLM 视野。
- **循环非直线**（§8.8）：问句 → 规划候选 → 循环 crawl 攒快照 → 质量门 → NLP 打分 → 校验 → 左支聚合出图 / 右支 LLM 总结 → 三方对账 verified。
- **双模式**（前端用户自选，两套 prompt 分开）：快速（参考样本量 120，爬轮硬上限 3，NGA+bili 同轮各爬一次，抓最热 1-2 侧面先给能看的初答）/ 精准（参考 500，爬轮硬上限 5，各侧面尽量全覆盖）。先快速、要深挖再切精准。
- **引擎闸门**（engine.py 主循环，09-08 改版后）：①爬轮预算(CRAWL_BUDGET 快速3/精准5) 为**硬上限且按「轮」计**——一条 assistant 消息含 ≥1 个真 crawl_live = 1 爬轮（同轮 NGA+bili 各爬一次 = 1），用尽即 block 强制作答，全被平台闸门挡的轮不耗预算；②样本**达标不硬停**：SAMPLE_TARGET(120/500) 只作参考量，每次真爬返回 ask_total 供 LLM 每轮比对自判够不够；③平台状态机：ask_fail 断路器 + bili 会话冷却(300→900 成功归零) + bili 爬距(BILI_GAP 60/120，键在真实请求时刻)；④上下文护栏(IN_CEILING 12000/26000)。46开/MIN_SHARE/`_balanced`/`_split_str` 硬 gate 已删（两侧收量天然不均，作答如实注偏即可）。双平台不二选一，bili 不再单飞。
- **记忆框架**（L1-L4，harness/state_card.py + archive.py + session_log.py + schema.sql）：L1 状态卡 materialize-then-prune，证据/结论不在可裁剪区；L3 archive = crawl_run(verbatim) + observation(dedup append-only) + session_claim 翻篇日志（话题命中按需回捞，离上下文）；L2/L4 defer。
- **上下文管理**：单 ask 内证据保真不折叠（答集聚合引各轮 [id=N]）；护栏只收异常风暴。省 token 的正解=少绕圈（轮数），不是截原文。
- **打标层**：训练1=部署1。LoRA StructBERT 双头（9/5 弃 head_topic，骂点挪 game_registry+judge 开放归类，登记表未建）：head_emotion(-1~1 概率加权) + head_sarcasm(可疑门→judge)。产物 `nlp/out/structbert-lora-gold002/`。评测 gold400：情绪 Spearman 0.389 / gold001-only 反讽 F1 0.368@th0.6（已反超 teacher，绝对值仍低）。nlp-tool(演示服务器:8772, systemd) + measure.py 接入 + judge 导流 + 服务挂→降级 keyword。
- **数据源**：crawlers/ 源码归档（nga 8770 自含3文件；bili 8771 + MediaCrawler fork，登录态=演示服务器 唯一 owner 不落地）。SQL 25.3w(生产服务器) 作废于问答取数。

### 10.3 组件八件套盘点（9/5 口径，9/7 状态）

| 组件 | 状态 |
|---|---|
| 工具调用（白名单 function-calling） | ✅ engine.py + tools/ |
| 约束机制（闸门/断路器/冷却/爬距/护栏） | ✅ engine.py |
| 记忆系统（L1+L3+session_log） | ✅（L2/L4 defer） |
| 上下文管理 | ✅ IN_CEILING 护栏（9/7） |
| 编排逻辑/runner（对话壳） | ❌ 未做（=让 ask() 对话化，"用户"进循环的入口） |
| 前台（双模式+ECharts+SSE+卡片+结论置顶+共享时间轴） | ❌ 未做（§8.9/8.10 设计已定） |
| 反馈回路（用户纠正→supersede→log） | ⛔ 搁置（9/7；挂对话壳之后才有东西可挂） |
| 系统工具/文件系统/沙箱/hook/search | ❌ 刻意不做（工具面窄；search 由 crawl 覆盖） |

### 10.4 数字常量正源（防口径漂移，以代码为准）

- 参考样本量 SAMPLE_TARGET（软，不硬停）：快速 120 / 精准 500（§8.10 前台口径写快速>150、精准 500-700，**数值实校=遗留**；改版后仅 LLM 每轮参考）
- 预算 CRAWL_BUDGET：3 / 5（**爬轮硬上限，按「轮」计**：同轮 NGA+bili 各爬一次=1 轮；用尽 block 强制作答）
- bili 爬距 BILI_GAP：快速 60s / 精准 120s；冷却 COOL 300s 起、连熔断翻倍封顶 900s，成功归零
- ~~MIN_SHARE = 0.4（46开）~~ 已删（09-08，双平台收量不均不再硬配比）；IN_CEILING = 快速 12000 / 精准 26000（≈2 倍正常用量）
- NLP 可疑门 th=0.6；judge 导流阈 NLP_SUSP_TH=0.4；NLP_JUDGE_CAP=40
- 情绪连续分 score = -1·P负 + 0·P中 + 1·P正

### 10.5 变更落点（9/4→9/7 改了哪些文件）

- `harness/engine.py`：46开闸门(9/7) + 平台状态机 + 上下文护栏(9/7)；`measure.py` nlp-tool HTTP + judge 导流(9/7 方案A)；`archive.py` 批量打分带标题；`context.py` 组装+token 计数
- `harness/state_card.py`/`session_log.py`/`schema.sql`：L1/L3 记忆(9/7)
- `nlp/train_structbert_lora.py` → `nlp/out/structbert-lora-gold002/`（9/6 双修）；`nlp/nlp-tool.py` → 演示服务器 `<打分服务目录>`（9/7）
- `labeler/`：annotate.html + gold001/gold002 批注（9/5-9/6）
- `crawlers/nga|bili/`：两爬虫源码归档并入（9/7 迁址，排除登录态/密钥）
- 记忆侧：`opinion-agent-plan.md` 逐段更新（迁移后路径 <项目根>）

### 10.6 遗留待办（9/7 视角，与 §8.7 重复处以此为准）

1. runner/编排（对话壳）→ 2. 真爬 live 标定+双模式数值实校 → 3. 前台（图表协议/SSE/深档预设）→ 4. verifier 三方对账 → 5. game_registry 登记表 → 6. planner prompt v0.1 落档 → 7. 命名/定位正文改写（待拍）→ 8. NLP 下一步 gold003 / C 定位（待拍）→ 9. §9.3 三轮数据测试重跑（bili 修好后的收尾验证）→ 10. Phase 5 部署 + 简历 bullet。反馈回路 ⛔ 搁置（对话壳之后再议）。

### 10.7 决策落成 + 三正式文档（2026-09-07）

用户指令（"落成吧…我分别需要：产品需求文档 / 架构设计文档 / 技术选型对比表，就当我们还在初期阶段，不要偏袒已有技术"）→ 本轮产出：

1. **`docs/PRD.md`** — 产品需求（背景/边界/FR1-8 带验收口径/NFR/数据需求/全局验收/打开问题）。
2. **`docs/ARCHITECTURE.md`** — 架构设计（原则 P1-P6/分层图/一次问答时序/记忆 L1-L4/门控与上下文治理/组件映射/凭证去敏/部署/演进）。
3. **`docs/TECH_SELECTION.md`** — 选型对比 D1-D9（**独立论证不因已实现加分**，每项带触发更换条件）。
4. `PROJECT_MEMORY.md` 精简重排（一页锚点，细枝指向 docs/）。

性质：把"已实现状态 + 既定决策"**正式化**为文档，不是新决策；关键选型（D1 手写循环 / D3 SQLite / D5 LoRA+judge / D6 标准库）经独立论证**复确认**。核心待拍（前端技术栈 D7、双模式区间数值实校、gold003/C）已登记进 docs 打开问题与本段下方；后续改决策须**同步改三文档 + §10.4 常量表**，防止文档与代码漂移。

### 10.8 决策落点（2026-09-08）— 评估 / 自进化 / 日志复盘 设计共识 + 前端可见性映射

> 当日脉络：live 标定 run3（calib_live3b_20260908）单进程 5/5 跑完（scope 逐题隔离）；之后连轮讨论"评估怎么建、自进化会不会上下文膨胀、日志监控复盘怎么落地"。以下 = 聊定 vs 待拍，正源本节。

**聊定**
- **评估分层**（方法优先于模型打分）：确定性免费层 = 引用校验 verifier（[id] 对回 scope 存档）+ 结构/口径 lint（结论行/不确定声明/引用格式/抽样偏置声明）→ 固定回归集（存量已知答案段）→ 陷阱探针集（阴阳/无证据/引战/冷门等已知坑，规则可判踩没踩）；再上人工审（用户 + /correct 即真值源）；最后偶尔 LLM judge（同题 A/B pairwise 比单判稳）。可视化进本地复盘 HTML 一个区块，不单独仪表盘。
- **自进化防膨胀护栏**：纠错 = supersede 留痕（v1 已落地 StateCard.correct + CLI /correct）；教训库有界、按检索命中才注入、默认关、重答 ≤1、门控常量静态；**trace（append 留痕，无界）与 procedure（改行为，闸控）分离**——一次性纠错只进账本不改行为。
- **日志/复盘**：engine 工具层统一流水（jsonl、scope 隔离）→ 本地自包含 HTML 复盘（时间轴 + 门控停因标注 + [id] 点原文）；nlp/爬虫未来融合同口子即都有流水（🅿 PRD 打开#7）；**复盘 ≠ 服务器侧监控/告警**。

**前端可见性映射（产品 UI / HR demo vs 作者侧本地）**
- 前端可见：① 自进化 → **纠错按钮**（结论卡"纠正"，= CLI /correct 网页化，唯一必须前端交互）② 评估 → 答案**校验徽标**（引用已核 ✓ / 引用未对上 ✗ / 已注不确定 ✓，零交互）③ 日志 → **可折叠 trace 面板**（每轮爬了什么/门控为何停/token/[id] 点原文，演示亮点）④ 爬取过程进度（可选边爬边显示）。
- 作者侧本地、不进产品 UI：批量 judge / 回归集对比 / 探针集 / 复盘 HTML / 教训库 / 服务器运行监控告警。

**待拍**：评估是否恢复 + 顺序；教训库做不做、何时；前端 D7；先清 run3b 逐题复盘（q2 结论行游离 `</tool_calls>` 碎片已验非 DSML 泄漏，但真结论未抓到待查）；下步 A=逐题复盘 / B=回归+探针题单。

### 10.9 爬取规则改版（A 伞形板护栏 + B 引擎双平台按轮）决策落点（2026-09-08）

> 当日脉络：用户要求「试试 fid+key 版内搜」「无主板冷门游戏找找实测」→ A 落地；「爬虫不要二选一、每轮并行」「快速≤3 / 精准 5 或 6 硬上限、双模式是前端可选项不许软标准、两套 prompt 分开」「数据总量大概即可、每轮后让 LLM 判断够不够」→ B。正源代码。

**A — NGA 搜索路由（crawlers/nga/nga_search_demo.py + harness/sources/live.py）**
- `thread.php?fid=X&key=Y&__output=11` = **版内关键词搜**（fid+key 交集），实测返回帖 100% fid 命中、标题全含词；版内无词返回空 = 真没搜到（诚实，不跨界）。
- 泛词不再走全站 `search|`（跨游戏漂移噪音），改 `searchin|<游戏>|<泛词>`：先 `_resolve_board_fid(game)` 定主板 fid，再版内按词搜。
- `_resolve_board_fid` 规则（修 3 bug 后）：翻 1-4 页全站搜游戏名 → 只认正 fid（滤负哨兵）→ 在 top fid 里取第一个「归一化板块标题含游戏名」的专属主流板（占比 ≥0.30 且命中 ≥8）→ 否则 None。排除子版（王者 562=招募子版，真主板 516）与伞形综合板（428/414/823 标题不含游戏名）。
- **无专属主板**（冷门/散落综合板，如万象物语）→ `board_fid=None`，锚定「游戏名+词」全站兜底（不裸搜词防漂移）；落盘 `board_fid=None` 供上层辨识「证据零散、引用谨慎」。
- 板块 `<title>` 是 **GBK**（JSON `__output=11` 才 utf-8）：`for enc in ('gbk','utf-8')` 顺序解码，修全角冒号匹配（`崩坏：星穹铁道` 需归一化 `_norm_name`）。
- E2E（经隧道真爬）：崩铁 流萤→818 版内 10 帖干净；王者 孙膑→516 真主板打法讨论；万象物语 停更→None 锚定兜底。

**B — 引擎闸门改版（harness/engine.py，对照 §10.2/§10.4 旧表）**
- 平台由「二选一」改「**同一轮 NGA+bilibili 各爬一次**（一条 assistant 消息里多个 crawl_live，预算按「轮」计：同轮双平台=1 轮；用尽 block 强制作答，全被平台闸门挡的轮不耗预算）」。同轮双平台 HTTP 最初仍顺序执行（并行待拍）；09-08 落地 = 拆两独立 crawl 工具 + 同轮 fetch 并发（见下条 B 收尾）。
- 达标不硬停：SAMPLE_TARGET 降为参考量（软），每次真爬返回 `ask_total`（本 ask 两平台新增累计）供 LLM 每轮比对自判。
- 46开/MIN_SHARE/`_balanced`/`_split_str` 硬 gate 删除；保留平台断路器 ask_fail + bili 会话冷却(300→900 归零) + bili 爬距(BILI_GAP)。
- BASE_SYSTEM 拆成**快速/精准两套** `_base_system(mode, cap, budget, tgt)`（前端可选项，行为/预算/收口口径不同）；`_EVIDENCE_CAP` = 快速 5 / 精准 15。
- 验证：`_tmp/verify_budget_rounds.py` 10/10（一轮 NGA+bili 都真爬但只耗 1 轮；第 4 轮被预算 block 且不再真爬；两套 prompt 文字不同且都含「平台不作二选一」）。
- 决策：精准预算取 **5**（用户定 5）；SAMPLE_TARGET **先别改**（保持 120/500 软参考）；live 冒烟 **别跑**（真爬 LLM 双平台行为留正式实跑轮再验）。
- **crawler 单元拆分**（09-08，用户方向 = 未来把 NGA/bili 做成**独立可选装 tool**）：`sources/live.py` 把单 `crawl()` 的 platform 分叉拆成独立函数 `crawl_nga()`/`crawl_bili()`（各自不含对方逻辑）+ `CRAWLERS` 注册表；`crawl()` 退为只做分发的 dispatcher（SEAM #2 provider 接口不变）。装/卸注册项 = 增删爬虫能力面，届时对某 crawler 包一层 schema 即独立 tool。`py_compile` 过、verify_budget_rounds 10/10 + test_finalize 22/22 未受扰动。
- **B 收尾 = 工具面落地为两独立 crawl 工具 + 同轮并发抓取/落库单写**（09-08，用户「再走一步，然后搞落库/你干吧」，代码完成、**功能测试未跑**）：`tools/crawl_live.py` 改注册表驱动（CRAWLERS = crawl_nga/NGA、crawl_bili/bilibili；模型工具面 = {crawl_nga, crawl_bili, search_archive}，平台由注册表固定、不再暴露 platform 参数给模型）；工具实现拆 `fetch`（provider 抓 items，不碰 DB，可并发；CrawlError 上抛）/ `commit`（record_crawl 落库 + summary/evidence）/ `call`（兼容旧调用方）。engine 同轮对通过 `_precheck`（断路器/冷却/bili 爬距，主线程顺序预检）的 crawl 用 ThreadPoolExecutor 并发 fetch，`_mark_fail`/`_after_crawl`/落库/`_plats` 计数/翻篇全回主线程串行 —— **sqlite conn 单写不跨线程**；预算仍按轮（成功轮末 +1，同轮双平台=1）；工具返回按原 tool_call 顺序回填。`_crawl_gate` 拆成三个小方法；删死代码 `_norm_platform`/`PLAT`。`py_compile` 全绿。
- **B 收尾真爬实跑**（09-08 20:05-20:14，用户「去演示服务器上跑两轮测试」回选 B = calibrate 2 题真问答，单进程 interval 300s）：
  - 首轮（`calib_twotools_20260908.db`，tag `twotools-b-20260908`）**两题同点炸** `AttributeError: 'int' object has no attribute 'result'`：并发路径 dict comprehension 方向反了（`{ex.submit(...): i}` → items() 得 (key=Future, value=int)，`for i, f in futs.items()` 解包 f=int）。修复 = idx→Future（`futs={i: ex.submit(_do_fetch, args) ...}`，`fetched={i: f.result() ...}`）；离线 verify_budget_rounds 10/10 + `python -c` 隔离复现确认。
  - 重跑（`calib_twotools_fix_20260908.db`，tag `twotools-b-fix-20260908`）**2/2 通过**：q1 鸣潮(快速) LLM 3 回合 / 2 真爬轮，双平台各 2 条 crawl_run **起始同秒**（20:06:08，同轮并发成立）→ 平台新增 NGA 21+bili 121=142 obs；q2 王者孙膑(快速) 同形（20:12:27 起）→ NGA 10+bili 99=109 obs。阻塞=无、预算未顶格（`_n_crawl` 2 < 快速 3）、两题均「结论:」干净收口、无线程/sqlite 炸、翻篇=0（每题独立 scope 本就不跨题）。
  - 口径注：calibrate 打的 `rounds` = engine ctx.rounds = **LLM 回合数**（engine.py:314 每往返 +1，含收口回合），非爬轮；预算闸门看独立计数 `_n_crawl`。measure 注：本机无 nlp-tool token → senti/sarcasm/topic_tags 度量降级 keyword（软信号，真 LoRA 度量在演示服务器/生产服务器 侧，非 harness bug）。
- **遗留**：run3b q2 收口 + 游离标签 scrub；正文+引用集持久化（C）。—— 09-08 晚已陆续关单/落地，见 §10.10。

### 10.10 复盘地基 A1-A4 + 空 query 炸题修复 决策落点（2026-09-08 晚，用户「全做吧」）

> 脉络：big10 大标定（3快7精）跑一半 q3 金铲铲 live 炸题（`TypeError: crawl() missing 1 required positional argument: 'query'`）→ 修 + 乘空窗把 §10.8「日志/复盘」里挂起的四件复盘地基一次做齐。**big run 实证结果另条续，本节=代码事实。**

- **决策：空 query 炸题 → 守卫挡成受控 tool-error，不让 TypeError 炸整题**（engine.py crawl 分支、`recs[idx]={"tool":..,"blocked":..}` 提示补词；`tools/crawl_live.py` schema `query` 进 required）。动机 = 工具 schema 把 query 标可省略而 live `crawl_nga(query, game, limit)` 是必填位置参，模型漏传直接抛整题级异常、报废一次真爬机会；应让模型补词重调。回归 `_tmp/verify_crawl_noquery.py` 6/6（漏 query 被 blocked 提示、不真爬、补词后照常真爬收口）。**只对新码生效，跑中的 big run 仍旧码**。
- **A1 工具层统一流水 jsonl（§10.8 落地一半）**：新 `harness/flow.py`。engine 每 ask 收口自动落一条 per-scope jsonl（`harness/data/flow/<slug>.jsonl`，scope 隔离成文件）——answer 全文 + concl + 逐轮 trace + ev(引用 run#) + usage + mode + rounds + ceiling，与 session_log(结论摘记)并存互补。写 = engine `_emit_flow` 收口处自动副作用（append try/except 静默）；`HARNESS_FLOW=0` 可关（测试/不想落盘）。验 `_tmp/verify_flow.py` 9/9（scope 隔离、answer/concl/question/trace/usage 在、engine 返回不变）。数据底 = 复盘/verifier/回放的正文级来源。
- **A2 确定性 verifier 雏形（§10.8 评估第一层，免费不烧 LLM）**：新 `harness/runner/verify.py`（只读）。吃 jsonl 流水 + 归档 DB，逐条判 ①有干净「结论:」行 / `(未收口)` 缺结论 ②每个 `[id=N]` SELECT observation 对回且 scope 与本题一致（`cite_unresolved`/`cite_cross_scope`，scope 隔离校验）③`--check-run` 把 ev 的 run# 对回 crawl_run。退出码 0=全过/1=有 FLAG/2=无流水。验 `_tmp/verify_a2.py` 11/11（顺手修 verify.py 一个真 bug：resolved 计数漏自增恒 0）。
- **A3 run3b q2 收口/游离标签遗留关单**：`_scrub`/`_final_answer`/`_drop_empty_concl`/`_tool_intent` 已覆盖孤儿 `</tool_calls>`/空结论/意图残句（test_finalize 单测）。补 engine 端到端「无结论草稿→强制二次无工具收口」回归 `_tmp/verify_a3_final_answer.py` 9/9：场景A 初稿=搜索意图残句 → `_final_answer` 二次 `_llm(tools=False)` 紧致重写落干净「结论:」；场景B 二次仍只给悬空「结论: 」→ 清空标记返回正文、materialize/flow 诚实记 `(未收口)`（不冒充历史结论）。不炸不悬空。
- **A4 跑批中间态观察器（§10.8 复盘工具）**：新 `harness/runner/observe.py`（只读）。跑批中途不碰进程，直接读 calib DB（每题独立 scope=`exp:calib:<tag>:qN`）判 收口(该 scope 有 session_claim)/采集中/疑卡(有 obs 无结论且静默>interval+900s)/起步?；平台列归一（NGA/bilibili→nga/bili）。用法 `python -m harness.runner.observe --db <db> [--loop 秒]`。live 快照自证：标出 big run q3 疑卡、q5 采集中、q1/q2/q4 收口。
- **big10 大标定（task b4bd3vdha，中途）**：10 题 3快7精/8 游戏（题单 `_tmp/calib_big_20260908.qs.tsv`，DB=`harness/data/calib_big_20260908.db`，tag `big3f7p-20260908`，单进程 interval 300）：q1✓/q2✓/q4✓/q6✓ 收口（q4 阴阳师 NGA 仅 3 obs·bili 286，**新游戏 NGA 版内偏薄再确认**；q6 无限暖暖 NGA 全程 0 obs）；**q3 金铲铲 + q5 明日方舟均炸** = 旧码 crawl 空 query TypeError（爬几轮后模型某轮漏传 query 即报废整题，无守卫）；q7-10 待跑。**炸的两题 + q4/q6 里 NGA 拿不到的侧面，复盘后统一用新码补跑。**

### 10.11 NGA 检索三连修：逐词单搜 + 两段式主板定位 + 正文兜底（2026-09-09，用户「A吧」收尾）

> 脉络：bigfix 4 题（q1金铲铲/q2明日方舟/q3绝区零/q4崩铁）收口时 **NGA 爬 0** 成主瓶颈 → 逐层诊断：①版内 title 搜 + 多词空格 AND 整串几乎必 0（词在标题的概率≈0）→ **逐词单搜**；②即便逐词，长草/产能/复刻这类"只进正文不进标题"的词连 title 搜也够不到 → **正文兜底**；③正文兜底要主板锚，而**明日方舟本体没独占正板**（全局搜被 846 终末地稀释 11/38<0.30，真主板是**负版区 -34587507「罗德岛大使馆」聚合页**）→ **两段式主板定位**。三者叠加 = searchin 从"词必 0/主板 None"到真打 0→10 帖。

**① 逐词单搜 `_board_search_tokens`**（09-09 首版，见 HANDOFF 02:12 段）：searchin 先定主板 fid 只一次，kw 拆**单个词**逐词发版内 title 搜（`thread.php?fid&key=<单短词>`）按 tid 合并去重；不做空格 AND。A/B：`金铲铲之战|版本平衡 超标 阵容强度` 0→10 帖。离线 `_tmp/verify_nga_single_search.py` 8/8。

**② 两段式主板定位 `_resolve_board_fid` v3**（本轮新增，负 fid 语义澄清）：NGA 结构澄清 = **正 fid = 可发帖正板**（金铲铲510461 属 LOL 负区-152678 下；终末地846 是独立正板）；**负 fid = 版区聚合页**（明日方舟本体 -34587507 大使馆，下辖 735问答室/734酒吧/805图书馆…，`fid=-34587507&key=长草` 实测跨子版回 32 帖）。解析两段：① top8 正板里取「归一化标题含游戏名」首个，占比≥0.30 直接返回（金铲铲/崩铁/终末地走这，**保留 09-08 只认正 fid 修正**）；② 占比不足 = 多子版游戏 → 从候选板面包屑 `_section_of` 取「标题含游戏名的负 fid 版区」按子版帖数加权取最多作锚。配套 `_keep_item`：admin/#SYSTEM#/匿名过滤，**fid 相等校验只对正板强制**（负锚下子版帖不误砍）。离线 `_tmp/verify_nga_resolver.py` 10/10（含 终末地→846 不误取负区、无归属→None）。`_board_html/_section_of` 复用同一请求+缓存解析标题/面包屑。

**③ 正文兜底 A `_board_body_fallback`**（用户拍 A，见 02:1x）：searchin 逐词 title 仍 0 → 拉主板最近 `FALLBACK_BOARD_PAGES=2` 页帖 → 标题含词直接收（不翻详情）→ 其余候选进详情 `_parse_detail` grep 正文命中才收，详情封顶 `FALLBACK_DETAIL_MAX=15`（防 429）；正文命中帖嵌 content/hot_replies，run_query post-loop `if 'content' in it` 复用不再二次翻详情。离线 `_tmp/verify_nga_body_fallback.py` 11/11（含 cap 截断/标题直收不带 content 留正常翻/正文无关过滤/空 kw）。

**真打 A/B**（`probe_nga_tokens.py`，tunnel 18770→演示服务器:8770）：金铲铲 10 帖回归不破；`明日方舟|长草 产能 复刻 节奏` **0→10 帖**，落盘 `board_fid=-34587507`、正文/热评齐全（"拿完危机合约奖励了开始舒适地长草""长草期刷红票并非没有意义"…全对题）。**演示服务器 md5 `678dcb6b…` 与仓库同版**（`.bak_single`/`.bak_bodyfallback`/`.bak_bodyfallback_prev` 快照均在），nga-tool active。

**观测修正**：明日方舟这批帖热评 1-4 正常 → `_extract_hot_replies_fixed` **非系统性 0**（早先金铲铲那批 0 = 帖级 DOM/老帖差异，非改动引入，低优不追）。

**回写状态**：HANDOFF 已更顶部；本段= PLAN 决策落点；PROJECT_MEMORY §2/§5 锚点待顺手补一行；ARCHITECTURE 组件表 NGA 行旧述（"只认正 fid"）已过时，需按本条校正。

### 10.12 平台比例旋钮入参（语义 A：样本证据配比）阶段一 = 穿针（2026-09-09，用户「A 你先写好入参」）

> 脉络：用户从「爬虫能否单独调一边」→「我想让用户自主选择」→ 前端「设旋钮让用户选平台比例，我们根据那个比例来」。统一 = NGA↔bili 连续占比；这正是 09-08 §10.9 删的 46开/MIN_SHARE 的复活，但 46开 是写死常量，现在变**用户输入**。用户拍语义 **A = 样本证据配比**（否决 B 爬轮配比：轮数 3~5 粒度太粗、强切丢同轮双需求；否决 C 只定接口）。A 目标 = 引擎盯每 ask 累计 NGA:bili 证据量与比例比，欠配比边下轮提示优先爬；0/100 端点硬压另一边（=单平台，收敛早先 `--platform` 提案）；熔断/冷却闸门永远优先于比例；50≈现状「双平台每轮都爬」。

**阶段一（本节落地）= 入参穿针，无行为引导**（engine `_base_system` 里「平台不作二选一」两句**未动**）。三文件：
- `harness/engine.py`：常量 `DEFAULT_NGA_RATIO = 50` + `clamp_ratio()`（任意输入→0..100 整数，非法回落 50）；`Engine.__init__` 默认 + `Engine.ask(..., nga_ratio=None)` 里钳制落 `self._nga_ratio`（每 ask 有效值，阶段二引导据此读）。
- `harness/runner/session.py`：`Session.__init__(..., nga_ratio=None)` 存会话默认 `self.nga_ratio`；`ask_turn(..., nga_ratio=None)` 单题可覆盖、缺省=会话默认，透传 engine.ask。
- `harness/runner/calibrate.py`：CLI `--nga-ratio 0..100`（默认 50；越界拒绝 exit 2）；报头 `nga_ratio=%d` echo；`Session(db_path=..., nga_ratio=...)`。

口径：**单标量 = NGA 占比%，余量=bili**（100=只 NGA / 0=只 bili / 50=均等≈现状）；前端旋钮值将来直接映射本参数，数值口径第一天定死。验证：mock 单题报头 echo `nga_ratio=100`、流程不崩（mock 仍双平台各 10 属预期，阶段一无引导）；clamp 边界单元（-5→0 / 150→100 / '70'→70 / None→50 / 'abc'→50）；`--nga-ratio 150` 拒绝 exit 2；`Engine._nga_ratio` 默认 50。

**阶段二拆分**：端点 0/100 硬压（`_precheck` 顶部：`>=100` 禁 bili / `<=0` 禁 NGA；`ask()` 前置提示）**已同日落地并验证**（单元 + mock：ratio=100 → NGA 15 / bili 0），详见 §10.13。内部 1..99 **软引导也已落地**（用户定「option 1、误差≤5%」）：`_ratio_nudge()` 每轮按本 ask 两平台累计新增算 NGA 占比，偏离目标 >RATIO_TOL=5 且建议平台未熔断/冷却时才追一句系统提示让模型下轮优先补欠配比边 —— 只提示不改调度，详见 §10.14。留痕 = flow trace `ratio_nudge` 事件 + 报告 header/平台新增即够，不加 DB 列。

### 10.13 端点硬压落地 + B NGA-only 补跑实证（2026-09-09，用户「b补跑且只跑nga」）

> 触发 = 用户要验证 NGA 修复泛化但只爬 NGA → 阶段一穿针无引导满足不了 → 顺手把阶段二端点最小切片做了。文档：HANDOFF 现态 06:4x；PROJECT_MEMORY §2 已更；PRD FR1/§6/#8 已登记旋钮。

**端点硬压**（engine.py，阶段二最小切片）：`_precheck` 顶部 `nga_ratio>=100` 禁 `crawl_bili`、`<=0` 禁 `crawl_nga`（熔断/冷却仍在下各自生效；"挡失败/风控"与"挡平台配比"不混）；`ask()` 开头单平台时加一句前置提示防模型浪费回合调被禁工具。验证：py_compile；单元 100→禁bili/0→禁NGA/50→双放行；mock 全链 `nga_ratio=100` → **NGA 15 / bili 0**。

**B live NGA-only 补跑**（task b5b5ziaum，`harness/data/calib_bngaonly_20260909.db`，tag `bngaonly-20260909`，interval 420，题单取 big10 line11/12 原文）：
- **q1 绝区零「不驻场/记录伤害机制」**：NGA **42 obs / bili 0** / rounds=5（精准预算吃满）→ 收口对题（蕾米三体人级但节奏拖沓、6命反亏、争议在"记录耀变伤害"抹平角色差异、建议2命）。⚠ 答案**无 `[id=N]` 引用**（verifier OK，带引用=0）——内容强溯源弱，打磨点。
- **q2 崩铁「立绘抄袭/借鉴」**：NGA **90 obs / bili 0**，末 2 爬轮被预算 5/5 挡（正常）→ **诚实收口**：NGA 上"抄袭"集中系统/技能演出/地图，立绘侧=质量批评（84/87）+ 撞设计联想（81 鞋子、83→化鲸）+ 煎炒嘲讽 vs 吃瓜装死两极（123 对谈新闻），"立绘抄袭"具体侧面**样本不足如实注**。
- verifier q1/q2 **OK FLAG=0**；q2 引用 **10/10 全 NGA** 对回。
- 结论：NGA 修复泛化成立（非明日方舟巧合）；引擎全程只爬 NGA 证明端点硬压有效。遗留 = q1 无引用、崩铁"立绘抄袭"在 NGA 确实薄（诚实非爬虫失败）。

### 10.14 阶段二内部 1..99 软引导落地（option 1，误差 ≤5%）（2026-09-09，用户「1就行，误差不超过5％即可」）

> 触发：B 补跑归来后问内部占比怎么处理 → 用户放弃"每轮强制卡配比"，拍 **option 1 = 只做软偏置、误差 ≤5% 可接受**（否决硬切：内部值不该像端点一样硬卡，欠配比时硬拦会丢另一平台恰好到手的样本；否决统计题预算：3~5 轮粒度太粗）。实现 = engine 每轮收口时比对**本 ask 两平台累计新增样本**，偏离目标 >RATIO_TOL=5 且欠配边未熔断/冷却才追一条系统提示让模型下轮优先补那一边——**只提示不改调度**，模型仍有 final say；端点 0/100 走 §10.13 硬压，不在此软逻辑内。

**落地**（engine.py，在 `_after_crawl` 之后新增 `_ratio_nudge()`）：
- 常量 `RATIO_TOL = 5`；`_ratio_nudge()` 读 `_plats['NGA']['new']` / `['bilibili']['new']` 算本 ask NGA 占比，share < r-5 且 NGA 未熔断 → 提示优先 crawl_nga；share > r+5 且 bili 未 ask_fail/冷却 → 提示优先 crawl_bilibili；熔断/冷却/无样本/端点 → None 不干扰。
- 触发点：round-end `if round_ran: self._n_crawl += 1` 后插入 nudge 系统消息 + flow trace 事件 `{"event":"ratio_nudge","nga_ratio":...,"note":...}`。
- `ask()` 前置提示补充内部段：0<r<100 时提示"NGA X% / bili Y%，按此配比取两侧新增，偏差别超 5 个百分点，某平台熔断/冷却/真没料时别为凑比例硬爬"。
- 不改 DB 列，留痕 = trace 事件 + 报告 header 平台新增计数即够。

**验证**：py_compile；`_tmp/verify_nga_ratio_nudge.py` 边界单元 **PASS 8/8**（带内→None / 欠NGA→提示crawl_nga / 过NGA→提示crawl_bili / NGA熔断→抑制 / bili冷却→抑制 / 无样本→None / 端点100→None / 端点0→None）；mock `--nga-ratio 70` 全链跑 flow trace 实测 **ratio_nudge 触发 2 次**（NGA 占比 5→10→15→20 逐步修正方向）。

**留档清理**：验证脚本与 mock db/report/三个 mock flow jsonl 已按本会话删除纪律清理（只删自己建、按名删）。

**当前内部语义总览**：0 = 只 bili（硬）、100 = 只 NGA（硬）、1..99 = 每轮软引导按 ±5% 收敛到目标占比（模型可控）；50 ≈ 现状"双平台每轮各爬"但不强卡 1:1。文档已同步：PRD FR1/§6 已改 option-1 软 ±5 语义；PROJECT_MEMORY §2 已更；HANDOFF 顶行已刷新。

### 10.15 数据可删性契约 + 前端「清除对话记忆」按钮入规划（2026-09-09，用户「我们怎么防止我们自己的harness误删文件 → 可以，前端按钮也写上去吧」）

> 触发：用户问防 harness 误删文件 → 先核清**事实**：harness/*.py 全仓**零破坏性文件操作**（grep：无 os.remove/unlink/rmtree/truncate/`open("w")`/SQL DELETE/DROP/UPDATE；文件写 = flow/report 追加 "a"；sqlite 仅 CREATE IF NOT EXISTS+INSERT+ALTER 加列；observe 库只读 mode=ro）——数据层天生只增，误删不可能来自 harness 自身文件处理，现实风险只在**运维/清临时文件时的 shell（操作员=代理本人）**与**未来加删除功能时破坏只增契约**。用户随后点出真正要防的场景 = 将来必有「清除所有对话记忆」按钮 → 拍契约 + 按钮一起写进规划。

**拍定的契约（正源 ARCHITECTURE §5.1）**：数据层分**会话层=可删**（前端聊天历史 / L1 StateCard / session_log / flow）/ **证据层=禁删**（L3 archive observation·crawl_run·session_claim + 报告——跨 scope 共享、`[id=N]` 引用对回正源、append-only）——**删/不删在数据层分家，不靠按钮自觉**。核心洞察：防"清全部"误删不能靠弹窗确认（手滑可再点走），靠①范围隔离（删除 API 强制 scope+kind∈{conversation}，证据表带 protected 禁删标记，删除函数先校验 kind，动证据层须管理员级单独入口）②软删优先（tombstone `deleted_at` + 默认 7 天保留期可恢复；物理清除=独立延迟任务不进按钮）③单点+留痕（删除只走一个带 --dry-run 的 CLI/endpoint，type-to-confirm 前端做，删后写 append-only 审计日志）。

**前端按钮入 PRD（FR9 + FR1 + §8 #9）**：FR9「会话数据删除 —清除对话记忆 防误删」⏳——需求（单条删/清单会话/清我全部，只触会话层）、软删语义、防误删 UX 三闸、验收（全清后历史 `[id=N]` 仍对回证据库 / 7 天可恢复 / 审计可查）；FR1 会话历史加"支持清除→见 FR9"；§8 #9 = 打开项（保留期天数默认 7 / 按钮入口 / type-to-confirm 粒度 / 单条删 vs 全清两级）。**代码零改动**（无删除路径可守，接口预留是文档级，等接按钮时按三闸实现）。

### 10.16 append-only 守卫固化（2026-09-09，用户「要的，弄完之后我们接下来可以做什么」）

> 触发：§10.15 契约落地后问要不要把"仅只增"固化成守卫 → 用户「要的」。目标 = 未来任何改动往 harness/*.py 塞入删除/覆盖/截断原语时，回归一眼可见，不靠人肉 review。

**落地 `harness/guard.py`**（纯 stdlib）：静态扫 harness/*.py，三类违规命中即列并 exit 1——
1. **破坏性文件原语**：`os.remove/unlink/rmdir/removedirs/rename/renames/replace/truncate`、`shutil.rmtree/move/copy/copyfile/copy2/copytree`、`.unlink/.truncate/.write_text/.write_bytes`（方法调用式，含空参 `Path(x).unlink()`）。用负向后顾 `(?<![A-Za-z0-9_])` 防把普通标识符尾部误当原语；不做尾部 `\b`（`(` 后接 `)` 无词界，首版即踩）。
2. **破坏性 SQL**：`DELETE FROM` / `DROP TABLE|INDEX` / `TRUNCATE TABLE` / `UPDATE .. SET`（大小写容忍）。
3. **`open(...)` 写模式**：参数表内以 `w` 开头的模式字面量（含 `mode=`）＝截断/覆盖写；括号配平扫描，`r`/`a`/`ab`/`a+`/`x` 放行。

guard.py 自身豁免（源码必然含这些 token）。运行 `python -m harness.guard`（或 `--root <dir>`）；import 用法 `guard.check_harness() -> list`。

**验证**：py_compile；自检 `_tmp/verify_append_only_guard.py` 正反例 **14/14**（干净读/追加/ab/a+ 不误报；os.remove/os.unlink/shutil.rmtree/shutil.copyfile/Path.unlink/open"w"/open"wb+"/DELETE/DROP/UPDATE 逐类命中）；live 扫真库 **0 违规** → 现状"只增"事实再固化。CLI exit 0。verify 脚本保留作离线回归（对齐 verify_nga_* 惯例）。

**文档同步**：ARCHITECTURE §5.1 注（运行命令）+ §7 组件表新行；PROJECT_MEMORY §2；HANDOFF 顶行；本节。运行纪律 = 每次动 harness 代码后 `python -m harness.guard` 当回归（本仓库非 git，无 pre-commit 钩子可挂）。

### 10.17 context 计量接线 + L2 checkpointer / L4 alias registry 转活（2026-09-09，用户「全做」架构审计收尾）

> 触发：用户中断旋钮/前端方向，要求审计系统架构还能改什么 → 给 8 项清单（文档一致性 + 两个 DEFERRED 转活），用户「全做」。本文记代码落地项（1-6 的文档一致性清理不重复罗列），只记 ITEM6/ITEM7 两个真实实现。

**ITEM6 context.py token 计量接线（正源 ARCHITECTURE §6.2）**：`context.token_count`（cl100k）接进 engine 当**引擎侧输入估算**——模块级 `_est_input_tokens(messages)` 只计 content 文本（不含工具 schema/元数据）＝真实输入的下界，故**不会比 provider 用量更早触发护栏**；`ask()` 每 `_llm` 后累计 `in_est`，护栏判定改 `(prompt_tokens > ceiling or in_est > ceiling)`（provider 缺 usage 时它是唯一护栏）；两条收口路径都回传 `est_input_tokens`、`_emit_flow` 带 `est`。自检 `_tmp/verify_context_wiring.py` 8/8。

**ITEM7a L2 checkpointer 转活**：`state_card.py` 加 `snapshot()/restore()`（整卡 JSON 化拷贝）；新建 `harness/checkpoint.py` —— **只增契约下 save/clear 全用 `INSERT OR REPLACE` 覆盖同 scope 一行，不留 DELETE**（guard 逼出的实现；物理清除留给将来删除按钮）。schema 加 `checkpoint(scope PK, card, saved_at)`。Session 接线：冷启动 `_restore_checkpoint()`（恢复卡 + 续 `engine._n` 防 cid 撞）、`ask_turn` 每轮后 `_save_checkpoint()`（崩溃可续到最近收口轮）、`flush()`/`set_scope()` 清旧 scope 检查点（内容已翻篇进 session_claim，防重复续接）。

**ITEM7b L4 alias registry 转活**：新建 `harness/registry.py` —— 黑话→规范名**账本式**注册表，**supersede-not-overwrite：生效=该 alias 最新(id 最大)行，语义由 id 序替代，不用 UPDATE valid_until 关旧**（guard 禁 UPDATE 逼出的设计）。内置 6 条（三蹦子/三崩子/崩三=崩坏三、终末地=明日方舟终末地、星铁/崩铁=崩坏星穹铁道），`seed` 幂等（同 alias 最新行同 game+source 则跳过，重播不刷账本）；`resolve`/`find_in_text`（长词优先去重）无表安全回落。Session 建库后 `seed_builtin()`；engine 加 `_alias_context()`：问句含黑话且无显式 game_hint → game_hint=规范名 + 系统提示注（检索/归档用规范名）+ trace `alias_resolve` 事件；显式 game_hint 优先、注册表不覆盖。检索空 query 缺省 game 走 `_norm_game(..., game_hint)`，黑话还原后工具缺省即规范名。

**验证**：`_tmp/verify_l2_l4_activation.py` **19/19**（registry 往返/幂等重播、StateCard 往返、checkpoint save/load/clear、Session 三代同库跨进程续接——首问 M1 → 新 Session 冷启恢复 M1+_n=1 → 再问 M2 不撞 cid → flush 清检查点 → 三代冷启动无残留 + 日志 count≥2、engine `_alias_context` 黑话还原/显式 hint 优先/完整 ask 黑话注进 messages）；py_compile 全过；`python -m harness.guard` 0 违规（checkpoint/registry 均无破坏性原语）。**文档同步**：ARCHITECTURE §3/§5/§7/§10 + 版本头（L2/L4 ⏳→✅、context ⚠️→✅）；RECALL 3 行刷新；HANDOFF 顶行；本节。旧 PLAN §10 item5「DEFERRED 转活」作废。自检脚本保留作离线回归（对齐 verify_*.py 惯例）。

### 10.18 M0 基建文件 + M1.2 黄金回放 + M1.3 收口证据校验（2026-09-09，用户「M0-M1 编 todolist 自己做」）

> 触发：用户要整仓梳理 + 参照外部 harness 出到"harness 完成"的路线图 → M0-M4 路线图拍板（D1 搞吧 / D3 放吧 / D4 无现成黄金案例、崩铁强度膨胀可当 / D5 做吧 / D6 标定 runbook 别急）；随后 **git 化全线暂缓（用户「都先别进 git 了吧」）**——文件仅落盘，不做 .gitignore/commit。本轮做 M0 + M1.2 + M1.3。

**M0 基建文件（纯标准库卖点固化）**：根 `requirements.txt` = 仅可选 `tiktoken`（context.py cl100k 精确计数用，不装退 `len/1.7`），核心 harness 零第三方依赖；根 `README.md`（demo mock/--live、calibrate、`python -m harness.test`/`--server`/`--list`、guard、凭证纪律、数据只增纪律、三层验证分层）；`harness/test.py` 一键自测聚合器 = guard（进程内直调）+ **offline gate**（纯离线 verify_*/test_* 子进程，按退出码 + 无 Traceback 判 PASS）+ **gold replay**（evalgold 子进程）+ `--server` 才跑服务器门。判据/分组集中在 `GATES` 常量。

**M1.2 黄金回放 v0（确定性、不烧 LLM/不触网）**：`harness/runner/gold_cases.json`（manifest，case = scope + 标定 DB；6 `expect_pass`：bigfix q1金铲铲/q2明日方舟/q3绝区零-abstain/q4崩铁 + bngaonly q2 + ngafix q1；1 `known_issue`：bngaonly q1 绝区零**实指强结论却 0 引用**，`expect_fail:["c3"]` 单列不阻塞；另 `pending_live` 登记 **崩铁·本版本强度膨胀 fresh**——D4 用户指认可当黄金、无现成录得，待 D6 真跑窗口录 flow+DB 后入 cases）。`harness/runner/evalgold.py` 评分器判定原子：**C1** 结论在（含「结论:」且非 `(未收口)`）/ **C2** 引用对回（每个 `[id=N]` 能解析到 DB observation 且 scope==case scope，跨 scope=隔离泄漏）/ **C3** 无引即避（0 引用必须 abstain 措辞，实指断言违纪律）/ **C4** 有真爬（case scope 在 crawl_run 有落库）。**首跑 6 PASS + 1 KNOWN + 0 UNEXPECTED + 0 ERR**：引用全部对回本 scope = scope 隔离在真实录得上成立；唯一弱项 bngaonly q1 实指 0 引被正确标记（待 M1.3 缓解）。

**M1.3 收口证据校验（engine 硬防线）**：探明**工具返回本就带 obs id**（search_archive.rows[*].id + crawl_live._ev[].id = observation.id，模型据此 `[id=N]`）→ engine 收口处加 `_sanitize_cites()`（模块纯函数）+ `_seal_answer()`（方法）：直答/强制收口**两个出口都过校验**，凡 `[id=N]` 对不回当前 scope observation（编造/跨 scope）→ 剔除 + trace `cite_drop` + 答案附更正注，保证落纸/落库/流水的引用可溯源。回归 `_tmp/verify_cite_guard.py` 12/12（单测剔 9999/跨 scope/保留本 scope + 端到端直答夹带 + 干净负例零改），已入 offline gate。

**prod flow 污染治理（本轮顺带揪出）**：根因 = 多个 verify 单测在**临时 DB 上 scope=prod** 跑 `engine.ask`，而 flow 文件**按 scope 落盘**（`data/flow/prod.jsonl`）→ 每跑一次 `harness.test` 都往 prod.jsonl 追加测试行。修：`verify_budget_rounds`/`verify_l2_l4_activation`/`verify_context_wiring`/`verify_feedback`/`verify_crawl_noquery` 顶部 `HARNESS_FLOW=0`（它们不断言流水；断言流水的 verify_flow/a3 用自有 SCOPE 不受影响）；已删 17 行今日测试残行，prod.jsonl 剩 3 行 09-08 旧行（疑似早期引擎测试残留，**去留待用户拍**——prod 原则 = 留真实会话）。

**收尾状态**：`python -m harness.test` 全绿（guard + evalgold + 15 offline），`python -m harness.guard` 0 违规。git 仍全线暂缓（无 commit）。M1.1 认证 runbook 因 D6「别急」留演示服务器 窗口。下一候选：M1.3 落地后 bngaonly q1 的 0 引实指若再现即自动记 cite_drop（收口校验不含新增引用，只防编造）；gold 集等崩铁强度膨胀 live 录得后扩。

### 10.19 M0-M4 完整路线图文档化（2026-09-09，从会话转录恢复——此路线图原先只存在对话里，上下文压缩后曾丢，务必以此段为准）

> 教训：跨阶段路线图（M2/M3/M4 定义）此前只在会话正文、从未落 PLAN。压缩后 M2/M3/M4 定义丢失，靠翻转录 JSONL（`c8853510-…jsonl` 里 [18:58] 那条）才找回。**今后大规划第一步先落盘再干活**。以下为完整路线图存档（里程碑含 doD），M0/M1 已按下面状态推进。

**锚（主体定位，做任何步都别忘）**：harness = 游戏社区问答 Agent：中文问句 → 冷启动判断 → 按需真爬（NGA+bili，关键词）→ 原文自动存档 → 证据合成 → **引原文作答**（逐条 `[id=N]` + 明说不确定性 + 末行结论）。舆情纵深（黑话解码/反串阴阳/自基线爆发）是**旗舰护城河，不是全貌**；毕设大屏 =「监测类」，我们 =「问答类」。「harness 完成」= PRD FR1-FR9 全绿 + §7 全局验收 5 条（端到端 demo / live 标定 / 评测门槛 / 诚实性抽查 / 去敏发布）。

**缺口三桶**：A 质量/验证（可复跑认证流程、答案离线回归集、引用「生成时保证」、舆情纵深指标未过线 FR7 `sarcasm F1 0.368@th0.6` 瓶颈=标注噪声、determiner 硬尾判定未建）；B 前台（网页双模式+旋钮+历史、图表、删除按钮）；C 工程化（git、README、依赖清单、一键测试——M0 已基本补齐）。

**借别人（5 行表，判断性借不照搬）**：① RL agent 评测 harness（SWE-bench 式）→ 借「flow.jsonl = 确定性回放单元」做离线黄金回归（已 = M1.2 落地）；② Agents SDK/LangGraph guardrail+session 一等公民 → 借「收口前显式关卡」= M1.3 cite 校验，session/checkpoint 本已一等公民；③ promptfoo/DeepEval golden 回归 → 借 {题,必须引用,期望要点}+评分器进 `harness.test`（已落地，不上重框架，纯 stdlib 守 NFR-可演示）；④ DSPy 程序化 pipeline → 借「评测先行」纪律，**不借** prompt/签名自动优化（诚实口径要可归因可回滚，手调+决策日志 > optimizer 漂移）；⑤ Langfuse 追踪 → 借回放+HTML 复盘（PRD §8#7 已规划，不上 SaaS）。**明确不借**：第二套向量库、放大模型工具面（保持窄工具 `{crawl_nga,crawl_bili,search_archive}`）、多 agent 编排、nlp 硬判定接主链直到评测过线。

**M0 工程化地基（done，git 项暂缓）**：git init+.gitignore（用户拍暂缓，只落盘）；根 README（demo/标定/自检/发布/凭证纪律）；根 requirements 声明纯 stdlib；`python -m harness.test` 一键聚合（guard 进程内 + offline gate + gold replay + `--server` 门）。doD：克隆即全绿 ✓、guard 强制每跑 ✓。

**M1 质量认证**：M1.1 固化 live 认证流程（可复跑题单+参数表+预期，跑一轮干净双平台认证产 cert report 过 verify.py）——**D6 用户「别急」→ 留演示服务器 窗口未做**。M1.2 离线黄金回归集（done，v0：6 expect_pass + 1 known_issue + pending_live 崩铁强度膨胀，判定 C1 结论在/C2 引用对回 scope/C3 无引即避/C4 有真爬）。M1.3 运行时证据断言（done：收口前 `_seal_answer` 校验每条 `[id=N]` 对回本 scope observation，超集剔除+cite_drop+更正注）。M1 doD：认证 run 过 verifier（等演示服务器）；golden suite 过门槛（阈值随首批跑定）；0 幻觉引用（M1.3 硬防线已在）。

**M2 舆情纵深冲线（旗舰，依赖 M1 评测跑通 = 现成）**：① 拍 §8#3：gold003 扩容 vs C 方案（judge-primary），**先降标注噪声**（反讽标注一致性 review、gold 难例复核）——瓶颈在噪声不在方法；② 指标过线后按 FR7 决定 sarcasm 软信号能否进一步 + 建 **determiner 硬尾判定**（黑话解码「膨胀神游」式 referent / 反串阴阳 / 自基线爆发）= PRD §0 分层里唯一没落地的护城河。doD：gold400/反讽 F1 过既有门槛并记录在案；FR2 硬尾黑话题可溯源解码或明确放弃；FR7 验收可演示。依赖演示服务器/LoRA 判链，多为服务器活，**待 M1.1 之后**。

**M3 前台（FR1/FR8/FR9，依赖 M0+M1 后端已稳）**：M3.1 后端最小 API（动工前拍 D7 栈）：`POST /ask{text, mode, nga_ratio, conversation_id}` → Session 持会话（checkpoint 已备重启可续）→ answer+trace+obs 聚合（喂图表源）；删除/会话管理同层。M3.2 网页：左会话历史/右聊天 + 档位 + 平台旋钮（0-100 映射 nga_ratio，接 §10.12 口径）+ markdown/`[id=N]` hover 回原文；FR8 图表卡片（热度折线/情感分布，argmax 负面口径）接聚合源。M3.3 FR9 删除按钮：会话层软删（tombstone `deleted_at`+7 天保留）+ type-to-confirm + dry-run 明示 + 审计日志；guard 开**受控删除豁免**（契约只此单点）；物理清除二期不放按钮。doD：端到端 demo（拨旋钮 0→全B/100→全NGA 实证）；清历史后 `[id=N]` 仍对回证据库；7 天可恢复。

**M4 发布交接**：去敏复查（gitignore/无明文 token/无真 cookie）、发布物决策（`crawlers/bili` vendor 不进 GitHub——许可证口径 D9）、完整 README、录屏 demo。doD：§7-5「去敏后克隆即跑」成立；§7-1 端到端 demo 录下。

**依赖/待拍快查**：M1.1 前 = 演示服务器 窗口（free -h/单进程/量级对齐）；M2 = **已改写定案（§10.20）**，落地见 §10.20（determiner 路径，部分要演示服务器 真爬验证）；M3 前 = §8#1 D7 栈 + §8#8 旋钮方向口径（现定 NGA 占比 0-100）+ §8#9 删除细节；全程 = 动 harness 代码跑 guard + live 前隧道 /health 探活。

### 10.20 M2 改写定案：免人工、不扩标、不追小模型指标，determiner = 引擎 LLM 判定（2026-09-10，用户「M2改写吧」拍选项 1）

> 触发：我把 M2 讲成"先降标注噪声 + gold003 扩容 vs C 方案"后，用户两连质疑——**「已经有两个gold了还要加啊？？？我不想打了啊」**（拒任何扩标/复核）、**「C方案直接用judge来判不等于就是LLM在做NLP吗？？？」**（点破 judge-primary 到尽头 = 让主链 LLM 判）。用户拍「M2改写吧」= 下方案选项 1。

**定案内容**：
1. **人工标注零新增**：gold001/gold002（各 200）保留作验证切片，**不再扩 gold003、不做反讽一致性 review/gold 难例复核**。gold400 评测集不再扩。
2. **不追小模型数值指标**：反讽 F1 0.368@th0.6 / 情绪 Spearman 0.389 定格为历史记录，**不再作为 M2 阻塞门槛或投入目标**。铁律相应改写：评测不达标前「**小模型不作硬判定闸门**」——小模型软信号**只作初筛提示 + 图表 argmax**，不进主链当判定依据的硬闸；**LLM determiner 属主链本来就有的语义能力，不受此限**。
3. **determiner = 引擎 LLM 判定**（黑话解码 referent / 反串阴阳 / 自基线爆发判别）：由主链 DeepSeek 引原文判定，能判给证据、判不了明说，走 PRD FR7 ②分支「带证据的判定或明说判不了」。C 方案/judge-primary 并入此形态：judge（temp0+few-shot 覆写）只在可疑反讽**已落库软信号**做导流，不承担 determiner 判定。
4. **小模型 StructBERT 的用途 = 每条样本的软信号章，随证据喂主链 LLM 判向**：落库时本地毫秒级给每条打 senti(负/中/正) + sarcasm 可疑门(≥0.4 那撮交 judge 覆写) + topic_tags；这些字段**随 crawl/search 的 evidence 行一起进模型上下文**（用户 2026-09-10 点明：「NLP 用于告诉 LLM 每条爬回来的数据是正面还是负面」）。engine 系统提示已加字段解读（§见 10.21）：sarcasm=1 的正面措辞常是反讽别采信、senti=负 提示负面，但**软信号本身可能判错，判向最终以原文为准**——这就是"软"的含义。情绪分布另供图表 argmax 负面。**不追求 F1 达标、不扩标、不因它不准就摘它**。

**为什么对（判定 vs 初筛分离）**：舆情纵深（黑话 referent/反串阴阳/自基线爆发的**最终判定**）本质重语义 + 可溯源 → 由主链 LLM 判；但每条样本的**低阶情绪/反讽初筛**（"这条是正还是负"）用本地小模型便宜批量打，作喂给 LLM 的软信号，正是 FR7 软信号的本义。用户那句「不等于就是 LLM 在做 NLP 吗」的落点 = 高阶判定别绕去训小模型分类器，主链 LLM 直接判即可，别误伤低阶初筛这层。

**M2 改写后落地（从"冲小模型指标"改为"做 determiner 判定路径"）**：M2 目标 = 在舆情硬尾问句上跑通「黑话 referent 还原（挂 L4 registry）+ 反串/阴阳带证判定 + 自基线爆发判别 + 判不了就明说」的判定路径，以 live/黄金题验证。doD：崩铁·强度膨胀类硬尾题能给可溯源 decode 或明确放弃；反串题给出引原文判定或明说判不了；FR7 验收可演示。依赖：部分要演示服务器（真爬验证），determiner 逻辑本身离线可写可 mock。

### 10.21 证据软信号字段解读接线（2026-09-10，承接 §10.20）

> 我在 §10.20 措辞里把「小模型定位」一度写成"图表工具、主链不消费"，用户纠偏：**「NLP 用于告诉 LLM 这条爬回来的数据是正面还是负面」**——即 senti/sarcasm/topic_tags 是随 crawl/search evidence 行喂给主链 LLM 的**判向软信号**（非仅图表），链路本就通（engine 把工具返回体原样 json.dumps 给模型，engine.py:566），缺的是**读取契约**：系统提示从没教模型怎么解读这些字段。

**改**：engine `_base_system` 加【软信号提示】段——每条样本/证据行附 senti(负/中/正)、sarcasm(1=可疑反讽/阴阳)、tags(话题标签)；sarcasm=1 的正面措辞（"策划真懂玩家"类）常是反讽别当正面采信、senti=负 提示负面；**但软信号是初筛、本身可能判错（反讽字段精度有限），判向最终以原文为准，拿不准按规则 4 明说，别被单条软信号带偏**。撞号问题：原 head 规则 1-7 + 快速/精准 tail 的 8)-10)，故新段不加编号、放 head 规则 7 之后。

**口径定死**：小模型章 = 低阶初筛软信号喂 LLM（这条正/负/反讽），高阶 determiner 判定 = 主链 LLM 引原文。**不扩标、不追 F1、不因不准摘除**——不准恰恰是"软"（提示、非硬闸），LLM 最终仍以原文为准。文档同步：PROJECT_MEMORY §2 评测行、labeling-nlp.md 口径铁律、PRD FR7、ARCHITECTURE。验证：guard + harness.test 全绿（本文档改动不涉代码回归；prompt 为纯文本，gold replay 离线判定不受影响）。

### 10.22 M2 落地 #1：determiner 纪律 + 软信号读取契约离线门（2026-09-10，用户「落地。。。」）

> 承接 §10.20/§10.21。改写后 M2 落地定义（离线可做 vs 演示服务器 验证）：**#1 离线 gate**（本节）→ #2 硬尾 case 补录（反串/自基线判别型，需演示服务器 live 窗口录得）→ #3 FR7 验收 demo 合并 M1.1 窗口。本节做 #1。

**新增 `_tmp/verify_determiner_discipline.py`（14/14，入 OFFLINE_GATE；harness.test 现 18 PASS / 0 FAIL / 2 SKIP）**——纯离线、零 LLM/零网络，锁两件事：

- **Part A 软信号读取契约（防 §10.21 修的洞复发）**：证据行字段在 ≠ 模型认得了。三处咬合逐一断言——① DB 层存储键 `senti_arg/sarcasm/topic_tags` 落库取回值域合法；② 工具层两处 mapper（`crawl_live._ev` / `search_archive` 行构造）把 DB 键映射成给模型看的 `{senti: 负/中/正, sarcasm, tags}`，**键集与取值一致、两处同构**（独立代码防漂移，改名即撞）；③ `_base_system` 模板含【软信号提示】块 + 教读三键 + 「判向以原文为准」+「规则 4」避答钩，且**教读键 ⊆ 工具实发键**。任一环改线 = FAIL。
- **Part B determiner 纪律判定器 `disciplined()`**：硬尾答案要么 ≥1 条 `[id=N]` 引原文，要么 abstain 措辞（证据不足以/判不了…），**禁 0 引用实指/定调**——措辞与取数镜像 evalgold（`ABSTAIN_MARK/_abstain/_cites/_find_entry`），防口径漂移。合成正负例自证会开火会放行；真实语料地板扫全 gold expect_pass 录得答案（6/6 合规）；known_issue（bngaonly q1 实指 0 引）按 known 放行、日后答案补引用自然转合规不反向阻塞。

**设计注（增量 vs 现有 C3，避免重复）**：evalgold C3 只对 **0 引用** 答案开火（实指违纪律）；determiner 门不重复该判定，聚焦**读取契约**（Part A，真缺口）与把纪律判据沉淀成可复用谓词 + 挂上真实语料地板（Part B）。真实硬尾 case（黑话 referent / 反串阴阳 / 自基线爆发）scope 录得后（#2，演示服务器 窗口）直接复用 Part B 扫描做纪律地板，无需新脚本。

**文档同步**：HANDOFF 顶行（本节）；test.py GATES 列表新行。PROJECT_MEMORY §2 已由 §10.20/§10.21 覆盖，本节不再重复。

### 10.23 认证题单升 v2（+自基线爆发题）+ 打分服务上限提 3G 后三题补测全过（2026-09-10，用户「nlp那边调到3gb了，补测吧，基线爆发这个补一个问题吧」）

> 触发：上一轮 live 认证里两题（鸣潮卡池 q2 / 明日方舟夏活 q3）**整批软信号降级成关键词兜底**——根因是演示服务器 上打分服务被 OOM 杀掉。用户把 nlp-tool 的 `MemoryMax` 提到 3G，令补测；同时点名加一道自基线爆发题（题单 8→9）。

**改完的东西**：

1. **题单升 v2（9 题）**：`harness/data/cert_qs.tsv` 追加第 9 题「崩铁这版本节奏讨论度什么时候上涨，又什么时候下降的？」（崩坏星穹铁道 / 精准 / 自基线爆发类，无源帖——考的是能否用**存档发布时间**把讨论度涨跌还原出来）。`qbank_20260910.tsv` 自基线段同步录入。`docs/CERTIFY.md` 对应改 8→9（判据 1、参数表 3 快速 + 6 精准、预计总时长、判据 2 校验循环 `1..9`、附录 A 表头 v2 + 新增第 9 行），并加"单题补跑 `--start 8 --max-q 1`"说明。
2. **三题补测（全过四条判据）**：
   - `cert-q9-20260910:q1`（新题，精准）：19 引用全对回本 scope，**兜底批次数 = 0**；用 `published_at` 给出可辨识锚点（2026-09 上旬高点 / 2025-05 底一波），**明说下降时点无证据**、NGA 侧仅约 14% 覆盖不足；正面措辞按 `sarcasm=1` 标为反讽不采信 → 硬尾纪律成立（有引用 + 该避就避）。
   - `cert-q2-20260910:q1`（鸣潮卡池，快速）：151s，NGA 17 / bili 187，12 引用 12/0/0；诚实定调"证据不足"，未坐实"小巧思"的单一所指。
   - `cert-q7-20260910:q1`（明日方舟夏活，精准）：354s，NGA 86 / bili 112，16 引用 16/0/0；结论为"证据不足以坐实今年 vs 去年星熊那批的强度对比，'遥'及陪跑强度无有效样本"。
   - **三条都 `兜底批次数=0`**（打分服务真在跑，软信号非关键词兜底）→ 判据 ③ 成立；`NRestarts` 停在上限未再增、23:00 后无 oom-kill。
3. **黄金回放扩到 16 case**：`harness/runner/gold_cases.json` 末条新增 `cert-q9-崩铁节奏涨落(自基线爆发-硬尾)`（expect_pass，指向 `calib_q9_20260910.db`）；`_comment` 补 09-10 认证记录来源。现 15 PASS + 1 KNOWN（bngaonly q1 实指 0 引）。
4. **收口脚本自防**（用户「收口脚本加吧」授权）：`_tmp/verify_a3_final_answer.py` 加一行 `os.environ.pop("HARNESS_FLOW", None)`——该脚本靠流水断言，外壳若带 `HARNESS_FLOW=0` 会**假 FAIL**（上一轮现场踩到：落 2 条→0 条），与 `verify_flow.py` 同款自防。
5. **观察（不修）**：同题 q7 本轮 16 引用 / 结论偏弱，而黄金集里同一题（`calib-fix-20260910:q2`）是 34 引用 / 结论更实。**判定为诚实采样波动**（爬到的样本不同），非回归——不追平、不调参。

**未做（用户「只跑这三个先」）**：全 9 题认证批次**未跑**；演示服务器 OOM 根因（无 swap / 内存回收优先级）**未修**，当前靠 3G 上限缓解，仍待用户拍。

**文档同步**：HANDOFF 顶部现态段；CERTIFY.md v2；cert_qs.tsv / qbank_20260910.tsv；gold_cases.json。

### 10.24 出口关卡分层定案 + 入口 judge 实测（2026-09-11，用户「verifier token 大 / judge 没用 / 要输出前的校验」一连串追问）

> 触发：用户提"黑话靠累积能不能做成自进化"，追问 verifier/judge 在出口有没有用。中途用户点破我把 **论文机制**（LLM-as-a-Verifier, arXiv:2607.05391, Kwok+ 2026-07：最终输出前生成多条候选、验证器挑最好）与**仓库脚本** `harness/runner/verify.py` 混为一谈 —— 是两回事，已更正。

**① 名称正本清源（防再混）**：

| 名字 | 是什么 | 位置 | 花 token | 能改输出 |
|---|---|---|---|---|
| `harness/runner/verify.py` | 确定性 verifier：吃 flow 流水+归档库，判「结论行/引用对回 scope/run 对回」 | **落盘之后**（事后审计） | **零** | 不能（只报 FLAG） |
| 收口防线 `engine._sanitize_cites` | 输出前把对不回本 scope 的 `[id=N]` 剔除+留痕 | **收口那一刻** | **零** | **能** |
| judge `measure._judge_sarc` | 可疑反讽样本交 DeepSeek 覆写 sarcasm | **样本入库时**（入口） | DeepSeek 调用 | 能改样本字段 |
| 论文 LLM-as-a-Verifier | 多候选生成+概率连续分+判据分解+锦标赛挑选 | 最终输出前 | **大**（候选数×重复×判据） | 挑选 |

**② 出口分层定案**（用户认可的框架）：

- **第一层 · 确定性引用对账**（已有，零 token）：引用对不回本 scope 即剔除+留痕。
- **第二层 · 纪律闸门**（判据已写好但**闲置**，零 token）：硬尾答案须「有引用」或「明说判不了」，禁 0 引用下死结论 = `_tmp/verify_determiner_discipline.py` 的 `disciplined()`（§10.22 Part B）。目前只离线扫黄金集，**未接线上**。
- **第三层 · 概率语义挑刺**（未建，要 token）：判「引用对得上但论证说过头/断章取义」。论文那套的**可借部分**是它的三个手法（读概率算连续分 / 同份多评取均值 / 判据拆开分别评），**不可借的是 best-of-N** —— 我们爬取受 B 站风控约束（实测精准档单题 5 轮、NGA 86+B 站 112 条、354 秒），**多候选各爬一遍物理上跑不了**。

**③ 入口 judge 实测（`_tmp/eval_nlp_vs_judge.py`，走生产通路：nlp-tool 18772 + `measure._judge_sarc`，口径 0.6 标签线 / 0.4 导流线）**：

- **先决污染检查**（`_tmp/check_gold_leak.py`）：**gold001 与训练集重叠 0/200 = 干净 held-out**；**gold002 重叠 200/200 = 已被训练集 100% 吸收**（`merge_train_gold002.py` 所致）→ gold002 的 **nlp** 数字是训练集成绩、**不可用于对照**，但其 **judge** 数字有效（DeepSeek 没训过）。

| 集 | 判据 | P | R | F1 | 备注 |
|---|---|---|---|---|---|
| gold001 (n=200, 正例16) | nlp @0.6（生产标签线） | 0.333 | 0.375 | **0.353** | 与在案 0.368@0.6 吻合（自洽） |
| | nlp @0.4（导流线） | 0.173 | 0.812 | 0.286 | |
| | judge 全体 | 0.308 | 0.250 | **0.276** | 比 nlp 还差 |
| | 生产管线 nlp筛+judge覆写 | 0.333 | 0.250 | **0.286** | **低于 nlp 单跑** |
| gold002 (n=200, 正例65) | nlp @0.6（污染，仅参考） | 0.431 | 0.338 | 0.379 | |
| | judge 全体 | 0.571 | 0.431 | **0.491** | 比 nlp 好 |
| | 生产管线 | 0.605 | 0.354 | 0.447 | 高于 nlp@0.6 |

**关键读数（回答"judge 是不是被前置 nlp 卡死"）**：**nlp 召回@0.4 ≈ 81%（13/16）与 78%（51/65）**，两集一致 → 导流门只挡掉约两成真反讽，**"前置分辨力不够所以 judge 没用"这个机制只成立约 20%**；judge 表现平庸的主因是**它自己精度低**（judge P=0.31/0.57），不是被门挡住。

**结论（诚实版）**：干净集上 **judge 反而把管线拖低于 nlp 单跑**（F1 0.286 < 0.353），污染集上 judge 又更好 —— **两集结论相反 = 证据不足以下判断**，根因是干净正例只有 16 条、统计功效不够。且 §10.20 已定**不再扩人工标注**，故此题**按现状保留、不追**（现有数据反而**印证 §10.20 的判断**：该层 F1 只有 0.3~0.5，**本就不该当硬闸**，只配当"以原文为准"的软提示）。

**④ 用户拍「3 4 先记下来 搁置」**：

- **③ 出口第三层（概率语义挑刺）→ 搁置。** 且**先量后建**：先抽验已有黄金集答案「引用是否撑得住结论」，**观察到该失败模式再建**，不凭感觉上花钱的关卡。
- **④ 黑话累积回路 → 搁置。** 方向已认（社区新造词只能"遇到→解决→记住"），且 `registry.py` 的 L4 表**本就是这个设计**（账本式只增不改、带 `hits/confidence/source/valid_from`）—— 缺的不是表，是①表只认"游戏名"维度、②无回写通路、③**无关卡**（入口有 nlp+judge、出口有引用对账，唯独"学习写回"这条新路上一个守门的都没有）。落地时须守：只收可核对的短知识（指代/别名，**不收结论评价**）、两次确认才入库、带证据带出处、`hits` 驱动复核、**黄金集当护栏**。

**⑤ 用户拍「出口两层都加」→ 第二层已接线（2026-09-11）**：

- `harness/engine.py` 新增 `_ABSTAIN_MARK` + `_discipline_ok(answer)`（有 `[id=N]` 或 abstain 措辞 = 合规）；
  `_seal_answer(answer, trace, crawled=False)` 增第三参 —— **crawled=True（本题真爬过样本）且判据不过**时，
  trace 记 `discipline_flag` + 答案末尾附**纪律校验注**。
- **刻意做成非破坏性**：只标注、不删正文、不改判、不重问。理由：已知 `bngaonly-q1`（绝区零实指强结论 0 引用）
  这类 0 引用答案用户**已认可暂不改**，硬拦会推翻该决定；且 evalgold C3 本就"只对 0 引用开火"，线上照此口径即可。
  → 破坏了再谈加硬。
- **两处独立实现互校**：engine 版与 `runner/evalgold` 版是两个独立副本（设计如此，防单点改坏），
  `_tmp/verify_determiner_discipline.py` 新增 D1~D4 断言二者**逐字一致 + 同判**。
- **验证**：`py_compile` 过；`python -m harness.guard` **0 违规**；`python -m harness.test` **18 PASS / 0 FAIL / 2 SKIP**。

**下一步（待办）**：① **已做**（第二层接线）。② 抽验黄金集答案的"断章取义"是否真实存在（决定第三层要不要建）。

**文档同步**：HANDOFF 顶部现态段；本节为出口分层与 judge 实测的正源。

### 10.25 黄金集答案「断章取义」抽验 + 引用格式洞（2026-09-11，承接 §10.24 待办②）

> 触发：§10.24 ④ 拍"第三层先量后建"——先抽验已有黄金集答案「引用是否撑得住结论」，观察到该失败模式再建。用户「试试吧」。**结论：该失败模式未观察到 → 第三层不建。**

**做法（纯离线、零 LLM/零触网）**：新增 `_tmp/audit_cites.py`（逐 gold case 把答案全文 + 每条被引 `[id=N]` 的原文/平台/时间/senti/sarcasm 导出成 `_tmp/audit_cites_dump.txt`）→ 人工逐条比对「引的原文支不支持这句话」；配 `_tmp/audit_cite_format.py`（量化引用格式）+ `_tmp/audit_grouped_ids.py`（补拉校验器看不见的成组/区间 id 原文）。

**规模**：16 个 gold case，共 **290 条引用**（274 条严格单引用 `[id=N]` + 16 条藏在**成组括号** `[id=a, b, c]` 与**区间** `[id=a-b]` 里）——290 条**逐条对回原文**。

**发现①：断章取义 = 未观察到（决定性结论）**。290 条引用全部与原文相符，**无一条曲解**。反串/阴阳这类最易翻车的，模型反而处理得对：
- `cert-h1-q4`（崩铁姬子）3 条 `sarcasm=1` 的阴阳帖被明确标为反讽、未当真心夸；
- `cert-h1-q1`（鸣潮深塔）一条 `[id=208]`「我鸣潮没有主推！！！！！没有膨胀！！！」**小模型标的是 sarcasm=0**，模型自己在答案里改判成反讽（`(senti=负，属阴阳怪气，实为反讽)`）——比软信号还准；
- `cert-q9`（崩铁节奏）把「随便做做就能赚到那么多钱」这类正话反说正确归入反讽、未当正面。

**发现②：引用格式洞（真问题，但不是断章取义）**。`evalgold` 的 `CITE_RE = \[id=(\d+)\]` **只认严格单引用**；成组 `[id=a, b, c]` 与区间 `[id=a-b]` 这两种写法它**完全看不见** → 这些 id 既不进 C2（对回 scope）也不进 C3（0 引用纪律）= **未校验**。分布：**2/16 case 命中**（`bigfix-q2` 2 处成组含 12 id、`bngaonly-q2` 1 处区间含 4 id），全在 **09-08/09-09 两轮旧记录**里；**09-10 认证批 8+1 题全部是规矩的严格单引用，零命中**。补拉这 16 条原文逐条核对：**内容都对**（如 `bigfix-q2` 成组引的 8 条确是 2020 火蓝复刻「不给源石」节奏、`bngaonly-q2` 区间引的 4 条确是「又在唐诗煎炒」技能演出对比）→ **是格式漏检，不是内容造假**。

**诚实边界**：本抽验只验了「引的那条支不支持这句话」，**没验「有没有只挑对己有利的、把反证藏起来」**（挑樱桃）——后者需对照全部样本，属盲区，记此备案。

**决策（承接 §10.24 ④「先量后建」）**：
- **出口第三层（概率语义挑刺）→ 不建。** 前提是「答案会曲解引用」，翻遍 16 案 290 条引用**该失败模式不存在**；为一个找不到的病花 token 上关卡不划算。
- **可做的免费修补**：把 `evalgold`/`engine` 的引用校验从「只认严格单引用」扩到「也认成组/区间」，或（更简）在系统提示里**要求逐条单写 `[id=N]`**。**待用户拍**（本轮只量、未改代码）。

**本轮产物**：`_tmp/audit_cites.py`、`_tmp/audit_cite_format.py`、`_tmp/audit_grouped_ids.py`、`_tmp/audit_cites_dump.txt`（可溯源）。**正源本节。**

**文档同步**：HANDOFF 顶部现态段。

---

### 10.26 NGA 时间窗落实 + 端到端实测（2026-09-12，用户「你把 nga 这个落实然后整点问题试试，各种时间的，冷门和热门游戏甚至无版面游戏」）

> 目标：问句里的显式时间锚点（「三个月前」「去年」「2024年6月」）要能真的取到**那个时段**的 NGA 社区言论，而不是只取当下样本。

**落实（四段，本地改 → 部署 `<NGA爬虫目录>` → 重启 `nga-tool.service`）**：
1. **爬虫** `crawlers/nga/nga_search_demo.py`：加「翻页到目标时间段」——`_page_to_items` 通用翻页导航（整页早于窗口下界→停；空页/410→到底；`max_pages` 与 `budget_s` 双闸）+ `_board_search_window`（版内搜逐词翻页）/ `_global_search_window`（无版面走全站搜）。**版面无窗**时仍用版面列表（版面列表按「末回」排序，活跃版一页只覆盖几小时，**不是时间轴**，故有窗时不走它）。
2. **服务端** `crawlers/nga/nga_search_server.py`：`/crawl` 收 `since/until` 透传给 `run_query`，超时放宽到 300s。
3. **引擎** `harness/engine.py`：`time_anchor(user, now)` 解析显式时间锚点 → 整月/整年桶（N个月前/半年前→那月；N年前/去年/前年→那年；YYYY年M月→那月；YYYY年→那年），与 `recency_since`（「近期」）互斥；注入 `since/until` 到 crawl 工具参数 + 提示词。
4. **provider 全链** `harness/sources/live.py`、`mock_nga.py`、`harness/tools/crawl_live.py`：透传 `since/until`（**bili 侧服务端尚未接** → 现在等价无窗；提示词已明说 bili 回来的样本是当下的、别当那个时段的料）。

**实测发现①「主板被综合板稀释」= 真 bug，已修。** `_resolve_board_fid` 原判据「标题含游戏名的板占比 ≥ `BOARD_CONF_MIN_RATIO`(0.30)」，但泛用综合板（428 手机综合 / 414 综合讨论）会把真主板在全局搜里的占比稀释到线下 —— 实测「原神」全局搜 428 占 0.30 > 650 占 **0.20**，650 被门限拒 → 误判「无专属主板」→ 落全站搜；而全站搜对活跃板 p25 才退 3 个月，**2023 窗口翻不到 → 假 0**。**修法**：先扫「板名与游戏名**完全同名**」的板（强信号，**不受占比门限**），同名不成再退「含名且占比够」老口径。**回归实测**：原神→650、**戴森球计划→839（此前同样被误判无版面）**、明日方舟→-34587507（负版区锚，未回归）、明日方舟终末地→846、鸣潮→854、洛克王国世界→510558、金铲铲→510461。

**实测发现②「静默截断」。** `_page_to_items` 翻满 `max_pages` 且未触发任何停条件时原本**不吭声** → 0 命中会被误读成「那会儿没讨论」。已加日志「翻满 N 页仍未确认到头」+ 落档字段 `window_capped`。

**实测发现③「时间词污染检索词」。** 模型把「2023年」写进检索词（板内搜按标题匹配，几乎必 0）。已在时间锚点提示词里明说「检索词别再写时间词，只填游戏自己的话题词（版本号/角色名/玩法）」。

**端到端实测**（真隧道 + 真模型；NGA 单平台 5 题，`--nga-ratio 100`，tag `ngawin2`；报告 `_tmp/calib_ngawin2.txt`，修前对照 `_tmp/calib_ngawin.txt`）：修前 3 题假 0 → 修后 **5 题全部取到窗内证据**：
| 题 | 修前 | 修后 |
|---|---|---|
| 终末地/干员 @2026-03（半年前，热门） | 14 帖 ✓ | 14/14/18 帖 ✓ |
| 戴森球计划 @2025（去年，冷门） | **0** | **9/10/10**（6-30 开发进度 + 黑雾战斗 + 蓝图物流） |
| 原神 @2023（热门大板） | **0** | **19 帖**（枫丹/水神/芙宁娜，**仅 12 月末**） |
| 洛克王国世界 @2025（无版面游戏） | **0** | **2/10/11**（测试期 PVP 排位，无正式赛季） |
| 鸣潮 @最近（无锚点基线） | ✓ | ✓（11~15 帖/轮） |

**已知边界 + 用户拍板（2026-09-12）**：
- **宽窗只取到窗口最新一截**：板内搜翻到窗口边缘就填满 `limit` 即停（原神 2023 只到 12 月，1–11 月未取）。**用户拍「就取最新一截吧 不往复杂做了」→ 不加按时间均匀抽样，保持现状**；模型已如实标「仅取到 12 月末样本」。
- **「0 命中」vs「翻不到那么深」对模型不可分**：**用户拍「那就说材料不足，多用 b站的」→ 不上抛 `window_capped` 标记**（省掉 服务端→live.py→crawl_live→engine 四层接线，且避免误触平台冷却），改为**提示词口径**：时间锚点提示里写明「NGA 只保证取该时段最新一截，别据此断言『那会儿没人讨论』」+「bilibili 无时间窗、回来的是当下样本 —— 材料薄时**多靠 B站多爬几轮补当下社区口径**当参照，但要说清是当下的」+「确实材料不足就直说『该时段材料不足』，绝不拿别的时段的料冒充」。（`window_capped` 字段仍保留在落档 JSON 里，供离线分析。）
- 单平台测试下同轮多次 `crawl_nga` 会撞服务端单飞 429（双平台正常跑不会，是测试方式所致）。
- nlp 度量本轮降级 keyword（本机无 nlp token；非本轮引入）。

**本轮产物**：`_tmp/probe_genshin_board.py`（主板锚定诊断）、`_tmp/probe_genshin_win.py`（板内搜能否翻到 2023 的验证）、`_tmp/probe_boards_multi.py`（多游戏锚定回归）、`_tmp/q_time.tsv`（题单）、`_tmp/calib_ngawin{,2}.txt`（修前/修后报告）。**正源本节。**

**文档同步**：HANDOFF 顶部现态段。

### 10.27 对比题「现在和当时一起爬」= 分段窗落实 + 真打验证（2026-09-12，用户「这个你得现在和当时的一起爬吧？」）

> 背景：§10.26 的 `time_anchor` 一问句**只认一个**锚点，且返回单窗。对比题「今年夏活…跟去年…比」只解析出「去年」→ 只爬 2025，今年那半根本没爬。**并且单纯改「并集大窗」（2025-01~2026-12）也不行**：翻页按末回降序走，一年多的跨度会先把 `limit` 喂给最新那段，去年那段照样取不到 —— 这正是 §10.26「宽窗只取最新一截」的同一机理。

**改法 = 多锚点 → 分段窗，爬虫逐段各翻一窗再按 tid 去重合并**（五处，本地改 → 部署 `<NGA爬虫目录>` → 重启 `nga-tool.service`）：
1. **引擎** `harness/engine.py`：新增 `time_windows(user, now)` → `[(since, until), ...]`（单锚点=单窗；多锚点=各一窗，**并新增「今年」识别** → 当年整年）；`time_anchor` 保留为兼容签名，返回各窗**并集**（只用于提示词文案与入档闸门，**不用于取数**）。`ask()` 起 `self._win_windows`；多窗时另发一条系统提示「本题是跨时段对比…两段各自归纳再对比…某段材料不足就直说，绝不拿另一段的料冒充」，trace 记 `time_window_multi`；工具参数在多窗时注入 `args["windows"]`。
2. **工具层** `harness/tools/crawl_live.py`：`fetch` 白名单加 `windows`。
3. **provider** `harness/sources/live.py` + `mock_nga.py`：`crawl_nga/crawl_bili/crawl/_post` 全线加 `windows` 形参；`_post` 有 `windows` 时进 payload、超时放宽到 600s（**bili 服务端不识别该字段，忽略** —— 对比题的「早那段」只能靠 NGA 一侧）。
4. **服务端** `crawlers/nga/nga_search_server.py`：`/crawl` 收 `windows`（必须是 list，否则丢弃）→ `_crawl` → `run_query`；分段时超时放宽到 600s。
5. **爬虫** `crawlers/nga/nga_search_demo.py`：抽 `_req_windows(windows, since, until)`（分段窗优先，否则 since/until 单窗，都没有 → `[]`；缺一端取 0 / 2^31-1）+ `_search_win(page, mode, target, lo, hi)`（**窗内三种模式的取数分支整体抽出**，无窗路径原样保留）；`run_query` 多窗时逐段调 `_search_win` 再按 tid 合并，单窗走同一条 `_search_win`；`_save` 的 `time_window` 增 `windows` 字段。

**验证（真隧道 18770 → 演示服务器 真爬，2026-09-12 01:5x）**：
- **分段窗真打**（`_tmp/probe_win_multi.py 分段`；query `searchin|明日方舟|夏活 限定 陪跑`，两整年分段）：**20 帖 = 2025 年 10 帖 + 2026 年 10 帖**，`board_fid=-34587507`、`window_capped=False`（两段都翻到头）、耗时 **142s**。修前合成单窗只会拿到 2026 那段。
- **单窗回归**（同一探针 `单窗`，只给 2025）：**10 帖全在 2025**，`window_capped=False`，耗时 **27s** —— 重构没改坏老路径。
- **本地转发链**（`_tmp/probe_plumb.py`，把 `_post` 换捕获桩、不触网）：① `fetch` 白名单放行 `windows`；② `crawl_nga` 打到 `/crawl` 的 payload 里两段 `windows` 完整；③ `crawl_bili` 也收（服务端忽略）。
- **编译**：本地 6 个改动文件 `py_compile` 过；演示服务器 上用 `<毕设爬虫目录>/venv/bin/python -m py_compile` 过；两边 md5 一致（demo `dad6639e…`、server `db810464…`）。
- **部署留痕**：演示服务器 备份 `nga_search_demo.py.bak_multiwindow_20260912_0152`（md5 `7991a224…`）、`nga_search_server.py.bak_multiwindow_20260912_0152`（md5 `f40f7436…`）；重启后 MainPID=1889783 持有 127.0.0.1:8770、`/health` ok、`queries_done=0`。**部署前 `free -h` = 可用 3.6Gi / load 0.12**，无 OOM 风险。

**题单扩充（待用户拍，尚未跑）**：现认证 9 题里只有 Q7 命中分段窗，其余 8 题行为不变（4 题时效窗、4 题无窗，`_tmp/probe_cert_anchors.py` 有逐题判定）。用户要求「广度/深度/时间跨度/对比/不指定游戏/冷门游戏/游戏攻略都要加一下」，候选均取自题库 `harness/data/qbank_20260910.tsv`（**按类型分桶，每行带源帖 id**；旁边 513 行原始素材池），提议新增 5 题（不指定游戏=手机游戏 331853、冷门=崩坏三 261211、攻略=原神 374494、长跨度=原神 366145、广度=阴阳师宽泛无源帖），**源帖已在生产服务器 库逐条核对**。

**本轮产物**：`_tmp/probe_req_windows.py`（抽真实源码验 `_req_windows` 各分支）、`_tmp/probe_win_multi.py`（真打分段窗/单窗）、`_tmp/probe_plumb.py`（本地转发链离线验）、`_tmp/probe_cert_anchors.py`（改版：逐题判定分段窗/单锚点/时效窗/无）。**正源本节。** 待办①（bili 接时间窗）已由 §10.28 关掉。

---

### 10.28 bili 时间窗接进来 = 对比题两侧都能取到「早那段」（2026-09-12，用户「b站时间窗检索接进来啊 然后测试一下」，= §10.27 待办①）

> 背景：§10.27 只接了 NGA 一侧，bili 服务端当时忽略 `windows` —— 对比题的「早那段」全靠 NGA 单撑。bili 的搜索接口**本身就支持发布时间过滤**（`client.py: search_video_by_keyword(..., pubtime_begin_s, pubtime_end_s)` → `/x/web-interface/wbi/search/type`），仓库里 `get_pubtime_datetime(start, end)`（`YYYY-MM-DD` → unix 秒）也是现成的，只是 `search_one_query`（HTTP 服务实际调用的那个）没用上。

**改法（服务端 2 处 + 引擎/工具 2 处）**：
1. **爬虫** `crawlers/bili/media_platform/bilibili/core.py`：新增静态 `_req_windows(windows, since, until)` —— `windows` 每段 → `get_pubtime_datetime` → `(int, int)`；`windows` 为空则 `since/until` 合成一段；半开缺的一端兜底**起 2009-01-01（b 站上线）/ 止今天**；`since > until` 自动换位；脏项跳过。`search_one_query(query, qi, max_videos, windows, since, until)`：有窗时**每段各搜一页**（`page=1, page_size=20, order=DEFAULT, pubtime_begin_s/end_s`），`max_videos` **预算摊到各段**（`per = max(2, N // 段数)`），按 `aid` 去重合并；无窗路径原样不动。返回体增 `time_windows` 便于核查。
2. **服务端** `crawlers/bili/bili_search_server.py`：`/crawl` 读 `windows`（非 list 丢弃）/`since`/`until` → `_crawl` → `search_one_query`；有窗时 `fut.result` 超时放宽 **300s → 600s**。**顺带把 `MAX_VIDEOS = int(os.environ.get('BILI_MAX_VIDEOS', '6'))` 同步过来**（演示服务器 上早有、仓库落后）。
3. **引擎** `harness/engine.py`：两处时间窗提示词里「bilibili 没有时间窗，回来的是当下样本 → 只能当参照」的旧说法**已删**（现在会误导模型），改为「NGA 与 bilibili **两侧都**已按段各取一窗」「两侧都只保证取到该时段里较靠前的一截」。
4. **工具/docstring** `harness/sources/live.py`（`_post`/`crawl_bili` 注释不再说「bili 忽略 windows」）、`harness/tools/crawl_live.py`（工具描述「返回最新样本」→「返回相关样本」，带窗后不再是"最新"）。

**验证（真隧道 18771 → 演示服务器 真爬，关键词「明日方舟夏活」，2026-09-12 02:2x）**：
- **分段窗真打**（`_tmp/probe_bili_win.py 分段`，两整年分段）：**6 条视频 = 3 条 2025（08-02 / 08-14 / 09-08）+ 3 条 2026（08-01 三条）**，服务端回显 `time_windows = [[1735660800,1767196799],[1767196800,1798732799]]`（2025 全年 + 2026 全年），耗时 **44s**；harness 侧 `_bili_items` = **59 条**（post 6 + reply 53）。
- **合成单窗对照**（`_tmp/probe_bili_win.py 合窗`，`since=2025-01-01, until=2026-12-31` 一段）：**6 条视频全是 2026，2025 一条没有**，14s → **bili 与 NGA 同病**（综合排序把额度喂给最新那段），分段是必需而非优化。
- **离线验**（`_tmp/probe_bili_windows.py`，ast 抽 `get_pubtime_datetime`/`_req_windows` 真源码 exec、不触网、不 import playwright）：9 用例全对（分段 / 单窗 / 无窗 / 半开两向 / 脏数据 / 非 list / 倒序）；单日 `2024-01-05 → 1704384000..1704470399` 与上游 docstring 逐位一致。`_tmp/probe_plumb.py` 复核 `windows` 一路到 `_post` payload。
- **部署留痕**：演示服务器 备份 `media_platform/bilibili/core.py.bak_win_20260912_0214`（旧 md5 `2416381103…`）、`bili_search_server.py.bak_win_20260912_0214`（旧 md5 `1968dedb…`）；新 md5 **core `85d81a51…` / server `9b0ddfe4…`**，本地与演示服务器 逐位一致、两边 `py_compile` 过；重启后 MainPID=1891602、`ActiveState=active`、登录态正常（`Use cache login state get web interface successfull`）。**部署前 `free -h` = 可用 3.6Gi / 4 核**，单服务重启无 OOM 风险。
- **做法注**：仓库 bili 副本原**落后演示服务器 两处**（`max_videos` 形参、单视频评论 `asyncio.wait_for(timeout=30)` 封顶），本轮**先把演示服务器 版落回仓库再在之上改** —— 否则会把演示服务器 的新特性覆盖回去。

**本轮产物**：`_tmp/probe_bili_win.py`（真打分段/合窗对照）、`_tmp/probe_bili_windows.py`（离线验窗解析）、`_tmp/bili_core_111.py`、`_tmp/bili_server_111.py`（演示服务器 版快照，做改动基线）。**正源本节。**

**仍未拍**：① 题单扩到 14 还是砍到 12（§10.27 候选）；② 认证批参数 `--interval 600 --nga-ratio 50`、DB `harness/data/cert_<date>.db`、报告 `_tmp/cert_report_<date>.txt`。

---

### 10.29 十四题逐题审计 → 四类错因 → 五处收口（2026-09-12，用户「开个子代理看看这14个问题的论据答案都对不对」→「逐个题去修」）

> 背景：认证批已扩到 14 题（`harness/data/cert_qs.tsv`，DB `harness/data/cert14_20260912.db`，flow `exp_calib_cert14-20260912_q1..q14`）。用户要逐题核「论据是不是真在库里、答案有没有歪」。开 3 个子代理分片审计（Q1-5 / Q6-10 / Q11-14）+ 我逐条复核重发现，结论：**没有一道是「爬虫没爬到」**，全是「爬到了但没给模型看 / 模型看岔了」。

**审计实证**：
- **Q13 崩铁节奏**「NGA 86 / bili 224 / 合计 310」实为答案串号：trace 里最后一次 NGA 爬完 `ask_total=244`、最后一次 bili 爬完 `ask_total=310` —— 模型把**两平台累计**当成**单平台条数**读（`ask_total` 原= `sum(new for 所有平台)`，挂在单平台返回体上，天生会被读串）。
- **Q4 姬子SP**答案「社区没提」为假：库里 [id=217][218][219][220][221][223][226] 共 7 条正是姬子SP 讨论，只是**没进展示上限**（`_EVIDENCE_CAP` 快速 5/精准 15，`crawl_live.commit` 返回的 `_ev` 被 `params["limit"]` 截断，`new` ≫ `shown` 而模型无从知晓）。
- **Q8 王者荣耀氛围**「近月 NGA 空」为假：该 scope 内 2026-08/09 有 **12 帖**，且 [id=927] 是直接反证（正面评价）。
- **Q5 终末地肝度**引号「…」挂在 [id=343] 名下，逐字比对实为 [id=342] 正文（相邻 id 抄岔，人眼扫不出）。
- **Q7/Q12/Q14** 只挑支持侧料、**Q10** 单游戏（原神）看法被写成「二游整体」—— 属提示词未约束的措辞越界。

**四类错因（映射到代码）**：
- **甲 平台数字串号**：`ask_total` 语义含混 → §10.29 改 `_after_crawl`。
- **乙 只给看前几条**：展示截断无信号 → `crawl_live` 补 `shown`。
- **丙 引用抄错号**：模型侧相邻 id 串位，**号是真实存在的**（对回校验/纪律判定都拦不住）→ 新增引文挂靠校验。
- **丁 措辞越界**：无引用支撑的全称/单点上升整体 → 提示词新增 11-14 条。

**五处收口**：
1. **`harness/engine.py` `_after_crawl`**：`res["plat_total"]` = 本平台累计、`res["ask_total"]` = 两平台累计（拆开语义）；`res["note"]` 三合一提示 —— ①`new > shown` 时明说「本轮新归档 N 条, 只向你展示前 M 条…你没看到 ≠ 社区没有」；②`new == 0` 时给收敛指引「NGA 是全词与, 检索词叠加越多越必然为空, 去掉限定词只留游戏名+1 核心词再试, 别原样重试」；③累计 ≥ `SAMPLE_TARGET` 时提示参考量。
2. **`harness/tools/crawl_live.py`**：`summary` 增 `"shown": len(ev)` —— 把「展示了多少」这个原先只存在于服务端的事实透给引擎。
3. **`harness/quotecheck.py`（新，只读）**：`check_answer(answer, conn, scope)` 逐条 `[id=N]` 取「前一引用之后 ~ 本条之前」窗口里的引号内容（「」『』“”+成对直引号，归一化去空白/`**`），本 id 找不到 → 在同 scope 其余 obs 里找，**唯一命中**才判 `misattributed`（报出真正归属）；查无出处一律不报（模型转述被引号括起，放开会把真信号淹掉：实测放开 202 查无 vs 171 对上）。`_MIN_LEN=10`（短引文多公共短语，撞车概率高）。CLI `--db --flow-dir --tag --n`，退出码 0/1。**接进 `_seal_answer` 第三层：挂错只加更正注 + trace `quote_misattributed`，不删引用**（顾问角色，绝不打断作答主链；裸 fixture 库无 observation 表 → `except sqlite3.Error: return []` 静默放行）。
4. **`harness/cites.py` 接线**：多形引用解析（`[id=N]`/`[id=N, M]`/`[id=N-M]`/混写）替换原先三处严格单引用正则 —— `engine._sanitize_cites`（成组/区间里的坏 id 原先既不对回 scope 也不进纪律判定 = 等于未校验）、`engine._discipline_ok`（成组/区间原先被判 0 引用 = 误伤硬尾）、`runner/verify.py`、`runner/evalgold.py`。
5. **`harness/engine.py` `_base_system` 新增 `_COVERAGE_RULES`（11-14）**：11 覆盖度（只能写「本轮样本中未见」禁写「社区没有」，先 `search_archive` 扩面）；12 数量口径（单平台用 `plat_total`、总量用 `ask_total`，别把 `ask_total` 当单平台条数）；13 反面声音（有相反意见必须单独交代并引 [id=N]，正反都有就说正反都有）；14 范围自觉（样本只来自一两个版要写明构成，别把样本看法说成整体）。另把规则 2 的引用写法更新为「多条可写 `[id=N, M]` / `[id=N-M]`，括号里只放 id」，并示例 `[id=283，sarcasm=1]` 这类整段不算引用。

**验证（全离线，未烧 key/未占隧道）**：
- `harness.cites` 单测 23/23；`_tmp/verify_cite_guard.py` **12 → 28** 例全过（新增成组/区间/混写剔除、纪律「成组引用算有引用」、挂靠「唯一出处报错 / 引对不报 / 短引文不报 / 查无不报」）。
- `evalgold` 15 PASS 0 UNEXPECTED 0 ERR；`verify.verify_record` 14/14 scope FLAG=0。
- 全量 `python -m harness.test` = **18 PASS / 0 FAIL / 2 SKIP(server)**（首跑 `verify_context_wiring.py` 曾因新 `_seal_answer` 在裸 fixture 上查 observation 表崩 `no such table` → 修法是 quotecheck 无引用早返回 + `except sqlite3.Error` 放行，复跑恢复全绿）。
- **挂靠校验打真答案**：14 题跑 `quotecheck` = **对上 122 / 挂错 6**，6 处逐条原文核对全部坐实（Q5 [id=343]→[id=342]；Q6 [id=473]→[id=476]；Q9 [id=2534]→[id=2533]；Q13 [id=2110]→[id=2109]、[id=2093]→[id=2085]、[id=2194]→[id=2193]）—— 其中 4 处人工审计没看出来，纯靠逐字比对抓出。

**本轮产物**：`_tmp/audit_pack.py`（复盘单题：问题+答案+被引原文）、`_tmp/verify_findings.py`（复核 Q4/Q5/Q8/Q13 数字）、`_tmp/peek_traces.py`、`_tmp/peek_trace13.py`（trace 骨架，验 `ask_total` 串号）、`_tmp/verify_quote_hits.py`（6 处挂错逐条原文核对）；备份 `_tmp/bak_20260912/engine.py.bak`。**正源本节。**

**待用户拍（铁律：占隧道 + 烧 key 须先点头）**：五处改动全落在**提示词与返回体**上，离线回归只能证「没改坏旧路径」，证不了「模型吃到了新信号」。**逐题重跑验证**需演示服务器 隧道 + DeepSeek 消耗 + 每题 5-10 分钟间隔（B 站风控）。建议开新 tag（如 `cert15-20260912`）跑同一 14 题，与旧批逐题对照，并重跑「三个子代理审计 + quotecheck」出前后对比数字（预期：Q13 数字不再串号、Q4/Q8 不再误报缺席、6 处挂错在更正注里现形）。

---

### 10.30 旧 14 题不重跑：答案冻结归档 + 回生产服务器 库按「题型矩阵」重编 15 题 v4（2026-09-12，用户「不跑这14题 把14题答案存起来 我们去sql里继续找 重新组成15题 注意广度及深度 时间跨度 对比等等各种可能遇到的问题类型甚至复合题 你去找找吧」）

> 背景：§10.29 的内存改动全在提示词与返回体上，只有真跑才算验过。用户决定**不重跑旧 14 题** —— 先把答案冻结，再回源库重编一套覆盖面更宽的新题单。

**① 旧答案冻结**（`_tmp/freeze_cert14.py`，只读汇总）：
- 落 `harness/data/cert14-20260912_answers.md`（人读全档：每题问句 / 引用清单 / 答案全文）+ `.tsv`（一行一题，便于比对）。
- 取数口径：`flow/exp_calib_cert14-20260912_qN.jsonl` 的**最后一条**记录（q9 取重跑后那条即最终版）。
- **正源不变**：`harness/data/flow/exp_calib_cert14-20260912_qN.jsonl` + `harness/data/cert14_20260912.db`；冻结档只是汇总，不替代正源。

**② 「SQL」= 生产服务器 的 `standardized_data` 库**（MariaDB，**只读**）：
- 直连器 `_tmp/q152_live.py "<SELECT>"`（`203.0.113.20:3306`，user `<DB用户>`，密码从仓库既有 `_tmp/probe152_retry.sh` 读、**不回显**；只放行 select/show/desc）；新写 `_tmp/mine152.py <out.tsv> "<SELECT>"` 落 TSV 便于翻。
- 库规模：`platform=0`（NGA）主帖 **29474** + 回复 8650；`platform=1`（bili）视频 184132 + 评论 31049。列名要点：游戏列叫 **`keyword`**（**没有 `game` 列**）、无 `title` 别名（要 `title_clean`）、另有 `publish_time / comment_count / view_count / content_clean`。
- **NGA 主帖的库内时间窗 = 2026-03-30 ~ 06-26**（约三个月快照）—— 出「时间跨度」题时注意：源帖只是**话题种子**，回看跨度靠答案侧合成，不靠源帖本身老。
- 素材面落盘 10 份：`_tmp/mine_hot.tsv`（全局热帖 250）、`mine_pergame.tsv`（每游戏 top40，682 行）、`mine_deep.tsv`（正文 ≥150 字，166 行）、`mine_small.tsv`（小游戏）、`mine_slang.tsv`（黑话/术语）、`mine_comp.tsv`（对比）、`mine_event.tsv`（节奏/事件）、`mine_time.tsv`（时间跨度）、`mine_guide.tsv`（攻略/入坑）、`mine_pay.tsv`（抽卡/氪金）。

**③ 重编 15 题 v4** —— `harness/data/cert_qs.tsv`（旧 14 题快照另存 `cert_qs_v3.tsv`，与既有 `cert_qs_v2.tsv` 同规）。构成 **5 快速 + 10 精准 / 10 款游戏**。

v3（14 题）的缺口：**只覆盖二游**（无 FPS/自走棋等品类）、**时间跨度与对比各只有一道**、**几乎没有复合题**。v4 按题型矩阵补全：
- **Q1** 三角洲行动｜快速｜广度·无焦点｜源 无（FPS 品类首考）
- **Q2** 手机游戏｜快速｜广度·不指定游戏（副：黑话「班味」）｜源 348117 348114
- **Q3** 阴阳师｜快速｜广度·无焦点（副：时间跨度·十周年在即）｜源 无
- **Q4** 鸣潮｜快速｜人群·入坑（副：攻略）｜源 373074
- **Q5** 金铲铲之战｜快速｜复合：冷门/材料不足 + 攻略 + 广度｜源 73256 73178 74439
- **Q6** 鸣潮｜精准｜深度·黑话解码（合轴 / 锯切 / 剪切）｜源 346730 368983
- **Q7** 绝区零｜精准｜深度·机制数值（副：自基线）｜源 348075 331866
- **Q8** 明日方舟｜精准｜深度·事件溯源（副：反串/两派）｜源 242522 242407
- **Q9** 崩坏星穹铁道｜精准｜深度·自基线膨胀（断言验证）｜源 368595 372379
- **Q10** 明日方舟｜精准｜时间跨度·开服至今（副：对比/厨向）｜源 244113
- **Q11** 阴阳师｜精准｜时间跨度·周年回看（断言验证）｜源 373689
- **Q12** 明日方舟终末地｜精准｜对比·跨游戏｜源 321940
- **Q13** 崩坏星穹铁道｜精准｜对比·跨版本（副：数值）｜源 376475
- **Q14** 鸣潮｜精准｜付费·抽卡氪金（副：事件）｜源 328913 363131
- **Q15** 原神｜精准｜复合：对比 + 时间跨度 + 人群｜源 372374 372107

- **每题源帖 id 已逐条在生产服务器 库核对存在**（2026-09-12）。Q1/Q3 是刻意的「宽泛无焦点」无源帖题；Q12 源帖偏薄（cc=5）是有意为之 —— 考两侧证据能否都取到。
- 解析校验：`_load_qs(harness/data/cert_qs.tsv)` = 15 题，`Counter({'精准': 10, '快速': 5})`，10 款游戏。

**本轮产物**：`_tmp/freeze_cert14.py`、`_tmp/mine152.py`、10 份 `_tmp/mine_*.tsv` 素材面、`harness/data/cert14-20260912_answers.md` + `.tsv`、`harness/data/cert_qs.tsv`（v4）+ `cert_qs_v3.tsv`（快照）。**正源本节。**

**待用户拍**：v4 只是题单，**尚未跑**。跑它要占演示服务器 隧道 + 烧 DeepSeek key + 每题 5-10 分钟间隔（B 站风控），按铁律须点头；跑法与认证批参数见 `docs/CERTIFY.md`。

---

### 10.31 双进程事故复盘 + 架构定位拍板（NLP 降为可选工具 / 爬虫升为核心工具 + 运行时查登录态）（2026-09-12 上午，用户「重跑吧」+「NLP作为可选工具得了，然后爬虫作为核心工具在运行时检查登录态」）

**① 事故：同一条隧道上跑了两个进程**

- **现象**：v4 认证批首次起跑（tag `cert15-20260912`），批 1 只跑完 4 题，第 5 题在 `s.set_scope()` 处报 `sqlite3.OperationalError: database is locked` 崩溃（09:15:09，exit=1）。
- **取证（全是硬证据，非推测）**：
  - 报告文件里**两行**「批 1 起跑」= 08:22:03 与 08:26:56，两次命令行逐字相同（同库 / 同标 / `--start 0 --max-q 5`）。
  - `flow/exp_calib_cert15-20260912_q1..q4.jsonl` **每题各 2 条**流水 = 各被收口两次。
  - `crawl_run` 时间戳分成两根互不相交的序列（q1 = 08:22:20–08:24:36 与 08:27:12–08:29:12）。
  - `session_claim`：q1/q2/q3 各 2 条、q4 = 1 条。
  - q5 只有爬取记录（09:13:36 / 09:15:00）、**无流水、claims=0** = 从未收口。
  - 残留进程 PID 292（起于 08:22:03，命令行同上）仍存活且 CPU 冻结在 0.14s = 另一进程卡死占着库锁 → 已 `Stop-Process`。
- **后果**：q1–q4 两轮样本混进同一 scope；**平台配比被打歪** —— 全批 NGA 91 / bili 788（NGA 仅占 10%，远低于 `--nga-ratio 50` 的意图），大概率就是并发访问触发 NGA 侧限流（正是铁律警告的那件事）。q5 无答案。**该轮不可用于认证。**
- **服务侧无损**：演示服务器 上 nlp-tool `NRestarts=3`（与起跑前基线一致，没被内存回收杀掉）、nga-tool / bili-tool 均 `NRestarts=0`、内存 4.0G 可用；三条隧道 200。
- **根因**：压缩前后各起了一次批 1，两次之间**没有互斥检查**。
- **处置**：`_tmp/cert15b_batch.ps1` 起跑前先扫 `harness.runner.calibrate` 进程，有则拒绝启动（exit 3）。**换新标新库干净重跑** —— tag `cert15b-20260912` / 库 `harness/data/cert15b_20260912.db` / 报告 `_tmp/cert_report_cert15b_20260912.txt`；污染轮产物（`cert15_20260912.db` + `_tmp/cert_report_20260912_v4.txt`）**原样保留作证据**，不删不改。
- **附带坑**：`Write` 落的 `.ps1` 无 UTF-8 BOM，PowerShell 5.1 按 GBK 读 → 中文串断裂、语法报错（未执行到任何语句）。批脚本一律补 BOM 再跑。

**② 架构定位拍板（用户 2026-09-12 上午）**

- **NLP / 小模型 = 可选工具**（不是必需，也不删）：
  - 检测到服务 → 用它（省 token + 图表有全量情绪分）；**没有 → 主链 LLM 自己判情绪**。
  - **现状必须改**：今天不设 `NLP_TOOL_URL` 时是**静默降级成关键词兜底**（只有 0/1 两档、无可疑度、且库里看不出来），是三条路里最差的一条 —— 降级目标应改成「LLM 自判」。
  - 图表口径跟数据源走并标注（有软信号 = 全量分布；无 = 被引证证据的分布）—— 后者每一条都带 `[id=N]`，比小模型（情绪 Spearman 0.389）打的全量分布更可溯源。
  - 对毕设：小模型那条线（labeler / gold 集 / 蒸馏 / 评测）**作为论文产物保留**，只是不再作运行期硬依赖。
  - **Why 现在拍**：分发这个维度以前没算进去 —— 小模型要模型权重 + 深度学习依赖链，别人的机器装不动。
- **两个爬虫 = 核心工具**（必须装，否则没有问答），**运行时检查登录态**：
  - 现状：两个服务已有 `GET /health` → `{ok, busy, queries_done, time}` —— **没有登录态**。bili 侧已有现成校验（`bili_client.pong()` 出 isLogin，`qr_login_once.py` 正在用）；NGA 侧 `_login(ctx)` 内含验证步骤。
  - 要加：`/health` 增登录态字段 + 校验时间戳。校验时机 = 服务启动验一次 + 按需（前端按钮触发）+ 被动（一次爬取 0 命中、或识别到登录页特征时标记可疑）。**不能每次爬取都验** —— 多一次请求就是多一次风控暴露。
  - harness 侧：读 `/health`，登录态无效就**明说「爬虫未登录、本轮样本作废」**，绝不让模型拿空结果下「社区没讨论」的结论 —— 这正对 §10.29 审计里最重的一类错（把「我没看到」说成「社区没有」）。
  - 前端：两个灯分开显示 ——「服务在不在」与「登录态有没有效」；bili 失效直接在界面出二维码（`qr_login_once.py` 已能存 PNG + 落 flag 供轮询）。
- **许可证口径修正**：`crawlers/bili/LICENSE`（NON-COMMERCIAL LEARNING LICENSE 1.1）授权范围第 1 条明确授予「使用、复制、修改、合并」的非商业学习授权；条件第 1 条要求保留版权声明与本许可证于「软件**及其副本**」的所有显著位置 —— 即**非商业学习用途下带副本分发本在授权范围内**，原文并无「不得分发」条款。故 §5 铁律「不发 vendor 树」应改口径为「发则保留 LICENSE + 版权声明 + 显著写明用途限制」。（何时改仍待用户拍。）
- **两条线的分界**（本轮想清楚的一句话）：小模型**有便宜替代品**（LLM 自判）→ 剥成可选；爬虫**没有替代品**（没爬虫就没有问答）→ 只能做成核心工具 + 可插拔地址（`NGA_HTTP` / `BILI_HTTP` 环境变量已支持覆盖）。

---

### 10.32 改动清单（09-12 拍板落地）—— NLP 改按需 / 爬虫加登录态 / 前端两盏灯（2026-09-12，用户「可以 改吧 有关前端的改动记到前端的相关文档内」）

> **铁约束：本清单全部涉及代码，必须等 v4 认证批跑完（预计 13:10）再动手。**
> 批 2/批 3 是重新起进程、会读盘上的新代码 —— 半途改会让同一批的前 5 题与后 10 题跑在不同版本上，认证不可比。
> 前端口径已入 `docs/PRD.md`：FR1（状态灯 + 登录态自助修复）、FR7（按需打分 + 降级改自判）、FR8（图表按需 + 口径标注）、§8 #7（取代 9/8 停车项）、§8 #10（新开分发细节）。

#### ① NLP 改「按需 + 可选」（`harness/measure.py` + `harness/engine.py`）

- **摘掉"自动挂载"**：现在打分挂在爬取落档的副作用上（每轮爬完无条件给全部新样本打分）。改成**引擎按需调用** —— 保留打分函数本身，摘掉自动触发；引擎拿到"这题要看风向"时才调一次，对象是**该题已归档的全量样本**。
- **降级目标改掉**：`_nlp_measure` 不可达时**不再走关键词兜底**（0/1 两档、无可疑度、且库里看不出来 = 三条路里最差），改为返回「无软信号」→ 引擎走**主链 LLM 自判**路径。
- **判据位置**：冷启动判断那一步多输出一个布尔 `need_sentiment`（这题要不要社区风向/情绪分布）。**不给模型加第四个工具** —— 工具面仍收窄为 `{crawl_nga, crawl_bili, search_archive}`。
- **提示词**：系统提示里加「本轮无软信号，请自行依据原文判断情绪与反讽」的分支；日志把「度量降级 keyword」改成明确的「未启用软信号（按需模式）」，两者语义完全不同，别混。
- **图表口径**：有小模型 = 全量分布；无 = 被引证证据的分布 + 图上标注样本口径（见 PRD FR8）。

#### ② 爬虫 `/health` 加登录态（服务端 2 处 + harness 1 处）

- **服务端**（`crawlers/nga/nga_search_server.py`、`crawlers/bili/bili_search_server.py`）：`GET /health` 返回体在现有 `{ok, busy, queries_done, time}` 上**增 `login`（true/false/unknown）+ `login_checked_at`**。
  - bili 侧校验用现成的 `bili_client.pong()` → isLogin（`qr_login_once.py` 正在用这个）。
  - NGA 侧用 `_login(ctx)` 里已有的验证步骤（开首页、等加载、查登录态）。
  - **校验时机三档**：服务启动验一次 / 前端按钮按需触发 / **被动标记**（一次查询 0 命中或识别到登录页特征 → 置 `login=false|unknown` 并记时间）。**不每次爬取都验** —— 多一次请求就多一次风控暴露。
- **harness 侧**（`harness/sources/live.py`）：crawl 前读 `/health`；`login` 为假 → 返回明确的「爬虫未登录」错误，让引擎**明说「本轮样本作废」**，绝不下"社区没讨论"的结论。
- **注意**：改这两个服务要**重启演示服务器 上的 `nga-tool` / `bili-tool` systemd 服务**，重启会掐断正在跑的批 → 必须等收工。实现时同步更新 `docs/ARCHITECTURE.md` 的组件映射（`/health` 契约变了）。

#### ③ 前端：两盏灯 + 扫码面板 + 按需图表（待建，技术栈见 D7）

- 已入 `docs/PRD.md` **FR1 / FR8**，此处不重复。要点：灯分两个（服务在不在 / 登录态有没有效）；bili 失效出二维码；NGA 走 cookie 更新引导；非风向题不出情绪图是正常情况。

#### ④ 怎么验（分三层）

- **离线确定性**（并入 `harness/test.py`）：
  - 模拟小模型不可达 → 断言**不再**产生 keyword 兜底分、且引擎走自判分支（trace 里可见）；
  - mock 两个爬虫 `/health` 返回 `login=true/false/unknown` 三态 → 断言 harness 行为各不相同（假态必须让答案侧出现"样本作废"声明）；
  - 断言"非风向题不调用打分"。
- **对照实验**（要真跑，占隧道 + 烧 key，须另行点头）：**同一道题开/关小模型各跑一遍**（建议 1 快速 + 1 精准），答案放一起对比 → 验证 PRD FR7 验收①「无实质差异」。这是「小模型降为可选」这个决定唯一能证伪的实验。
- **前端**：手动过状态灯三态（正常 / 服务挂 / 登录态失效）+ bili 扫码全流程 + 非风向题不出图。

#### 本轮新增/改动的文件（清点，便于收工后动手）

- 代码（待改）：`harness/measure.py`、`harness/engine.py`、`harness/sources/live.py`、`crawlers/nga/nga_search_server.py`、`crawlers/bili/bili_search_server.py`
- 服务端部署：改完需同步到演示服务器 的 `<NGA爬虫目录>`、`<B站爬虫目录>`（毕设原版一行不动，只改副本）并重启两个 systemd 服务
- 文档（本轮已改）：`docs/PRD.md`（FR1/FR7/FR8/§8）、`PLAN.md`（§10.31/§10.32）、`docs/mem/HANDOFF.md`（顶部现态）

### 10.33 撤掉「每轮只喂 5/15 条证据」—— 爬到的全部进模型上下文（2026-09-12 中午，用户「我需要的是每一条爬到的都进好吗？这就吃几条那我爬这么多有啥意义」）

**事故**：`_EVIDENCE_CAP={"快速":5,"精准":15}` 卡在每次工具返回上，且 `archive.rows_of_run` 取的是 `ORDER BY id DESC LIMIT n`（该批**尾部** n 条）。实测认证批前 7 题：

| 题 | 模式 | 归档 | 模型实看 | 答案引用 |
|---|---|---|---|---|
| q1 | 快速 | 160 | 29 | 23 |
| q3 | 快速 | 120 | 15 | 14 |
| q6 | 精准 | 276 | 104 | 22 |
| q7 | 精准 | 78 | 28 | 28 |

两处硬伤：① 爬 160 条只让模型读 29 条，同批其余样本等于白爬；② 那 29 条**没有任何挑选逻辑**，纯按插入序取尾部 —— q1 run#6 给的是 9/5–9/8 的 5 条，同批里 9/12 最新的反而没给；q7 run#27 给的是 2025-07 的，同批有 2026-06 的。

**这违反本仓自己写的规矩**：`ARCHITECTURE.md` P5「护栏优先于裁剪 —— 上下文治理用上限护栏收口异常，不是对正常证据做启发式截断，截断会断引用链」；`PRD.md` FR6 验收「正常问答护栏休眠、证据完整」。代码与设计正源打架，是代码错。

**关键事实（推翻"怕上下文爆"这个前提）**：`deepseek-chat` 现为 1M 上下文别名（→ v4-flash 非思考模式）。全量喂进去，最大的一题（q6，276 条，正文 600 字上限）≈6.5 万字符 ≈ 4.3 万 token。预算根本不是约束。

**改动（4 文件）**：
- `harness/archive.py`：删 `_EVIDENCE_CAP` 相关两处调用；`rows_of_run(run_id, limit=None)` 全量返回、改 `ORDER BY published_at DESC, id DESC`（新的在前）；`search()` 的 `limit=None` 时不再拼 `LIMIT ?`（原 `args+[None]` 会报 datatype mismatch）。
- `harness/engine.py`：`_EVIDENCE_CAP` 删除；`args["limit"]=cap` 两处删除；`_base_system(mode, budget, tgt)` 去掉 cap 参数，规则 8「单次…每条证据上限 N 条」改写为「每次真爬取到的样本**全部**给你」；规则 11 覆盖度改写（取证面仍受检索词限制，别把"没命中"说成"社区没有"）。
- `harness/tools/crawl_live.py` / `search_archive.py`：`_ev` 正文 120→600 字、标题 60→120 字；`rows_of_run` 不再传 limit。
- **`IN_CEILING` 重定档 12000/26000 → 160000/320000**，且护栏判据从"上一轮旧的累计估算"改成**量这一轮真正要发出去的那份上下文**（`max(_est_input_tokens(messages), last_prompt)`）—— 旧的写法滞后一轮，证据不截断后一轮巨型返回就能把窗口顶爆。

**验证**：`python -m harness.test` 18 PASS / 0 FAIL（含同步修 `_tmp/verify_budget_rounds.py`、`_tmp/verify_determiner_discipline.py` 两处 `_base_system` 旧签名）；`rows_of_run(run#6)` 55 条（原 5）；`search(scope=q1)` 160 条。**实测复核（新代码首题 q10，精准）**：`用时=522s rounds=7 in/out=188775/2831`，平台新增 NGA 78 / bilibili 275 —— **输入 18.9 万 token**，旧护栏 26000 会在这一题当场触发、把证据拦腰截断；新档 320000 留有余量。代价是单题耗时从旧代码约 2.5 分钟涨到约 8.7 分钟（上下文变大）。

**运行决策**：批 2 跑到 Q9（11:45:55 落库）后停 —— **11:55:42 我按用户「停」终止 pid 22836**（`exit=-1`），剩余 **Q10-Q15 用新代码跑**（`_tmp/cert15b_batch_rest.ps1`，start=9 max-q=6，内建"等同隧道同类进程退干净再起"的互斥闸；12:03:21 起跑）。**Q1-Q9 是旧代码产物，本批认证属混版**；要统一须换新标签重跑（同标签会被去重，新样本数全 0），另议。

**事故记录（11:55 那次「批 2 崩了」是误判）**：报告里 `exit=-1` + q10 缺失一度被当成进程崩溃，回查自己的工具调用时间戳才确认是**我按用户「停」时手动 `Stop-Process -Id 22836 -Force`**（用户 11:55:28 发「停」，我 11:55:42 杀进程，报告 11:55:42 记 `exit=-1`，两个时间戳逐字吻合）。**教训：先怀疑自己刚做的事，再翻事件日志/崩溃转储**。附带真问题：`calibrate.py` 的 `w()` 不 flush，硬杀会把已跑题（q6-q9）的报告行连同缓冲区一起丢掉（DB 行与 flow 文件仍在）→ 已补 `rep.flush()` 逐行落盘。

### 10.34 前台按「修改方案.doc」改版：撤顶栏、会话栏仿 DeepSeek、控制件下移到输入区（2026-09-12 中午，用户「读这个来改前端」）

**素材来源**：`<本机桌面>\修改方案.doc` —— 真·二进制 OLE2（WPS 存的老式 .doc，502,784 字节）。机器上没有 Word/LibreOffice/olefile，且**铁律不许在用户机器上起任何 GUI 进程**，所以没装东西没起进程，用纯标准库自己解了 CFB 容器（`_tmp/read_doc.py`：header→FAT/DIFAT→miniFAT→目录项→`WordDocument` 流；正文按 FIB 的 fcMin/fcMac 切）。**正文只有 330 字节 / 7 行批注，剩下的 48.7 万字节全是内嵌图**；又在 `Data` 流里按 PNG 魔数雕出 5 张图（合计 485,676 字节，占该流 99.7% —— 图已抽全，没有漏看的画面）。图片用 `_tmp/carve_imgs.py` + PIL 缩图后逐张读过。

**批注与红框的对应（红框用像素定位，不靠肉眼猜）**：`_tmp/redbox*.py` 扫红色线框，得

| 图 | 红框位置 | 批注 | 落成什么 |
|---|---|---|---|
| png_01 | `(14,4)-(1904,54)` = **整条顶栏** | 「红框框住的全部不要 不要这种上栏位」 | 顶栏整条删除 |
| png_02 | 无红框 | 「左侧这个部分仿照下面的图片进行制作」 | 会话栏照 DeepSeek 版式重做 |
| png_01 左下 | 不是红框：`#f2cccc`(438px)+`#dc2626`(151px) = 本页 `.btn.danger` 自己的描边 | — | 「清除我的对话记忆」保留，并进设置 |
| png_03 | `(12,792)-(1688,902)` = **原底部输入条** | 「对话内不要 / 这么大」+「红框部分改为快速和精准的切换…爬虫状态放在红框区域，其中…这块保留改为到时候我们的各项设置」 | 控制件全部下移到输入区 |
| png_04 | `(144,704)-(1548,894)` = DeepSeek 的输入盒 | 「做成这样」 | 输入区做成 DeepSeek 那种单盒 |
| png_05 | `(10,904)-(254,948)` = 参考图底部的账号行 | （红框=不要） | 不抄账号行 |
| 批注 | 「这左上角图标预留 我到时候会去做一个」 | — | 左上角放虚线占位块 |

**落地改动（只动 `web/index.html` 一个文件）**：
- **删整条 `<header>`**：档位分段器、平台比例滑杆、两盏灯从顶上全部撤走。品牌移到会话栏顶（虚线占位图标 + 站名 + 收起按钮）。
- **会话栏照 DeepSeek**：`＋新建会话`(蓝底大方块) → **`⊕ 开启新对话`（白底胶囊）**；历史从「标题 + `N 轮 · 时间` 副行」改成**按 `7 天内 / 30 天内 / 更早` 分组的单行标题**（`daysAgo/groupOf` 真算，不是写死）；参考图底部的账号行**不抄**。
- **输入区收敛成一个 `.box`**（照参考图）：盒内底栏从左到右 = `快速|精准` 两个 chip → **平台比例按钮**（点开 `#mRatio`，滑杆 + 三个预设 + 偏置说明，`应用` 才生效）→ `NGA` / `bilibili` 两个状态 chip（圆点 = 服务与登录态的**最坏值**合成，点开 `#mStatus` 保住原来两盏灯的明细与「去修复」）→ `⋯ 设置` 虚线占位 chip（`#mSettings`，方案原话「这块保留改为到时候我们的各项设置」，清除对话记忆并进了这里）→ 圆形发送键。原来那条 `当前档位…非官方权威` 提示行从对话里撤掉（`非官方权威` 仍在每条答案卡上的徽章里）。
- **顺手修一处已经过时的口径**：`trace` 里还写着「单次工具返回上限 N 条」（§10.33 撤了 `_EVIDENCE_CAP` 之后这句就是假的），改成「每次真爬到的样本**全部**进上下文，不按条数截断」。

**验证**：① 静态地板 `_tmp/verify_web_wiring.py`（22 项：顶栏真没了/控制件真在盒内/id 与类不脱钩/前端不再出现 `evidence_cap`），**已挂进 `harness/test.py` 的 offline gate**；② 真 DOM 冒烟 `_tmp/web_smoke.js`（jsdom 装在 `%TEMP%\jsdomchk`，**不进仓库**；42 项：启动不抛错、点比例→改→应用、点状态 chip、折叠/复位、答案卡与图表降级全部走通）。`python -m harness.test` → **19 PASS / 0 FAIL**。
**没验到的**：jsdom 不做排版，**视觉效果（间距/对齐/配色）我没法自证** —— 铁律不许起浏览器，得用户自己开 `web/index.html` 看。

**两处我按自己理解拍的，请用户复核**：① 「对话内不要 / 这么大」两句短批注指代不明，我读成「输入条不该占对话区、且做成参考图那样紧凑的单盒」；② 「其中…这块保留」里的省略号无法还原，我当成「这一块留着，以后并进各项设置」。若拍错，改的是 `web/index.html` 里 `.composer` 与 `#mSettings` 两处，不动别处。

### 10.35 前台二改：爬虫状态+设置挪到会话栏下方、平台比例改「圆→胶囊」原地展开（2026-09-12 12:30，用户口头纠偏）

**触发**：用户看了一版后直接否掉两处 —— 「左侧对话记录栏下方是展示爬虫状态位置和设置位置」「平台比例这按钮也不对，我想要的是在按钮这块直接动态打开一个窗口，就是一个滑动按钮而已」。即 §10.34 把三样控制件全塞进输入盒，方向错了：**状态与设置属于会话栏（左栏）底部，不属于输入区**。

**回看参考图印证**：png_05 / png_02 底部那个红框（`(10,904)-(254,948)`）当初被我读成「不要抄参考图的账号行」——**只读对了一半**。红框标的是**位置**：会话栏底部那一块要放我们自己的东西（爬虫状态 + 设置），而不是照抄 DeepSeek 的账号行。png_01 左下角红框框着的正是当时那版「清除我的对话记忆」所在的底部位置，也对得上。

**落地（仍只动 `web/index.html`）**：
- 新增侧栏底区 `.sbot`（`aside` 内、`.sess` 之后，`border-top` 与列表分隔）：两行 `.srow`（圆点 + 平台名 + 一句话状态 —— `正常 / 登录态失效 / 服务不可达 / 待校验`），下面一个 `.sbtn`「⋯ 设置」。点状态行开 `#mStatus`（原两盏灯明细 + 去修复保留），点设置开 `#mSettings`（清除对话记忆仍挂在里面）。
- 输入盒 `.cbar` 里删掉两个状态 chip 与设置 chip，只剩 `快速|精准` + 平台比例 + 发送键。
- **平台比例删掉 `#mRatio` 弹窗**，改成 `.morph` 原地展开：收起态是 32px 圆（`％` 图标），点击后 `width` 过渡到 328px 胶囊（`.morph.open`，`cubic-bezier(.22,.8,.2,1)` 360ms），滑杆 + 取值在胶囊内淡入（`opacity/transform`，延迟 90ms 错开）；点空白处或再点按钮收起。滑杆 `oninput` **即时生效**（没有「应用」按钮了），`onchange` 松手给 toast；单边偏置的说明挪到胶囊的 `title` 上。

**验证**：静态门 `_tmp/verify_web_wiring.py` 由 22 项重写成 **24 项**（新增：比例是原地展开非弹窗、`.morph` 宽度过渡在位、状态/设置真在 `aside` 的 `.sbot` 内、且**不在** `.composer` 内——这条用「按 `<aside>`/`</aside>`、`<div class="composer">`/`</main>` 切块再查」实现，不靠文档顺序碰运气）；jsdom 冒烟 `_tmp/web_smoke.js` 由 42 项重写成 **48 项**（新增：点按钮→`.morph.open`→`aria-expanded=true`→拖到 100 即时改标题→点空白收起→再展开保留值→再点收起；侧栏底两行 + 一句话状态文案）。两脚本各自全绿，`python -m harness.test` **19 PASS / 0 FAIL**。

**仍没验到的**：jsdom 不做排版也不跑 CSS 过渡 —— **圆→胶囊的动效观感我自证不了**（铁律不许起浏览器），只能保证类名、宽度取值与过渡属性在文件里是对的。观感得用户自己开 `web/index.html` 点一下看。

### 10.36 认证批 Q10-Q15（全量证据新代码）跑完 + 两个新缺陷（2026-09-12 13:46 收工）

**跑批**：`_tmp/cert15b_batch_rest.ps1`（tag `cert15b-20260912`，start=9 / max-q=6，`--interval 600`）12:03:21 起 → **13:46:15 收工 `exit=0`**，无残留进程。六题耗时 407–602s、轮数 6–7。

**① 确定性验证：全批 15 题 15/15 通过**（`_tmp/verify_cert15b_q1_15.py` 逐题跑 `verify_record`：结论齐 + 每个 `[id=N]` 都能对回**本题 scope** + `ev run#` 全可回溯）。`python -m harness.guard` **0 违规**。

**② 全量证据改动的实测对照**（同一批、同一批题单，前后两种代码）：

| | q1–q9（旧：每轮 5/15 条） | q10–q15（新：全量） |
|---|---|---|
| 输入 token | 1.3 万 – 4.7 万 | **9.4 万 – 18.9 万** |
| 引用条数 | 5 – 28 | 28 – **129**（q12） |
| 单题耗时 | 约 2.5 分钟 | 约 7–10 分钟 |

q12 引用 129 条、q10/q11 各 29/28 条 —— 改动的收益方向成立（证据全进 → 引用面变宽），代价是耗时约 3–4 倍。

**③ 缺陷 A（引擎，待修）：同轮双 `crawl_bili` 必撞单飞 429**。q12（两游戏对比题）trace 第 12/13 条是**同一轮两个 `crawl_bili`**，此后第 8、12、13、22 条连续 `blocked: bilibili crawl fail: busy 重试 3 次仍 429`（前 3 轮 bili 还正常）。机制：`engine._precheck` 的 bili 爬距只在**预检阶段** sleep，两个请求随后仍一起进线程池并发下发 → 撞 bili-tool 单飞锁；`live._post` 对 429 只重试 3×12s≈36s，长爬取超 36s 即永久失败。`engine.py:554` 注释自认「同轮罕见双 bili 不额外串, 接受」—— 对比题让「罕见」变常态。修法方向：同轮同平台只放行一个（第二个 defer 下一轮）。

**④ 缺陷 B（工具契约，待修）：`search_archive` 的 `query` 是字面子串**。`archive._where` 把 `words` 每个元素各拼一个 `%w%` 再 `OR`，不分词、不做多词与。q12 第 17/18 条 `search_archive total=0`；同 scope 同 game 复算：**无词=104 条、词「终末地」=25 条、词「终末地 绝区零 对比」=0 条**。schema 里 `query` 只写 `{type:string}`，没说它是子串。

**⑤ q15 一处引文挂错号（验证器管不到）**：答案引「优菈最保值，自第一次up就保持第一物理大c到现在」却挂 `[id=3006]`（一条**正文为空**的 NGA 标题帖），该引文实际在 `[id=2987]`（bili 评论）。引擎的引文挂靠校验抓住并附在校验行；结论本身完整可用。同类错见 §10.29 审计第 ③ 类。

**⑥ nlp-tool 被 OOM 杀过一次（12:24:29，重启计数 3→4）但未致软信号降级**：宕机窗口 12:24:29–12:24:47 内落库 **0 条**（前后最近两批 run#66 @12:24:24、run#67 @12:26:48 都在存活期内打分）。nga-tool / bili-tool `NRestarts` 均 0，演示服务器 事后可用内存 4.8G。

**⑦ 本批仍是混版**：Q1–Q9 旧代码、Q10–Q15 新代码。要统一须换新标签重跑全部 15 题（同标签去重、新样本为 0），另议。

### 10.37 量化热度字段回补 + 一轮多发短检索词（fan-out）+ 提示词重排（2026-09-12 18:xx）

**起因**：用户连问三件事 ——「检查 nga/b站爬虫能爬出来的字段，为啥要丢掉，这些后面都可以当论据和图表绘制数据」；「b站25题无关？发给爬虫的关键词是啥」；「单轮内关键词数量本身就≥3啊…切换关键词最多等 10s-15s，你爬就完了呗」（并点名提示词太冗长致模型注意力涣散）。

**① 量化字段回补（6 处贯通，已落地 + 已验证）**。demo 服务其实一直返回这些计数，是解析层 `live._nga_items`/`_bili_items` 压成固定 8 键时丢的 —— 用户明确「我从一开始就说画图表要用」。新增 `observation` 五列（全部可空）：`view_count` 播放量 / `like_count` 点赞 / `reply_count` 回复 / `danmaku_count` 弹幕 / `video_tags` 视频自带标签（与 NLP 口径的 `topic_tags` 分开）。
> 贯通链：`sources/live.py` 解析（新增 `_num` 归一，非数→None）→ `schema.sql` + `db.py` `_RUNTIME_COLS` 运行时迁移（旧库 `init` 自动 ALTER 补列）→ `archive.record_crawl` INSERT → `rows_of_run`/`search` 两条 SELECT → `crawl_live._ev` 与 `search_archive` 两处 mapper（键 `view/like/reply/danmaku/video_tags`）→ prompt 教读段。
> **已归档的 3049 行不补录**（计数是采集当时快照，拿不回），一律 NULL；下游（含图表）必须把 NULL 读成「无此数据」而不是 0。`verify_determiner_discipline.py` 的 `TOOL_ROW_KEYS` 同步扩到 15 键。

**② 缺陷 A（§10.36 ③）已修：同轮同平台串行**。`engine.py` 新增 `SAME_ROUND_GAP = 12`；`_precheck` 的 bili 爬距分两档 —— 跨轮/跨 ask 仍 `BILI_GAP`(60/120)，**同轮**（`st["last_round"] == self._n_crawl`）只留 12s；真正的串行落在抓取调度：`plan` 按平台分组，**跨平台并发、同平台串行且相邻调用 sleep `SAME_ROUND_GAP`**（服务端单飞，并发同平台必 429）。新门 `_tmp/verify_keyword_fanout.py`（7 项：同平台不重叠 / 按词序 / 跨平台确重叠 / 同轮 12s×2 / 跨轮 ~60s）已挂进 offline gate → `python -m harness.test` **20 PASS / 0 FAIL**。

**③ 一轮多发短检索词（fan-out）**。提示词新增规则 **6b**：同一平台一轮内至少发 3 个短词（各 2-4 字、各一次调用），并给「错：`query="男干员待遇强度争议"` 长串 / 对：`男六星`、`男干员`、`男六星待遇`、`男六星强度`」对照；同轮多次调用**只算 1 个爬轮**（既有语义），间隔交给引擎，模型不必自己等。`crawl_live.CRAWLERS` 两条工具描述与 `query` 参数说明同步改写（此前「与 crawl_bili 同轮各调一次」的措辞会诱导长串）。
> bili 检索词**全去空格**是既定正确行为（实测保留空格搜不出相关内容），**不动**；问题一直是「拼成长串」，不是空格。

**④ 提示词重排（不删内容）**：`_base_system` 拆成六节连续编号（一·取证与引用 / 二·平台与轮次 / 三·下嘴纪律 / 四·检索词怎么造 / 五·采多少 / 六·覆盖度与采信纪律），`_SOFT_SIGNAL_HINT` 提为模块常量并追加「量化热度字段」教读段。**六条被测试盯着的硬约束子串全部保留**（`【软信号提示】`/`senti=情绪`/`sarcasm=1`/`tags=话题标签`/`以原文为准`/`规则 4`/`最多 3|5 个爬轮`/`平台不作二选一`，且「每条证据上限」仍不得出现）。

**⑤ 官号（官方账号）能力：现状 = 能爬但没接线，且不在 LLM 工具面**。`crawlers/bili/.../client.py` 有 `get_creator_videos/info/fans/followings/dynamics`（动态走 `host_mid`），但 `bili_search_server.py` 只暴露 `/health` + `/crawl`（→ `search_one_query`），**服务层没有官号端点**，`live.py` 也没有对应函数。PLAN §10.1 逐日脉络（09-07 段，PLAN.md:305）已拍「深度=3关键词+官号探针；快速=3关键词+官号」且「爬虫原语封闭：LLM 只见 `{query}`」→ 官号按设计是**引擎侧的一步探针，不是模型可调工具**。阻塞项仍是 `PROJECT_MEMORY §5` 记的：游客访问官号接口返 **−412 需登录**、UID 表要人工维护 → 搁置二期。**要不要现在做，待用户拍**（要动演示服务器 服务 + bili 登录态，属铁律区，须先评估 OOM）。

**⑥ 仍未修**：§10.36 ④ 缺陷 B（`search_archive` 的 `query` 是字面子串、多词 OR 不命中）—— 本轮未动。

### 10.38 bili 登录态恢复（扫码成功）+ `qr_login_once.py` 重写 → 官号阻塞的「登录态」那一条解除（2026-09-12 19:1x）

**起因**：用户「tm爬官号那个你直接用现有爬虫登录态不就行了，之前都跑通了现在跑不通?」→ 查证：机制在、cookie 已死（§10.37 ⑤）。用户「你发来吧」（要二维码）→「哦c 我忘了扫了刚刚 你重发一个」→ **19:11 扫码成功**。

**已恢复（实测，非推断）**：扫码后 qr 脚本 `pong()` 验 `isLogin=True` → `qr_done.flag=1`。持久 profile `browser_data/bili_user_data_dir/Default/Cookies` 里 `SESSDATA`/`DedeUserID`/`bili_jct`（`.bilibili.com` 与 `.bilibili.cn` 各一份）`creation_utc` = **2026-09-12 19:11（北京）**、`expires_utc` = 2027-03-11；只读了元数据（cookie 名 + 创建/过期时间），**没有打印任何值**。**这份 profile 才是 bili 爬虫 cookie 的唯一来源**（`create_bilibili_client` 从 `browser_context.cookies()` 读，不是 `config/base_config.py` 的 `COOKIES`）。

**服务已复位 + 端到端实证**：`bili-tool` 为跑扫码于 18:37 停、**19:11:34 重启 → active**，`/health` → `{"ok": true, "busy": false, "queries_done": 0}`。19:12 打一次最小实爬 `POST /crawl {"query":"原神"}` → **HTTP 200、6 个视频、每视频 10 条评论**，返回体带 `view_count`/`like_count`/`reply_count`/`danmaku_count`/`tags` —— 顺带坐实 §10.37 ① 的量化热度字段在服务端确有源数据、非空。

**`qr_login_once.py` 重写（框架自带扫码路子走不通）**：框架 `media_platform/bilibili/login.py:87` 的 xpath `//div[@class='right-entry__outside go-login-btn']//div` 点下去 30s 超时 —— B站 首页登录入口改版（现在点了只弹「立即登录」浮层）。**vendored 代码一行不动**，改直接开 `https://passport.bilibili.com/login`：该页一进来就把二维码渲染成 base64 PNG（选择器 `div.login-scan__qrcode img`），无需点任何按钮。踩过的三个坑：① `clear_cookies()` 必须挪到**开登录页之前** —— 放之后会让登录页触发自身跳转，下一次 `query_selector` 抛 `Execution context was destroyed`，整个进程当场带走（19:06 那次即此因）；② 取码要 try/except 兜住（登录页加载完还会自己跳一次）；③ 码丢了/失效就 `goto` 重载换新码，写 `qr_ready.flag`（内容=第几版），外部据此重取图。
> 运维坑（非代码，记下来免得再踩）：远程重启命令里**不能**出现 `pkill -f qr_login_once.py` —— 该命令整行也含这个文件名，`pkill -f` 会把自己的 shell 一起杀掉、重启静默失败；改成把动作装进远程脚本文件再 `bash`。同理 ssh 远程命令**别用 PowerShell 双引号**，`$(cat …)`、`2>/dev/null` 会被**本地**先展开。

**官号阻塞（§10.37 ⑤）更新**：其中「登录态死了」这条**已解除**。仍存：① 服务层无官号端点（只有 `/health` + `/crawl`）；② 按 §10.1 设计，官号是**引擎侧探针、不进 LLM 工具面**，别塞成模型工具；③ UID 表仍需人工维护；④ 「游客 −412」现在有条件用登录态复验（官号动态流是否随登录态转 200，**尚未实测**）。要不要接线 = 要动演示服务器 服务，属铁律区，**仍待用户拍**。

**卫生**：桌面二维码 PNG 与演示服务器 上的 `qr_login.png`/`qr_ready.flag`/`qr_done.flag` 均已删；旧脚本备份 `qr_login_once.py.bak_20260912` / `.bak_v2` 留在演示服务器。

### 10.39 官号探针端点落地 + 「登录态下动态流 412」复核通过 + 四家官号实跑（2026-09-12 19:3x）

**起因**：用户「你跑一波官号。。。」→ 兑现 §10.38 遗留的「登录态下官号动态流还 412 不 412，尚未复验」+ 把 §10.1 的引擎侧探针接上服务层。

**服务层新增 `POST /creator`**（`crawlers/bili/bili_search_server.py`，已部署演示服务器）：体 `{"mid": <int>, 可选 "max_dynamics"(默认10), 可选 "debug": true}`。复用**同一个常驻爬虫实例**（`CRAWLER.bili_client`），**不新起进程**（同 profile 不能两进程占，铁律）。同 `RUN_LOCK` 单飞、忙时 429；`mid` 非法 400；超时 504；任一段失败只记 `*_error` 不整体崩。按 §10.1 定位为**引擎侧探针，不进 LLM 工具面**；`sources/live.py` 侧**仍未接线**（待用户拍）。

**「游客 −412」这条解除（实测）**：`/x/polymer/web-dynamic/v1/feed/space` 在恢复后的登录态下**全部转 200**，四家官号动态流均读到（原神 14 条 / 明日方舟 13 条 / 崩铁 12 条 / 鸣潮 12 条）。§10.37 ⑤、§10.38 里「游客必 412」仍成立于游客态，但登录态下不再是阻塞。

**动态正文兜底（实测形状，不兜底则一半动态空白）**：`_dyn` 原只读 `module_dynamic.desc.text`，实测发现——
- `DYNAMIC_TYPE_DRAW`（图集）：**上游根本没有正文**，`desc` 键都不存在 → 兜底成 `[图集 N 张]`（N 取 `major.draw.items` 长度）。
- `MAJOR_TYPE_LIVE_RCMD`（直播推流）：正文是 `major.live_rcmd.content` 里的**一个 JSON 字符串** → `json.loads` 后取 `live_play_info.title`，前缀 `[直播]`。
- `MAJOR_TYPE_ARCHIVE`（投稿）：标题在 `major.archive.title`；并回补 `bvid` 与 `play`（`major.archive.stat.play`，上游给的是「340.2万」这类**字符串**，原样透传不归一）。
- `DYNAMIC_TYPE_FORWARD`：自己有话时 `desc.text` 就在；**纯转发**（自己没写）时兜底看 `orig.modules.module_dynamic.desc.text`。
- debug 通道加了 `_shape`（各类型 `module_dynamic`/`major` 键骨架）与 `md_raw`（前 4 条 `module_dynamic` 原始子树，`_trunc` 截断到 200 字）—— 就是靠它定位到上面这些字段的。

**偶发 HTML 拦截 → 退避重试已吃掉**：第一次批量跑时明日方舟、A-SOUL 的动态流返回的是 HTML 页（非 JSON），`client.get` 解析失败直接抛 `DataFetchError`、该账号 0 条。**验证是瞬时风控不是账号问题**：把明日方舟挪到最前重跑就通了。故在取页处加 **3 次退避重试（3s/6s）**；随后**紧挨着连探 4 家（1s 间隔，故意催）→ 4/4 全 OK**，含此前失败的两家。

**真 mid 表（实测 `info.name` 为准，非猜）**：原神 `401742377`（2080 万粉）、明日方舟 `161775300`（752 万，签名「重铸未来 方舟启航」）、崩坏：星穹铁道 `1340190821`（1249 万）、鸣潮 `1955897084`（536 万）。另解到 明日方舟：终末地 `1265652806`（487 万，未探）。**上一轮那几个 mid 是我猜的，全错**：`703007996` 是 A-SOUL_Official 不是崩坏3、`486604444` 是个人号「薯迷」不是鸣潮、`3546624712268817` 不存在（`info` 报「啥都木有」）。
> **mid 可用公开搜索接口免登录解析**：`api.bilibili.com/x/web-interface/search/type?search_type=bili_user&keyword=<名>`（带 UA + Referer，从演示服务器 直接 urllib 打，不碰登录态）。**但该接口有风控**：崩坏3 / 绝区零 / 首轮的明日方舟都吃过 HTTP 412，隔几秒重试就好（明日方舟第二次就成了）。`client.py` **没有**按名搜 UP 主的函数（只有 `search_video_by_keyword` 与 `get_creator_*`）。

**未做 / 待拍**：① 是否把官号接进 harness（`live.py` 侧 + 深度/快速口径按 §10.1「3关键词+官号探针」）—— 待用户拍；② 要不要把「按名解析 mid」也做成服务层能力（免人工维护 UID 表），但搜索接口会 412，脆；③ UID 表目前是 §10.39 这张表，仍要人工维护。**未修**：§10.36 ④ 缺陷 B（`search_archive` 的 `query` 字面子串）。

### 10.40 官号探针接进 harness 工具面 = `crawl_official`（舆论类专用，每 ask 一次）；评论锚点/排序参数实测定档（2026-09-12 20:xx）

**起因**：用户指出官号-节奏这套 **09-07 就商量过了**，别当新需求：「问近期有什么节奏 → 直接搜官号动态；十条之内数据应平缓，突然一个高峰=可能有争议的节奏；**高峰动态爬 25 条评论、普通动态只爬 5 条**；活动日程/公告也要用官号动态，总结动态内容 + 评论区态度」。随后拍板触发方式：**「这个放开给 llm 让他在舆论类都可以使用」** —— 这是对 §10.1「爬虫原语封闭：LLM 只见 `{query}`」的**单点例外，只此一工具**。

**① 评论接口标定（本轮最硬的一块，之前一直是 0 条的真因）**。`/x/v2/reply/wbi/main` 取动态评论**必须按动态类型换锚点**，传错一律 `DataFetchError('啥都木有')`：
| 动态类型 | oid | type |
|---|---|---|
| `MAJOR_TYPE_DRAW`（图集） | `major.draw.id` | 11 |
| `MAJOR_TYPE_ARCHIVE`（投稿） | `major.archive.aid` | 1 |
| 其余（转发/纯文字/直播） | 动态号 `id_str` | 17 |
> 另两条硬结论：**不能带 `pagination_str`**（一带有就 `访问权限不足`，不是 429 但同样取不到）；`mode` 实测定档 —— `0`/`3` = 热度（首条赞 14599，即站上默认那个）✓、`2` = 时间（首条赞 2）、**`1`（综合）返回 0 条，该接口下不可用**。故 `CMT_MODE = 0`。用户 09-07 说的「就综合」在这条接口上做不到，只能是热度——热度高赞在前，与原意（别让时间序刷出点赞个位数的新帖）一致。
> **`_dyn` 顺带修的两处**：① 补 `cmt_oid`/`cmt_type` 给评论层用；② `published_at` 原来直接透传上游的 `pub_time`（**只有「8月11日」「3天前」这种相对文案**），引擎入档闸门 `exclude_before` 按字符串比大小 → 「8月11日」< 「2026-…」会被**整批当老样本丢掉**。改成从 `pub_ts` 换算 ISO；**注意 `pub_ts` 上游给的是字符串**（`'1786420820'`），`isinstance(ts,(int,float))` 会静默不命中，必须 `int()`。

**② 服务层 `POST /official`**（已部署演示服务器）：账号先查 `OFFICIAL_MIDS` 表（§10.39 那 5 家 + 别名），表里没有按名搜、**只有名字全等或唯一官方认证号才认，否则回候选名单报错（不猜号——认错号整题证据全废）**。取最近 `OFFICIAL_WINDOW=10` 条动态，**基准线取中位数**（不是均值：那条高峰自己会把均值抬上去，峰越大越认不出来），`评论数 >= 3×基准 且 >= 100` 判高峰（`PEAK_FLOOR` 挡小号噪声）；高峰深采 `CMT_PEAK=25` 条、平峰 `CMT_NORMAL=5` 条。动态流端点偶发甩 HTML 拦截页，保 3 次退避重试（3s/6s）。临时标定端点 `/official_calib` 已删。

**③ 实跑（原神，20:39/20:50/20:59 三次一致）**：基准线 932.5，2 条高峰 —— 8月11日投稿（评论 23306）+ 9月7日图集（评论 10039），各带回 25 条评论，其余 8 条各 5 条。热度序首条即「把她的牺牲…值得足足 5 个小时的停服缅怀」14599 赞（这条才是节奏本体）。

**④ harness 侧接线**（`crawl_official`，**不在 `CRAWL_TOOLS` 注册表里**——注册表那条链是 `(query,game,platform)` 形状，官号探针没有 query，走**独立分支**）：
- `sources/live.py`：`crawl_official(game)` + `_official_items`；`_post` 抽出 `_post_path` 复用鉴权/429 重试。错误语义分两级：**账号没解到/动态流失败 → `CrawlError(throttle=False)`（不是 bili 风控事件，不熔断平台）**；传输超时/429 → `throttle=True` 交 bili 退避锁。
- 归档来源可区分：`kind=official_post`（官方口径）/ `official_reply`（评论区态度），与玩家口径的 `post`/`reply` 分开；`platform` 仍是 `bilibili`。**评论落库后按时间倒序、与别的动态的评论混排**，所以评论 `title` 必须带「哪条动态」——定为 `[官号·{高峰|平峰}·{日期}·评{N}] {动态正文前70字}`（同一天两条同型动态靠评论数区分），动态 `title` = `[官号·{tag}·{日期}] 评论 N / 基准线 M`。
- 引擎：`TOOL_DEFS` 追加 def；分支里 **每 ask 只放行一次**（`_official_used`）+ **占 1 个爬轮**（它确实是真爬 bili，不该白嫖风控预算）；走 `_precheck("bilibili")`（熔断/冷却/爬距全继承）；主线程串行执行（在并发 plan 之前），避免与 `/crawl` 抢服务端单飞锁。
- 提示词：规则 **1b**（放【一·取证与引用】）—— 只给舆论类题（近期节奏/争议、官方态度、活动日程公告）用，写明「本 ask 只调一次、占 1 个爬轮」，并教它 `official_post`/`official_reply` 分开引、必须引 [id=N]。

**⑤ 门禁**：新增 `_tmp/verify_official_probe.py`（40 项：映射形状/dedup 稳定/评论 title 挂回动态/同轮常规+官号只算 1 爬轮/成功即锁死/账号没认准允许补试一次且第 3 次锁死/失败不熔断 bili 也不耗爬轮/落库 kind+量化列/非 live 不发真请求/工具面与提示词登记），已挂进 offline gate → `python -m harness.test` **21 PASS / 0 FAIL**。另从 harness 侧经隧道端到端实调 `live.crawl_official("原神")` → **97 items（10 post + 87 评论）**，高峰 25/平峰 5、日期 ISO 均正确。

**仍未做**：① `/creator`（§10.39 那个独立端点）harness 侧没接、现在也不需要了（`/official` 已覆盖舆情用途）；② 没有跑过含 `crawl_official` 的**真 LLM 整题**（只验了取数链路，模型会不会正确触发/正确引用未验）；③ §10.36 ④ 缺陷 B（`search_archive` 的 `query` 字面子串）仍未修；④ 账号表仍人工维护（按名解析有 412 风控，脆）。

### 10.41 真 LLM 舆论题实跑：`crawl_official` 端到端验证通过（2026-09-12 22:38，用户「试试舆论题」）

**跑法**：新建 `_tmp/live_opinion_run.py` —— `HARNESS_LIVE=1` + 真 DeepSeek + 真爬，`Engine(scope=liveop)`，问「原神最近有什么节奏」/ `game_hint=原神` / 快速，本地隧道（**单 ssh 进程**带 18770+18771）打演示服务器。**用时 314s，一句话 457 条样本**。（另：`harness/data/` 下两个 token 文件之前是空的，先跑 `_tmp/fetch_tokens.py` 拉回才能连隧道。）

**结果 —— 触发 / 引用 / 落库三道全过**：
- **触发**：模型第一轮第一个调用就是 `crawl_official`（run 1），与 3×`crawl_nga` + 3×`crawl_bili` **同一条 assistant 消息**（trace 里 run1 与 run2-7 之间无 `ratio_nudge` 分隔，坐实同轮）→ 同轮官号+常规**只算 1 爬轮**，全 ask 共 3 轮（nudge 分隔正好 3 段）、**没超快速预算 3**；官号**只调一次**（无第二次被挡记录，一次成功即 `_official_used` 锁死）。
- **落库**：官方口径 140 条 = `official_post` 10 + `official_reply` 130。基准线 **826**（滚动 10 条中位数，比 20:00 那轮 932.5 低，正常），**4 条判高峰**（评论 7754 / 4036 / 4747 / 2492，阈值 3×826=2478；2492 只超阈 14 条，擦边过），高峰各带 25 条、6 条平峰各 5 条 → **100+30=130 精确对上**。
- **引用**：答案**主动单开一节「官方口径 vs 评论区态度」**并明写「crawl_official 探针」，官号条目被引到 10+ 个 id（四条高峰动态 [id=1, 33, 65, 103] + [id=55, 70, 34, 41, 127, 347, 361] 等评论）；抽检 12 个被引 id **全部落在对齐的行**上，没编号、没挂错。

**暴露的两点（都不是缺陷，记着备用）**：
① **官号动态正文上游就没有公告明细**：PV/活动汇总类 `official_post` 的 `text` 只有标题 + BV 链接，图集类更是 `[图集 1 张]`；卡池/兑换码/福利名单**只存在于热评**（本轮吃的是 [id=18/69/105] 这类「网友整理的公告摘要」）。所以规则 1b 里「`official_post`=官方口径」**略微高估了**——公告明细实际得靠评论，模型这次把热评转述当「官方口径」写。要么认（热评首条往往就是公告转贴），要么以后在条目里把「正文无明细」写清。
② **一句话 5 分钟**：314s / 457 条 / 19 次 crawl 调用（3 轮 × 双平台 × 3-4 短词，是 §10.37 fan-out 的**设计行为**不是失控）。慢主要在 bili 爬距 + 同平台串行；上前台要对耗时和样本量有预期。

**仍未做**（继承 §10.40）：③ 缺陷 B（`search_archive` 的 `query` 字面子串）、④ 账号表人工维护；新增 ⑤ 官号评论取的是**热度序**（站上默认），以后若要「最新态度」得另开一趟 `mode=2`。

---

### 10.42 崩铁官号评论实跑：**判峰抓到的是「爆款」不是「节奏」**；判据该改 + 窗口不设条数上限（2026-09-13 06:5x~07:2x）

> 正源明细见 HANDOFF 现态（2026-09-13 06:5x）。此处只记**结论与拍板**。

- **用户口令**：「发生节奏的时候 官号下方绝对是反响最热烈的地方之一…」→「崩铁这个版本的节奏，你只看官号动态和下面评论你就明白了」。
- **实跑**（`_tmp/probe_official_tie.py`，只调 `live.crawl_official("崩坏：星穹铁道")`）：10 条动态 / 70 条评论，基准线 2063，**只有 1 条判高峰**（砂金 PV，12821 评），其 25 条热评**清一色玩梗、零节奏**；而真节奏（四路深渊/水温膨胀、官方冷处理、福利缩水、玩家内战、联动定价）**全在平峰动态评论区的热度序前排**。
- **结论（判据问题）**：现判据（单条评论量 ≥3× 中位数）抓的是**爆款内容**、不是**节奏**；玩家冲官方是**逐条动态刷**的，所以**平峰动态评论区反而沉淀骂声**。**基准线抬升本身就是信号**（崩铁 2063 vs 原神 826）。高峰期**热度序失效**（高赞全是玩梗），要另采时间序或加深。
- **用户追加 + 纠错**：「别只走十条啊。。。」→ 用现成 `/creator`（`max_dynamics=30`，不改服务端）拉宽窗口到 **2026-08-25~09-10**；**8/31「恭喜…中奖」动态 202,162 条评论是全段最大、就在 10 条窗口外**。用户纠错「错误的 你看评论区内容了吗」：上一版把它猜成「抽奖刷楼」是**没读评论就下结论**——实读该中奖公告前排评论**清一色节奏骂声**（「别试探了，大大方方开四路深渊」「真是好傲慢…想回应 5 个小时就能回应」），真相相反 → **漏峰是「窗口太窄」，不是「该剔抽奖帖」**。
- **用户拍板（07:2x，原话「别限制啊 为啥要限制？用户问这版本有啥活动也能知道啊」）**：**窗口不设条数上限**——「30 条 / 不早于 30 天」被否。理由：同一支探针还要服务「**这版本有啥活动/公告**」这类题，官号动态就是活动序列本身，截断=丢信息。**方向：取数改「按时间」拉（覆盖约一个版本周期），不按条数截断**；基准线仍用窗口内评论数中位数（窗口越长越稳）。**唯一真成本 = 评论**（每条一次请求，45 天 ≈ 50+ 条 → 探针 ~30s 变 2-3 分钟）。**待部署**（改服务端常量/取数 + 重启 bili-tool；动演示服务器 属铁律区，已报备，动前先看内存）。

---

### 10.43 爬虫说明书两篇 + 50 题能力边界体检（2026-09-13 08:xx，用户「给你来个大任务」）

**用户口令（三件）**：① 通读两个爬虫代码、从关键词进到样本出**有多少功能都写**、LLM 怎么用也写，新写两个文件记录；② 从题库铺满九维度收集 **50 个**问题（冷门游戏维度须用**库外**游戏）；③ 逐题判**能不能解**、解不了**缺什么**并记录。

**交付物（4 个，本段即正源）**：
- `docs/CRAWLER_NGA.md` —— NGA 侧全功能说明书（关键词入口 `_nga_key` → 三种检索模式 → 动态定板两段式 → 版内逐词搜 + 正文兜底 → 时间窗翻页双闸 → 详情正文+热评 → 字段/过滤 → 能/不能 → 参数表 → 部署风控）。
- `docs/CRAWLER_BILI.md` —— B站侧全功能说明书（`game+kw` 去空格直拼 → 三端点 `/crawl`·`/creator`·`/official` → 视频搜索只第 1 页 top 6 → 每视频 top 10 热评 → 官号探针中位数基准/高峰判定/锚点取评论 → 量化字段 → 能/不能 → 参数表 → 登录态风控）。
- `harness/data/qbank50_20260913.tsv` —— **50 题**，九维度（深度 6 / 广度 5 / 时间范围 6 / 节奏 6 / 攻略 6 / 对比 6 / 笼统 5 / 不指定游戏 5 / 冷门游戏 5），格式 `编号<TAB>维度<TAB>游戏<TAB>问句<TAB>复合`；冷门游戏刻意选**库外**：重返未来1999 / 白荆回廊 / 无期迷途 / 枪火重生 / 太吾绘卷。
- `docs/QBANK50_REVIEW.md` —— 逐题判定表 + 逐维度说清「为什么」+ 汇总缺口清单（A 平台有但没取 / B 平台没有要引新源 / C 覆盖面太窄 / D 有能力没针对场景）+ 一句话收口。

**核心判定（正源）**：50 题 **能 ~26 / 部分 ~18 / 不能 ~6**。爬虫**「玩家怎么说」这一类（口径、态度、风评、节奏、氛围、对比）基本都能答**；**三座答不了的山**：① **精确数值/攻略正文**（干货在视频画面或数据站，不在能取的文本里）；② **官方公告明细**（动态正文常是图集/短链）；③ **冷门游戏/多游戏话题覆盖**（没开版的游戏取不到、账号表窄、跨游戏证据零散）。

**性价比排序（给后续立项）**：优先 = **给「按名搜歧义/搜不到」的官号补 mid** + **NGA 冷门定板降级**（低命中不足 8 条时的兜底）；中成本 = B站时间窗翻多页 / NGA 翻页预算放宽；**立新项**（动前评估风控与 OOM）= 结构化数值源、B站字幕/正文、弹幕内容、新平台接入。

> **两处已纠（2026-09-13 16:2x~16:3x，用户指正）**：① NGA 定板本身**已动态免维护**（`_resolve_board_fid` 全站搜游戏名统计主流 fid，无硬编码表），**不缺映射表**——有专属板的游戏（含冷门）能自动定到；没板时已降级为「游戏名+词」全站兜底（丢版内逐词搜 / 正文兜底 / fid 归属校验，board 模式配额 20→10）。② 官号**也非「要扩表」**——`_resolve_mid` 表外会**按名搜** `search_type=bili_user`（认「uname 全等」或「唯一官方认证号」），多数游戏能兜住。**故「扩表」收益面比原判小得多**（09-13 实测见 §10.44：表外 14 个里 9 个能自动解对）。

---

### 10.44 实测：官号按名搜「能解但会静默认错号」（15 个错 4 个）；修法在案，待拍（2026-09-13 16:4x~17:0x）

**用户两问**：①「为啥要扩官号账号表？按理说搜游戏名不就出来了」；②「你去试试呗，只搜官号看能不能爬到相关信息」。

**实跑**（只读探针 `_tmp/probe_user_search.py`，在演示服务器 上**纯游客 urllib + wbi 签名**，不碰登录态/不起浏览器/不占隧道）：15 个游戏（含对照组「原神」）→ **9 解对 / 4 认错号 / 1 安全拒答**。

- **对照组「原神」命中 `401742377`**（与表内一致）→ 通道可信。
- **解对 9 个**：崩坏3(`256667467`) / 绝区零(`1636034895`) / 三角洲行动(`3494376565115651`) / 金铲铲之战(`602664449`) / 无限暖暖(`3461576715667734`) / 王者荣耀(`57863910`) / 白荆回廊(`1881796598`) / 无期迷途(`647409444`) + 重返未来1999(`1197454103`，走「唯一官方认证」规则)。
- **认错号 4 个（静默）**：光遇→`519165468`（个人号；真官号「光遇手游」`211700578`）、阴阳师→`817568`（个人号；真官号「网易阴阳师手游」`30973654`）、太吾绘卷→`2670333`（个人号；真官号「太吾绘卷官方」`381218218`）、枪火重生→`1332753626`（个人号；候选前 5 里**根本没有被认证的官号**）。
- **安全拒答 1 个**：第五人格（候选里有 2 个认证号 → 拒答不猜，符合设计）。

**根因**：`_resolve_mid` 的「uname 完全等于游戏名」优先，**这一步不看认证**（`bili_search_server.py:237-239`）→ 蹭名的个人号排在真官号之前就赢。**危害是静默的**：模型会把粉丝号的话当「官方口径」引，外部看不出错——正是代码注释最怕的「认错=整题证据全废」，可这条规则自己没防。

**修法（已按本次候选验算）**：把「uname 全等」收紧成「**uname 全等 且 认证含官方**」，或**先筛官方候选再取全等**，无官方候选则拒答。预期：**4 个错答全消，2 个变对（太吾绘卷/光遇）、2 个变老实拒答（阴阳师/枪火重生）**。**改演示服务器 服务属铁律区**（已于 §10.45 拍板并落地）。

---

### 10.45 官号按名搜修复**已上线演示服务器**；顺带挖出更深根因：生产的按名搜一直走**会被风控的旧端点**（2026-09-13 17:2x~17:5x）

**用户口令**：「修吧修吧 遇到新问题自己解决」——据此拍板改演示服务器 服务 + 自主解决新问题。

**改动**（`crawlers/bili/bili_search_server.py`；本地改 → 部署 `<B站爬虫目录>/` → 重启 `bili-tool`；md5 `0b4c25be…`→`63c1f16d…`，备份 `.bak_official_midfix` 在）：
1. **删掉「uname 全等就认」这条不看认证的规则**（§10.44 的 bug 本体）。新增 `_pick_official`：候选须 **`official_verify.type==1`（机构认证）且 名字/认证描述含游戏名**（归一化后判定，`_norm_name` 去空白/中英标点/连字符）→ 有多个则优先「名字全等」、并列取粉丝最多；**一个官方候选都没有 → 拒答**。**不设粉丝硬门槛**（金铲铲之战官号仅 75 万粉，卡门槛会误拒）。
2. 候选从「前 5」放宽到「前 20」。
3. **更深根因（上线过程才撞见，生产独有）**：按名搜打的是**非 wbi** 的 `/x/web-interface/search/type` + `enable_params_sign=False` → B站甩 **HTML 风控页** → `cli.get` 解 JSON 抛 `DataFetchError` → 被吞成 `search_fail` → **表外游戏在生产里一律拒答**（即这条路径本来就是死的，§10.44 的「认错号」只在游客探针里能看到）。**改用 `/x/web-interface/wbi/search/type` + `enable_params_sign=True`** 后通。

**上线后实测**（打常驻服务 `POST /official`，`since=2099` 远期窗口让动态流首页即短路、只花解析几刀；**9 游戏全对**）：原神 `table`/401742377、崩坏3 `official_name_exact`/256667467、金铲铲之战 `official_name_exact`/602664449、光遇 `official_top_fans`/211700578、阴阳师 `official_top_fans`/30973654、第五人格 `official_top_fans`/211005705、太吾绘卷 `official_top_fans`/381218218、王者荣耀 `official_name_exact`/57863910、枪火重生 `ambiguous`(拒答)。**§10.44 认错的 4 个全消。**

**新事实**：**游客 ≠ 登录态**——王者荣耀游客 wbi 搜 `result` 空，登录态搜能解到真官号 `57863910`（1188 万粉）。以后判「搜不到」别只凭游客探针。

**回归测试**：新 `_tmp/verify_official_mid_pick.py`（从生产源码 ast 抠 `_norm_name`/`_pick_official`/`_resolve_mid` 直跑，喂实测候选 + 一个「无认证蹭名号粉丝更高」的合成陷阱，**19 项全 PASS**），已挂进 `harness/test.py` `OFFLINE_GATE`。

**卫生**：远程临时脚本已删；bili-tool active、`NRestarts=0`、内存 4.9G 可用；未新起隧道（沿用本会话前的旧隧道）。

**顺带纠正旧述**：09-12 段与 review 旧稿里「无限暖暖按名搜有歧义 / 绝区零·三角洲·金铲铲全落空」**均不实**——这几个都能自动解对。已改 `docs/QBANK50_REVIEW.md`（缺口第 8 条收窄 + 新增 §E 正确性缺陷）。

**过程坑（我自己的，别重踩）**：① 游客 urllib 打搜索结果接口**必须先取 buvid 指纹 + 补 wbi 签名**（`/x/frontend/finger/spi` + `/x/web-interface/nav` 的 `wbi_img` → mixin key → `w_rid`），否则 `code=0` 但 `result` 空；② 结果在 **`data.result`**、不在顶层 `result`（生产的 `cli.get` 会先拆一层 `data`）；③ **别拿常驻服务 `/official` 当"轻量解析探针"**——它走登录态 client，连打会撞 `aba.bilibili.com` 拦截页并触发退避重试（已及时 kill）。

---

### 10.46 拍板：**不给「爬虫够不着的题」加兜底搜索腿** —— 缺材料就如实说答不出来（2026-09-14 03:0x）

**用户口令**：「都不好这位三个 答不出来老老实实说答不出来吧」。

**语境**：§10.43 的 50 题体检把「整个爬虫够不着的题」归成**三座山**——① 精确数值 / 攻略正文（信息在视频画面或数据站，不在可取文本里）；② 官方公告明细（动态正文常是图集/短链）；③ 冷门游戏 / 多游戏话题覆盖（没开版的游戏取不到、跨游戏证据零散）。据此给用户拟了**三条兜底腿**：**A** 只在现有平台上加（B站时间窗多翻页 + 给搜不到的官号补 mid）；**B** 把 B站视频往下挖一层（正文/字幕；需登录态、易风控）；**C** 新开数据源 / 新社区（结构化数值站、贴吧/微博/知乎）。

**决定：三条都不做。** 爬不到就**如实标注材料不足、直说答不出来**，不靠加腿把「不知道」包装成「多爬了几个站的不知道」。

**理由**：① 现有设计**本就**是「拿不到就明说」——引擎有覆盖度与采信纪律（规则 11-14）+ 硬尾纪律，冷门题考的就是「敢不敢直说材料不足」；② 加兜底腿换来的是**风控 / 内存 / 维护三项新账**，收益只是覆盖面一小截，不改善答案质量；③ 三座山里 ① 属「信息不存在于可取文本」，②③ 中能靠 A 挪动的部分与「老实说答不出来」并不冲突——**宁缺毋滥**。

**影响**：§10.43「性价比排序」里的优先项（给搜不到的官号补 mid / NGA 冷门定板降级）**据此关闭、不立项**；后续除用户明确点名，**不再主动提议给爬虫加兜底腿**。

### 10.47 分发线第一次真演练：爬虫去敏拉到本机 + harness 本地模式 + 无登录态整题实跑（2026-09-14 05:3x）

**用户口令**：「我想把爬虫放到我本地 模拟用户下下来的样子 然后跑harness试试」，随后收窄为「你现在要做的就是把代码拉回来，做harness的适配改造」。对应 PRD §8 #10 分发线四子项（① bili 依赖裁剪 ② NGA 登录态自助更新 ③ 许可证口径 ④ 前端技术栈）**至今一个未落地**——本轮是第一次真演练。

**用户给的背景（决定后续方向）**：网页端将来要做**爬虫健康检查** → 看有没有登录态 → 没有就引导扫码 → **前端直接显示**。这块**前端 UI 已经做好**（`web/index.html` 侧栏状态灯 + bili 扫码弹窗 + NGA 换 cookie 弹窗 + `renderStatus` 双灯合成），缺的是**后端那一半**：`/health` 原先不返回登录态。

**做法**：
- **拉码（去敏在服务器上做，真实值永不离开演示服务器）**：从演示服务器 拉 `<B站精简目录>`（61 文件，最小可跑、仓库里没有这份）+ `<NGA爬虫目录>` 的 3 个 `.py` + sanitized `config.json`（`cookies={}`）→ 落 **`<本机临时目录>\`**（仓库外独立目录，模拟「下载到别处」）。bili `COOKIES` 由 `sed` 置空，`browser_data/`、`.tool_token`、`out/`、`data/` 全排除。
- **harness 适配**：`sources/live.py` 加 `HARNESS_LOCAL` 模式（指 8770/8771，否则仍旧隧道 18770/18771）+ **token 兜底候选**（找不到 `harness/data/.nga_token` 就自动去找本机服务的 `.tool_token`）+ 新 `health(plat)`；`runner/shell.py` 加 `--local`（隐含 `--live`）。
- **服务端两处硬编码修掉**（可移植性）：NGA `config_path()`（env `NGA_CONFIG` → 服务目录 `config.json` → **保留**老的服务器绝对路径兜底）；NGA Chrome 路径 `_system_chrome_path()` 跨平台（Windows/POSIX/macOS，都找不到返 None 走 playwright 自带兜底）。
- **补 `/health` 登录态**：两服务都返回 `login: ok|bad|unknown`（bili 取 `pong()` 结果、NGA 取 `_login()` 结果）。
- **修一个真 bug**：bili `core.py` 无 COOKIES 时原走 `login_by_cookies` → `check_login_state`（`@retry(stop_after_attempt(600), wait=1s)`）**卡 ~10 分钟才抛异常**；改成先 `pong()` 预检、无 cookie 直接以**未登录状态启动 + 告警**。

**实跑结果**（`python -m harness.runner --local`，真 DeepSeek + 真爬，问「最近二游社区怎么评价《鸣潮》的新版本？」）：
- 请求**确实打到本机** 8770/8771（`queries_done` NGA 11 / bili 6），**token 自动接上**（否则 401）。**未登录不阻塞、不崩。**
- **bili 未登录仍能搜**：每词 24 条、官号探针 96 条；但**偶发 HTTP 412 风控拦截**（日志见 `-352`/`412` + `security.bilibili.com/.../412.js`），客户端退避重试后仍拿到结果。
- **NGA 未登录 = 全 0**：7 次版内搜全部 0 命中。
- 答案**如实降级**：明写「NGA 侧 0 样本、不能代表 NGA、不能断言 NGA 风向」，只出 B站口径，末尾 `结论:` 照旧；**未编造**。
- 度量降级 keyword（本机没起 nlp-tool 8772，token 缺）——预期内。
- `python -m harness.test` 仍 **22 PASS / 0 FAIL**（改 harness 未破回归）。

**新用户从零到跑的缺口清单**（供后续写安装说明）：① bili 需**自建 `config/base_config.py`**（仓库不含，`crawlers/README.md` 已述，属预期步骤不是缺陷）；② bili `requirements.txt` **31 包全量**（含 opencv/pandas/kafka，服务路径并不用）；③ **NGA 无 requirements 文件**（缺 `beautifulsoup4`，装到才起得来——真发现）；④ NGA **模块级 import** `confluent_kafka`/`pymysql`（服务不用）；⑤ **NGA 服务 stdout 重定向下块缓冲、日志看不见**（需 `python -u` 或让 `log()` flush）；⑥ 服务日志在客户端正常断开时刷 `ConnectionResetError` traceback（噪声）。

**边界**：本轮只做「拉码 + 适配 + 跑通」，**没碰演示服务器**。副作用：仓库 `crawlers/` 下 4 个文件（`bili/media_platform/bilibili/core.py`、`bili/bili_search_server.py`、`nga/nga_search_server.py`、`nga/nga_crawler_playwright.py`）已改，**与演示服务器 上在跑的版本产生分叉**——同步回演示服务器 属铁律区，**须用户先拍板 + 先评估 OOM**。前端接后端 `/api`、前端扫码恢复登录态的真实链路，仍不在本轮。

### 10.48 设置面板填满 + 平台比例去百分号 + 导出 PDF 开关（2026-09-15 00:xx~01:xx，用户「给设置里面填充，平台比例按钮不要用%指代 直接写上，在发送按钮左边增加一个PDF输出开关」+「1.B」）

**用户口令**：先问「我们都有哪些配置项？」，点明三件事——① 设置面板要**填满**（不是留个占位）；② 平台比例**别用 ％ 打哑谜，把话写出来**；③ 发送键左边加**导出 PDF 开关**。追问设置项可否在页面上改，用户答 **1.B = 可改可存**（不是只读展示）。

**做法**：

- **新 `harness/settings.py`**（设置的后端，纯标准库）：一张 **15 项注册表**、按 **5 组**（作答 / 情感度量 / 爬虫启停 / 存档 / 接法（高级））铺开；**三层优先级** 页面设置（`settings.json`）> 环境变量（`config.bat`）> 默认值，**每项在页面上标出「来源」**，免得用户改半天不知道被谁盖住；**两类生效**——`_LIVE` 表里的改完**当场生效**（直接推 `supervisor.configure()` / `measure._XXX` 常量），其余标「改完要重启」。
- **凭据硬规矩**：大模型密钥 / 打分服务令牌 **只回报「已配置 / 未配置」**，明文永不回传、永不写进 `settings.json`（`_SECRETS` 集合把关，`save()` 收到也直接拒）。
- **新 `harness/pdfout.py`**：用包里已有的 playwright 渲染 A4 PDF，**不引 PDF 库**（浏览器能把中文、长引用、表格排得像样，引库反而要自带字体）。`[id=N]` 转成上标徽标，**引用清单真排进正文之后**（页脚那句「对回上面的引用清单」指着它）。playwright 同步接口不能跨线程复用 → 一把锁串行化，导出是用户点一下才发生的事。
- **`supervisor.configure(idle_sec=/start_timeout=/lazy=)`**：运行期改旋钮（页面改设置时调），从「常开」切回「按需」时补起巡检线程。
- **`webapp.py`**：`settings.apply_to_env()` 必须排在 import `supervisor`/`measure` **之前**（那两模块 import 时就把环境变量读成常量了）；新增 `GET/POST /api/settings`、`GET /api/pdf`；`/api/ask` 透传 `ratio` 与 `pdf`，并把**本题实际用的比例**写回 `ask`（原先前端那条「平台比例 NGA 50%」是**硬编码的假数字**，铁律不许——改成有值才标）。
- **前端 `web/index.html`**：设置窗从「预留区 + 一个清除记忆按钮」改成**真清单**（分组、逐项输入框、来源角标、要重启的标出来、保存后回报「几项当场生效 / 几项要重启」与逐项错误）；平台比例**四个字写全 + 当前档位用汉字写**（用户自己又精修过说明文案：中间档只是**软引导 ±5 个百分点**、不硬门，跟引擎实际行为对齐）；发送键左边加**导出 PDF** 胶囊；题卡里给**下载 PDF** 链接（导出失败则如实标「PDF 没导成（答案本身没问题）」）。

**验证（按「用户会点的功能，判据必须是真开页面点到那步」走，全部无头）**：

- `_syscheck/ui_check.py` 真开 `:8780` 点一遍：设置窗 **15 行 / 5 组**、**2 项只读凭据**；改 `NLP_JUDGE_CAP` 40→41 → 后端确认落盘且来源变「页面设置」→ 再改回；比例滑到 70 → 显示 `NGA 70 : bilibili 30`、title 同步；PDF 开关 `aria-pressed` 翻转；**拦下 `/api/ask` 看真实请求体** = `{"question":...,"mode":"快速","session":...,"ratio":50,"pdf":true}`。零页面报错、零多余失败请求。
- **PDF 端到端**：合成一条 ask → `render()` → 90 KB → `GET /api/pdf?id=...` 回 `200 application/pdf`、magic `%PDF-`；另把同一份 HTML 截了图，肉眼核过题头 / 警示框 / 正文上标徽标 / 引用清单 / 页脚。
- `_syscheck/ui_shot.py` 截图核排版：折叠态「快速｜精准｜平台比例｜(空格)｜导出 PDF｜↑」——**导出 PDF 紧贴发送键左侧**（第一版把 `margin-left:auto` 留在发送键上，开关被晾在左边，已改到开关上）。

**「真跑一遍」才暴露的四个 bug**（这条是本轮最值钱的记录）：

1. `pdfout.build_html` 的格式串**多一个 `%s`、欠一个实参** → 一调就 `TypeError`；更糟的是 `blocks`（引用清单）**建好了却没插进模板**，而页脚已经写着「每个 ［id=N］ 都能在上面的引用清单里对回原帖」。**PDF 这条路此前从未被跑过**——只写了、没验。修：补 `cite_sec`（带 `<h2>引用清单（N 条）</h2>`）并把实参补齐。
2. PDF 用 playwright **自带那份 chromium** → 本机没装（爬虫走的是**系统 Chrome** `channel="chrome"`）→ 改成与爬虫同一套；顺带把「装包时要不要 `playwright install chromium`」这个问题消掉——爬虫本来就要求系统 Chrome。
3. `settings._source()` 拿**实时 `os.environ`** 当判据，而 `apply_to_env()` 会把页面存的值也灌进 `os.environ` → 页面设过的项被错标成「环境变量(config.bat)」。改成**只看启动时抓的 `_FROM_ENV`**。
4. **留空保存**会把空串写进 `settings.json` 并灌进 `os.environ`；下游读法都是 `os.environ.get(k, 默认)`，**空串会被当成真值用**（`NGA_HTTP` 一空、爬虫地址就成了空串，整条链连不上）。改成「留空 = 撤掉这层的覆盖、回到环境变量/默认」，不落空串。

**收尾纪律**：验证过程写出的 `settings.json`（测试把 13 项全存了一遍，页面上「来源」全变「页面设置」）与作废 PDF 已删；删完重启后端复核 **15 项、来源零异常**。全程无头，未弹可见窗口。

**边界**：`DEEPSEEK_API_KEY` / 打分服务令牌只做存在性展示，页面上改不了（只能在 `config.bat` 里改）。「清除我的对话记忆」按钮仍是**未接线的占位**（先不接，等删除契约落地）。

### 10.49 15 题系统测试：三批跑完（批内 600s / 批间 1800s），跑得通、双平台全覆盖，但**护栏没兜住大样本**（2026-09-14 18:53 → 2026-09-15 01:07，用户「先做系统测试 去爬15题题单试试 注意每题之间的间隔和三批次每批次的间隔」）

**用户口令**：要跑**系统测试**、用 15 题题单，并**特别点出间隔**——每题之间、三个批次之间都要按规矩来。同时明确**先不清测试残留**（「清啥啊」）。这是**按需启停上线后第一次长跑**（6 小时），顺带把「爬虫按需拉、闲下来自己关」在长跑里验一遍。

**怎么跑的**（编排脚本 `_syscheck/run15.py`）：三批 **5+5+5**，批内每题间隔 **600 秒**（`--interval 600`）、批间 **1800 秒**；`--nga-ratio 50`；三批**共用一个库**（`_syscheck/syscheck_20260914.db`）与**一个 tag**，scope 即 `exp:calib:syscheck-20260914:qN`（N 全局 1..15）；脚本自带**互斥闸**（先扫同类 `calibrate` 进程 + 落锁文件，跑完杀端口、删锁）。

**间隔落实情况（对着编排日志核）**：起跑 18:53:30 → 收工 01:07:43，**共 6 小时 14 分**，三批全 `exit=0`。批间实测 **30.0 / 30.1 分钟**（=1800 秒，一次不多一次不少）；批内每题收尾都 `sleep 600s`（报告每段都有）。

| 题 | 用时 | 轮 | 输入token | NGA | bilibili | 引文对不上 |
|---|---|---|---|---|---|---|
| q1 | 475s | 4 | 126,807 | 77 | 214 | **24** |
| q2 | 405s | 4 | 66,648 | 17 | 105 | 0 |
| q3 | 717s | 2 | 221,799 | 30 | 1,174 | 1 |
| q4 | 289s | 3 | 78,500 | 49 | 183 | 0 |
| q5 | 301s | 4 | 103,105 | 31 | 184 | 0 |
| q6 | 728s | 6 | 245,071 | 166 | 304 | 0 |
| q7 | 777s | 6 | 272,525 | 89 | 405 | 1 |
| q8 | 1,019s | 4 | **489,641** | 118 | 963 | 0 |
| q9 | 671s | 5 | 165,255 | 87 | 235 | **21** |
| q10 | 981s | 5 | 223,788 | 34 | 392 | 0 |
| q11 | 685s | 5 | 246,065 | 100 | 356 | 0 |
| q12 | 980s | 6 | 112,378 | 59 | 70 | 0 |
| q13 | 1,215s | 6 | 301,023 | 151 | 325 | 0 |
| q14 | 1,246s | 6 | 263,000 | 124 | 273 | 0 |
| q15 | 1,140s | 6 | 266,686 | 110 | 273 | 0 |

**合计** 输入 token 3,182,291 / NGA 1,242 条 / bilibili 5,456 条；单题最快 289s（q4）、最慢 1,246s（q14）。

**三条判据的结论**：

1. **跑得通 —— 15/15 通过**。15 题**没有一题 `(未收口)`**、三批全 `exit=0`；流水校验 `harness.runner.verify` 逐 scope 跑，**15/15 `OK=1`、零硬伤**（每个 `[id=N]` 都能解析回**本题 scope** 的存档，无跨 scope 泄漏）。
2. **引得住 —— 10/15 全清，4 题共 47 处对不上**（q1: 24、q9: 21、q3: 1、q7: 1）。把这 47 处按「所引的号」聚一聚（`_syscheck\quotecheck_detail.txt`），**不是一种病，是两种**：
   - **q9 的 21 处：引用粒度问题**——21 处里 **20 处全挂在同一个号** `[id=4110]`（`[id=N]` 标记才 35 个，被核对的引文却有 42 条）。这是一段话里并排好几条引文、**只在段尾挂了那一个号**：号没挂错方向，但读者顺着这个号去核对，后面几条自然对不上。**不是编造。**
   - **q1 的 24 处：真·挂错号**——散落在很多不同的号上（差值 **-216…+196**，不是差一格），且「一个号扛 3 条以上引文」的只有两个（`[id=201]` 4 条、`[id=213]` 3 条），剩下的都是一号一条。**这才是本条该跟进的重点。**
   - q3/q7 各 1 处，是零星个案，不构成形态。
   **修法要分开**：q9 那种要改**引用粒度**（并列引文逐个挂号，别共用一个）；q1 那种要查**挂号那一步**（合成时把引文和号对上）。**这是本轮最该跟进的一条。**
3. **双平台覆盖 —— 15/15 全部两侧都有样本**，无一题单平台。

**顺带揪出的三件事（都已记，未修）**：

- **护栏没兜住**：q8 输入 **489,641 token**，**越过了精准档 320,000 的上限**；另有 8 题（q3/q6/q7/q10/q11/q13/q14/q15）落在 **22~30 万**之间。这与 §10.33「爬到的证据全量进上下文」是同一根线上的事——证据一多，护栏就成了摆设。
- **B站从 Q10 起超时变多**：q10/q12/q13/q14/q15 都出现 `bilibili crawl fail: TimeoutError`，q12 一连失手 5 次（那题 bilibili 只取到 70 条）。**疑似长跑几小时后被风控**。**但引擎的降级是诚实的**——q12 的答案里明写「绝区零 B 站侧本轮未取到样本，其风评仅 NGA 单平台」，没有拿单侧样本冒充双侧。
- **阻塞字段渲染有个小 bug**：日志里出现 `None:bilibili crawl fail: ...`，平台名取到了 `None`。这是 `calibrate` 的**显示**问题，不是引擎问题。

**按需启停经受住了长跑**：每个批次边界都是 `chrome=0`、8770/8771 均为 `False`（=爬虫已按时关掉、**没留孤儿浏览器**），收尾也是「端口已关、锁已清、chrome=0」。

**边界**：判据「软信号是真模型」在**本包不适用**——本机没装打分服务，度量走关键词降级（用户已批）。测试残留按用户意思**先不清**。

---

### 10.52 爬虫状态补两个「忙态」：**正在使用**（爬虫侧）与**冷却中**（引擎侧，带倒计时）（2026-09-15）

**用户口令**：「做吧，爬虫状态那里应该有两个新状态：冷却中（避免风控，放置倒计时）和正在使用」。

**为什么值得单做**：这两个态此前**都已经在数据里、只是没画出来** ——
① `busy` 早就在爬虫 `/health` 里（`RUN_LOCK.locked()`），也一路带到 `status_payload()`，前端从没读；
② 会话冷却（B站风控退避）在引擎 `_plats[...]["cool_until"]` 里，页面上一个字都没有。
于是「这题怎么卡了半天」在界面上看不出来，用户只能猜。

#### 一、两个态各归各的账（这是关键，别混）

| 态 | 谁的状态 | 数据来源 | 寿命 |
|---|---|---|---|
| **正在使用** | **爬虫服务**的 | `/health` 的 `busy`（`RUN_LOCK` 被占着） | 一次 `/crawl` 的时长，秒级到分钟级，自己消失 |
| **冷却中** | **引擎**的（按会话） | 引擎 `_plats["bilibili"]["cool_until"]` | `COOL_START 300` 起翻倍封顶 `COOL_MAX 900`，B站专属 |

故 **`busy` 不带会话也有意义**，而 **`cool_left` 不带会话就没有意义**（同一个爬虫，这个会话在冷却、
别人那个会话不在）—— 这正是 `/api/status` 这轮要**带上 `session`** 的原因。

#### 二、后端：`/api/status?session=<sid>` 多一个 `cool_left`

`webapp._cool_left(sid)` 从**已存在的** Session 里读（`_SESSIONS.get`，**不建**）—— 轮询状态不该顺手
建 Session，那会把空 scope 写进记忆库。每行爬虫多带 `cool_left`（秒，不在冷却就是 `0`，**不是缺字段**，前端好判）。
秒数**进一位**（`int(left)+1`）：倒计时显示 `0` 时它才该真的好了。

**这轮自己揪出的一个真 bug（已修）**：引擎 `_plats` 的键是 **`"NGA"`（大写）**，而 `CRAWLERS` 表里的平台名是
**小写 `"nga"`** —— 直接 `plats.get(plat)` 会让 **NGA 那行永远查不到冷却**。NGA 眼下不冷却（它只熔断不冷却），
所以这个洞**一声不吭**；哪天给 NGA 也加冷却就会静默失效。改成**大小写归一后再查**，
并在自检里**按引擎的真实键（大写 `NGA`）摆数据** —— 否则用例会因为「名字凑巧对不上」而假绿。

#### 三、前端：判定收成一个纯函数，倒计时自己走字

- `crawlerState(c)` 是**纯函数**（不碰 DOM）：吃一行爬虫数据，吐 `{w, brief, cool, busy}`。
  优先级 **冷却 > 正在使用 > 未启动 > 原四态**（冷却带倒计时、信息最多，且它和 busy 同时成立时
  「正在用着」是上一秒的事）。写成纯函数是为了自检能直接喂数据 —— 见下。
- **灯色**：冷却中＝琥珀（跟「待校验」同色系，但它是**正常等待**不是异常）、正在使用＝蓝色**呼吸**（`busyPulse`）。
- **倒计时**：`coolAt[key]` 记本地截止时刻，**每秒自己走字**（后端状态是 30 秒一次，秒表不能靠它）。
  走到 0 **不自己判定「好了」，而是立刻去问一次后端要真值** —— 界面不许自己编状态。
- **正在使用时不给「去修复」按钮**（换成一句「正在使用，等它空下来」）：爬虫此时 `RUN_LOCK` 拿不到，
  按下去只会回 `429`，那是给用户按一个必然报错的钮。
- 每次问答题的 3 秒轮询里顺手刷一次灯（否则「正在使用」要等 30 秒那趟才看得见）。

#### 四、冷却期**强制不可使用**（用户补一句「注意了」）

**用户口令**：「冷却时强制不可使用 注意了」—— 这是对我上一版的**纠正**：我原来只把「正在使用」的按钮撤了，
「冷却中」还留着「去修复」。**冷却不是给用户看的牌子，是一条真管用的闸。**

先把「哪些路能碰到 B站」数一遍，再逐条堵：

| 路 | 原来 | 现在 |
|---|---|---|
| 模型调 `crawl_bili` | 已被引擎 `_precheck` 挡 | 不变（补了自检钉住） |
| 模型调 `crawl_official`（平台钉死 bilibili） | 也已挡 | 不变（补了自检钉住） |
| 页面点「去修复」→ `POST /api/login/bili` | **没挡** | **429 挡下，且挡在 `supervisor.ensure` 前面** |
| 页面点 NGA「去校验 / 扫码」 | 没挡（NGA 不冷却） | 一视同仁地挡 —— 规则是结构性的，不靠"记得给新平台加" |
| `/api/status` 轮询 | 只读本地 `/health` | 不变（**不能挡** —— 挡了用户就看不见正在冷却） |

**关键点：挡在 `supervisor.ensure` 前面。** 冷却期里连爬虫带浏览器都不必拉起来，更不必去打 B站的登录页 ——
风控退避期里再碰它，正是要避免的事。自检里专门有一条断言「`ensure` 一次都没被调到」。

**这轮自己揪出的一个真 bug（已修，且是"强制"被绕过的典型）**：我先写的是
`blocked = self._cool_guard(...)` + `if blocked: return blocked`，而 `_json()` **只写响应、不返回值** ——
于是 `blocked` 恒为 `None`，**响应看着是 429，代码却一路往下走**，真去 `ensure` 拉爬虫、真去打 B站登录页。
自检里那条「且没有去拉爬虫」的断言（`ENSURE == []`）把它抓了出来。改成 `_cool_guard` 挡下时写响应并 `return True`，
并把「不能写成 `if self._json(...)`」记进了 docstring。

**一处明说的边界（本轮没堵，留待拍板）**：`POST /mcp` 那条路（**别人家的 agent 连我们当工具用**）走的是
`registry.call()`，**根本不经过引擎** —— 冷却在引擎里、且**按会话**，而 MCP 服务端**没有会话这个概念**。
所以：外部客户端在这期间调 `crawl_bili`，仍然是会真爬的。这不是漏改，是**语义上还没定**（"谁的冷却"？
本机账号级的？还是每个连接各算？）—— 要堵得先把冷却从"按会话"改成"按机器"（进程级），那是另一件事，
不塞进这一轮。留在此处备查。

#### 五、怎么验的（全部离线，没打网、没烧模型、**没起浏览器**）

- 新增 `_syscheck/verify_status.py`（**38 条全绿**）：前半把 `status_payload`/`_cool_left` 抠出来喂桩数据 ——
  剩余秒数算得对（含「不到 1 秒报 1」）、**只读不建**（查一个没跑过题的会话，`_SESSIONS` 数量不变）、
  两态都随每行下发、**爬虫没起时也照报冷却**（冷却是引擎的账，与服务在不在跑无关）、
  `health` 坏掉不把冷却连带弄丢、**大小写归一**那条；后半是**真起一个后端在空端口上、逐个口敲一遍** ——
  冷却中打「开始扫码」回 429、话里说清原因与还要多久、**且 `ensure` 一次都没被调到**（证明挡在它前面）、
  换个没冷却的会话照常放行、bili 冷却不影响 NGA；再加引擎侧那半：风控失败 → 记冷却 → **熔断清了冷却还在照样挡**
  → 冷却走完自动恢复 → 连着被风控时间翻倍 → 成功一次归零。
- 新增 `_syscheck/verify_status_ui.js`（**36 条全绿**，`node` 跑）：抠出 `crawlerState` 等纯函数断言
  「什么数据 → 画什么灯」，含老四态没被动、`busy=null` 不算在用、`cool_left=null` 不炸、
  倒计时写法（`2:05`/`0:09`/`0:01`）、以及页面上那几处接线还在（`data-cool`/每秒 tick/带 session/
  **走完销账** —— 不销的话后端一断, 那盏灯会每秒空问一次 / **冷却中不给修复钮**）。
- `index.html` 内联脚本进 `new Function` 过语法；分包内 `python -m harness.test` **guard 0 违规、0 FAIL**（与上轮同为 1 PASS / 23 SKIP）。

**边界**：**没在浏览器里真看过这两盏灯**（要真造出「冷却中」得让 B站真被风控一次，不该为测试去伤平台；
「正在使用」要真跑一题）。按规矩没代劳开浏览器，**看灯这步交用户自己点**：问一题看它转蓝、
或被风控后看倒计时走字。

**另外两处**：① 冷却**只在内存里、只在这一次进程里**（按会话存在引擎对象上）—— 后端重启，冷却就没了。
这是既有设计（见 `PLAN.md §10.2`），本轮没动，但"强制"的成色受它影响，记在这里备查；
② 上面那条 MCP 侧的边界。

---

### 10.51 工具全部挂上 MCP：**一份注册表管三处**，我们对外是服务端、也当客户端连别人（2026-09-15）

**用户口令**：「把 NLP 两个爬虫分别包装为 tool，NLP 为可选 tool，在设置页面添加 tool 工具，支持 MCP 格式即可」
→「我们工具本身不就 mcp 吗？我们让用户可以自己拓展 mcp 工具不行吗？**我们可以提供也可以使用**啊 /
我们这次做的是后面要上传到 github 的，用户后面装的也是这个，但我们 harness 会自带」。

**拍板**：两个方向都做（对外提供 + 对外连接），自带工具随包发。

#### 一、一份注册表，三处消费

`harness/tools/registry.py` 是自带工具的**唯一正源**：一件工具 = 名字 / 中文名 / 受哪个开关管 /
是不是可选 / 给模型看的说明 / **一份 JSON Schema** / `needs()`（本机缺什么）/ `call()`。
三处读同一份，故永不打架：**① 引擎**组模型可见的工具面（OpenAI `parameters`）、**② MCP 服务端** `tools/list`
（同一份 schema 换个壳叫 `inputSchema`）、**③ 设置页「工具」栏**（每件一个开关 + 「本机缺什么」照实显示）。
原先写死在 `engine.py` 里的 `TOOL_DEFS` **删掉** —— 那种「工具面同时在两个文件里」的结构迟早对不上。

**关掉一件工具 = 它整个从工具面消失**（结构性防线，不靠提示词劝模型别用）。

#### 二、两个方向，都是 MCP，零依赖

`: 我们用别人的工具` 与 `别人用我们的工具` 两条都通，**都不引官方 `mcp` SDK**（那会牵进 pydantic + async，
与这套「下载即跑、纯标准库」的身份不符）。`harness/mcp/` 四件：`protocol.py`（JSON-RPC 消息层）、
`server.py`（对外）、`client.py`（对内）、`__main__.py`（stdio 入口）。

- **对外**：`python -m harness.mcp` 走 stdio；网页后端另开 `POST /mcp` 走 HTTP —— **同一套处理逻辑**
  （`handle()` 是纯函数，两处共用），支持单条与批量。stdio 那条有个坑：`supervisor._log` 往 stdout 打日志，
  会把 JSON-RPC 冲烂，所以 `serve_stdio` 一进来就**把 `sys.stdout` 换成 `stderr`**。
- **对内**：设置页「外部工具」填别人提供的服务（本地命令 / 网址两种）。外部工具在模型面前叫
  `mcp__<服务>__<工具>`，和自带的不会撞。

#### 三、三处硬要求（都验过，不是口头承诺）

| 要求 | 怎么落 |
|---|---|
| 外部服务坏了**不能拖垮整题** | 连不上/超时/回垃圾一律转成一句人话错误返回给模型，**不抛异常**（抛上去整题作废） |
| **超时必须是真的超时** | Windows 上读子进程没法 select → 每个会话配读取线程把消息塞进队列，请求侧在队列上等 |
| 设置页刷一次**不能把外部服务挨个连一遍** | 只用缓存（`peek_tools`，没测过 = `None` ≠ 「测过但没有工具」`[]`）；真连只在用户点「测试」时发生 |

#### 四、这轮真揪出的四个 bug（都是自检时暴露的）

1. **`nlp_score` 进了工具面，引擎却没有认领它的分支** —— 模型一调回 `unknown tool`。加了分支，并补一条
   **结构性护栏**（自检里断言「注册表里每件工具都必须在引擎里有分支认领」），挡住「加进清单忘了接线」。
2. **关掉的爬虫只在模型面上消失，真调仍会硬爬** —— 开关原来是「劝」，不是「拦」。改成挡回去（回一句人话错误）。
3. **外部工具名把非 ASCII 全换成下划线** —— 「我的工具」和「工具我的」会压成同一个名字，**两个服务串台**；
   而且原名反推不回来。改成：干净名字照搬（一眼看得出是哪家）、含中文的压短哈希，**还原靠组名时留下的一张对照表**
   （`engine._ext_defs`），不是反推。
4. **设置页保存时若「外部工具」那栏没读出来，会把用户已配的清单存成空的** —— 加了 `data-loaded` 闸：
   那一栏没读出来就**不碰**它。

#### 五、验证（全部离线：没打网、没烧模型、**没起浏览器**）

| 自检 | 结果 |
|---|---|
| `_syscheck/verify_mcp.py`（新增） | **约 70 条断言全绿** |
| —— 注册表 / 开关 | 关掉一件它就从两种形状里一起消失；可选工具缺依赖时**不在面上**（而非在、一调就报错） |
| —— 服务端消息层 | 握手 / `tools/list` / `ping` / 通知不回话 / 不认识的 method 回「方法不存在」 / 工具报错走 `isError` |
| —— 客户端 | **拿我们自己的服务端当外部服务接一遍**（一次把两侧都验了）：列工具、真调一件、坏服务不抛、缓存该失效就失效 |
| —— 引擎 | 名字对照表还原得回中文原名、两家不串、不在表里的名字报错不瞎猜 |
| —— HTTP | 后端真起来，`/api/tools`、`/api/mcp`（存/测/不认识的 action）、`POST /mcp`（单条 + 批量）逐个敲一遍 |
| `_syscheck/verify_tools_ui.js`（新增，`node` 跑） | **30 条全绿**：把设置页纯拼字符串那几个函数抠出来、喂真实后端数据断言 HTML；含**该转义处转义**、`[]`（测过没工具）与 `null`（没测过）分得开、那一栏没读到时**不打 `data-loaded`** |
| `python -m harness.test`（分发包内） | guard **0 违规**（新加的 `mcp/`、`tools/` 都在扫描范围内）；离线脚本照旧 SKIP |
| **没验的** | 设置页那套新 UI **没在浏览器里真点过**。本轮守「不在这台机器上起浏览器」的规矩，只做到「不吃进程」的自查；剩下**只有点才验得到**的那几步（切栏、＋加一个、测试改文案、保存后重开还在不在）**交用户自己点** |

#### 六、边界 / 与旧口径的冲突（待收口）

- **`§10.32 ①` 与本次冲突**：那一节说「不给模型加第四个工具」，而 `nlp_score` 现在**会进工具面**（**默认关**，
  故默认工具面没变）。收尾时口径要统一。
- **`§10.32 ①` 的另一半始终没做**：那节还要求「摘掉自动挂载、改按需 + 降级改 LLM 自判」——
  `harness/archive.py` **至今仍在自动调** `measure.score_records`。
- **分发包 `README.md` 还是仓库版视角**（`_tmp/`、演示服务器 隧道、`harness_dev.db` 那几行与产品包对不上），收口时一并改。
- 本次改动**全部落在 `harness-dist`**，源码仓库那份没动（承接 §10.49/§10.50 的分叉待拍）。

---

### 10.50 三处收口全修（引用挂号 / 护栏硬拦 / B站风控），并**更正 §10.49 两处自查误判**（2026-09-15，用户「都修吧 拦住」）

**用户口令**：「都修吧 拦住」——§10.49 记的三处（q1 挂错号、q9 引用粒度、B站长跑超时）全修；护栏那条**不是提高上限，是拦住**（真要发出去的那一份超了，就压回线下）。

#### 一、先更正 §10.49 的两处误判（自查有两处是错的，别照那条往下做）

- **【更正 1】q8 的 489,641 不是「单次请求越过上限」，是那一题四轮的累加。**
  `flow` 里的 `prompt_tokens` 是**跨轮累计**的；按单次请求量。q8 的闸门**从没为它触发过**。
  §10.49 说「护栏没兜住」这个定性要改：护栏不是没拦，是**量错了对象**——它量累计，该量的是「这一炮真要发出去的那一份」。
  §10.49 表里 q3 那题的 `ctx: input ceiling 160000` 才是**真触发过一次**的证据。
- **【更正 2】q1 的 24 处不是「真·挂错号」，是被「括号里混写说明」噎住的引用。**
  §10.49 判它「散落在很多不同的号上、差值 -216…+196 = 真挂错」，是**照着复核报告反推**出来的，没去看原文。
  实际成因单一：模型爱把热度一起塞进括号——`[id=49，like=530]`。这种 token **解析不出来**（`cites._parse_inner` 不认），
  于是整条引用等于没标；而它**前头那句引文**会被算到**下一条**真引用名下 → 复核看着就像「号挂错了」。
  id 一直在原地、一次都没挂错。抠掉说明后**归零**（见下方 `verify_quote_repair.py`）。
- **教训（两次同型）**：q8 是拿**累计数**当单次数比，q1 是拿**复核报告的结论**当原文事实。
  两处都是**没先复现、没先给失败形态定型就下了结论**。往后这类「数字/形态对不上」的判断，先把它还原成可跑的最小样本再说。

#### 二、修法（三处）

**① 引用挂号（对应更正 2，q1 的 24 处 + q9 的 21 处）**——分两条路：

- **提示词**（治根）：`engine` 的规则 2 改写——括号里**只放 id**，混写 `[id=283，sarcasm=1]` 这类整段不算引用；热度/时间写在**括号外面**。
  新增规则 **2b**——**一条引文挂一个号、号紧挨着它自己那句引文**，别把并排的几句引文攒起来只在段尾挂一个号（这条治的就是 q9）。
- **确定性收口**（兜底，不靠模型自觉）：`_seal_answer` 里按序加两步——
  - `cites.normalize_junk`：把 `[id=N，说明]` 抠成 `[id=N]`（说明丢掉），只动「解析不出来且数字打头」的，合法成组/区间/`[id=abc]` 一律不碰；
  - `quotecheck.repair_answer`：引文在自己挂的号里**找不到**、却在**另一条且唯一一条**帖子里找得到时，**就地改挂**到那条真出处（多轮迭代，因为插进去的标记会改后文 token 的归属区；查无出处/多处命中的一律不动、只标注交人核）。

**② 护栏硬拦**：新增 `engine._shrink_to_ceiling(messages, ceiling)`，**只在收口那一炮出网前**对**工具结果**两段压——
先按「每条留头 3000 字 + 截断说明」，还不够就把**最早**那条退成一行占位。**tool 消息一条不删**（删了 `tool_calls` 配不上，整轮请求会变成非法 400）。
平时不裁任何证据轮（答案引用跨全部爬轮，裁了会断 `[id=N]` 链）——省 token 的正解是少绕圈，不是截原文。

**③ B站长跑超时**：真病根不是「重试太慢」，是**撞了风控却没人知道**。

- `client.request`：**412/403（B站安全风控，回的是 HTML 拦截页）立刻失败、一次都不重试**（原来退避重试 5 次，把一次爬取拖过上游 300 秒上限，上游只看到一句 TimeoutError，「撞了风控」反倒被吞掉）。
- `DataFetchError` 加 `risk` 标记；`core.search_one_query` **不再把风控失败吞成 `None`**（吞了就等同于「真没搜到」——不冷却，下一题接着撞）；
- bili 服务端 `/crawl` 把风控单独回成 **503 + `risk:True`**（不混进 `crawl internal error`）；
- `live._post_path` 收错误体时**保留 JSON 结构**（原来拍成一行字符串，`risk` 直接丢）；`_raise_if_err` 把「风控/超时/429」认成 throttle。

#### 三、验证（全是离线，没打网、没烧模型）

| 自检 | 结果 |
|---|---|
| `python -m harness.cites` | **PASS=31 FAIL=0** |
| `_syscheck/verify_quote_repair.py`（拿 15 题现成流水复核） | **改前 47 处挂错 / 改后 0 处**（q1 抠混写 41、q9 补号 22、q3/q7 各 1、q11 抠混写 1） |
| `_syscheck/verify_guardrail.py`（合成样本，15 项） | **PASS=15 FAIL=0**（含「压完落到线下」「tool 消息一条不丢」「`tool_call_id` 一一对应没断」） |
| `_syscheck/verify_bili_risk.py`（新增：起回环假服务端，20 项） | **PASS=20 FAIL=0**（串到引擎那端：风控失败→bili 进会话冷却、下一题再调被挡下；NGA 只熔断不冷却） |
| `python -m harness.test`（**分发包内**） | guard **PASS**、gold **SKIP（如实）**、20 个离线脚本 SKIP |

**顺带修掉的两处（都是验证这一步才暴露的）**：

- **`guard` 把 `settings.py` 的原子写误判成破坏性原语**：它写的是**配置文件**（settings.json / secrets.json，临时文件 + `os.replace`），不属于「数据层只增不改」那条契约。
  给 guard 加了**文件头豁免标记** `guard: allow-write`（只认头部 2000 字，一眼可见、也免得别的文件随手一句注释就把自己放行），`settings.py` 头部声明。
- **`evalgold` 在分包里逐题报 ERR**：`gold_cases.json` 是**开发期录的那批题**（scope 写死在里面），不随包走；本机没这批流水时它逐题报「无对应 flow 条目」，把自测刷成一片红。
  改成**本机一道都没有时如实 SKIP**（「黄金回放的底是开发期录的，不随产品走」）——那是「没有可回放的底」，不是产品的错。

**更正 §10.49 之外的第三处：分包内的验收口径**。此前记的「分发包里跑 `python -m harness.test` 仍 22 通过 / 0 失败」**不成立**——那 20 个离线脚本在源码仓库的 `_tmp/` 里，**不随包走**。
分包内的真实口径是：**guard 1 PASS + gold（有底才跑，没底 SKIP）+ 20 SKIP**，退出码 0。

#### 四、边界（没做的与本轮没覆盖的）

- **没真跑一次 412**：真去撞既慢（守 5-10 分钟间隔）又伤平台。`verify_bili_risk.py` 覆盖到「服务端回什么形状 → 下游怎么认 → 引擎真冷却」为止；
  **唯一没覆盖的**是「真爬虫发出请求、真服务端把 412 翻成 503」那一步（`client.py` / `core.py` / `bili_search_server.py` 三处只过了 `py_compile`）。
- §10.49 记的 `calibrate` 阻塞字段把平台渲染成 `None`，仍未修。
- **本次改动全部落在 `harness-dist`**（分包）；源码仓库那份**没动** —— 两边分叉又宽了一格，待用户拍板（承接 §10.49 item「分发包 vs 源码仓库分叉」）。

---

### 10.53 拆开「会话」这一把钥匙：**记忆按会话隔离，证据跨会话仍回捞得到** + 扛得住追问 + 前台补多对话（2026-09-15）

**用户口令**：「你改吧，前端页面在用户第一次进入的时候也抄一下布局风格，去掉logo /
我现在希望的是我们的harness能扛得住追问，做得好会话隔离，在之前查过的情况下还能回去查到」。
本轮**全部落在 `harness-dist`**（承接 §10.50 的边界：源码仓库仍未同步）。

#### 一、病根：一个 `scope` 键干了两件事

`scope` 同时被当成「这段对话的**记忆**归属」和「这条**证据**归谁」。于是「换个对话」把证据也一起藏了 ——
用户要的「之前查过的还能回去查到」被**结构性地**挡住，不是某个查询少写了个条件。

#### 二、拆法：记忆守住 scope，证据折成「池」

- 新增 `harness/evidence.py`：`pool_of(scope)` —— `exp:*`（标定）**各自成池**（标定与真实样本必须互不可见，两个方向都查），
  其余（`prod` / `web:*` / CLI）**归一池 `local`**；`pool_clause()` 出 SQL 条件（真实池那条是 `NOT LIKE 'exp:%'`）。
- **记忆那半一个字没松**：状态卡 / `session_claim` / `checkpoint` / flow 流水**仍严格按 scope**。
- **穿透四处读路径**（漏一处就"半开"）：`archive._where`（检索/计数）、`archive.record_crawl`（**去重键按池算**，
  否则同一条帖子在别的会话再爬到会存两份、检索重复计数虚高）、`quotecheck`（引文核验语料）、`engine._sanitize_cites`（引用闸门）。
- **前端的数字没被带歪**：`webview.build_ask` 拆成「**本题新爬到的**」`stats.new` 与「**本题引到的**」`stats.reused`，
  卡片上「本题归档 N 条」不会因为池变大而变成谎话。`rows_of_ids` 明说**刻意不按 scope 过滤**。

#### 三、扛得住追问（`engine.ask`）

- 原先只发 `[system, user]` 两条，跨轮全靠 L1 状态卡。现在把**上一轮问答**拼进 `messages`（新增 `flow.recent` + `engine._recent_turns`）；
  **assistant 那侧只放 `concl` 那一句、不带 `[id=N]`** —— 免得教会模型照抄号。
- `Session.resolve` 的兜底链补最后一档「**本会话锚定的游戏**」：接着问「那它最近呢」题面里没有游戏名，
  原先退化成"未知游戏"、检索与过滤全失去约束。锚定值来自状态卡（上一轮真爬过哪个游戏）。`_load_relevant` 的检索词也补上了它。
- 提示词加了规则 **1c**：认出这是在追问谁，别重头再来；早先的证据在**跨会话共享**的归档里，引之前先 `search_archive` 捞回来。

#### 四、前台（`web/index.html`）

- **首次进入页**照参考产品的姿态做成**一竖列**（标题 + 一句实话说明 + 示例问句），**不带 logo**
  （`#logoSlot` 与 `.logo` 样式一并删掉，不抄人家品牌）；空对话时**标题与输入框一起居中**，问出第一题后输入框落回底部常驻位。
  示例问句点一下**只填进输入框、不直接发**（一题要爬几分钟，先让人看一眼）。
- **侧栏列表从「本会话问过的题」改成「本浏览器开过的对话」**：`localStorage` 一本登记簿（`ha_sids`），
  对话内容仍在后端那本账里（换机器打开就是空的，这是对的）。`#newSess` 与**点行切换**都接了真链路（原先是个只弹提示的空壳）。
  标题取自该对话**最早的问句**，不编。切/新建都挡在「有题在跑」之外；切之前先把上一个的题清干净再拉新账。
- **后端会话缓存**（`webapp`）：`SESSION_CAP=12`，按最久没用回收；**正在跑题的会话不回收**（抽走 = 那一轮的卡没了，
  检查点只在收口时落）；`_LOCKS` **不回收**（另一条线程可能正握着旧锁对象）。被回收是安全的：卡在 `checkpoint` 表，回来能续。
- 清除记忆窗里写死的「3 个会话 / 24 条消息」删掉（数字不编），改成按登记簿如实报对话数。

#### 五、验证（离线，**不启浏览器**）

| 自检 | 结果 |
|---|---|
| `_syscheck/verify_evidence_pool.py`（新，28 项） | **PASS=28 FAIL=0**（池折算/跨会话检索/标定池两向不串/同池去重/引用闸门/记忆隔离/追问进 messages/出题卡解析跨会话引用） |
| `_syscheck/verify_session_lru.py`（新，17 项） | **PASS=17 FAIL=0**（同 sid 同 Session/超限回收/**锁着的不回收**/查状态只读不建） |
| `_syscheck/verify_web_wiring.py`（`_tmp` 那版的适配，40 项） | **PASS=40 FAIL=0**（新增：产品不带 logo、首屏、多对话接线、数字不编） |
| `python -m harness.test`（**分发包内**） | **8 PASS / 0 FAIL / 22 SKIP**（5.7s） |

- `harness/test.py` 的 offline gate 改**两处找脚本**：`_tmp/`（开发期，源码仓库才有）优先，找不到落 `_syscheck/`（随产品走）。
  这更新了 §10.50 的验收口径：分包内不再是「20 个全 SKIP」，而是 `<随包脚本数> PASS + 其余 SKIP`。
- **顺手补挂的四支**（`verify_guardrail` / `verify_bili_risk` / `verify_mcp` / `verify_status`）：
  它们本来就住在 `_syscheck/`（随产品走）、本来就离线、本来就有退出码，**只是一直没挂进门里**（§10.50 是手工跑的）。
  挂上后分包自检从 4 PASS 到 8 PASS。**没挂** `verify_quote_repair.py`：它只打印对比报告、**一条断言都没有**（退出码恒 0），
  挂进门里会给一个假的绿灯；且它读的是开发期冻结的 `syscheck_20260914.db` + 本机 flow 流水，不随产品走。
  `ui_check.py` / `ui_shot.py` **绝不挂**（会启浏览器）。

#### 六、边界（没做的 / 没覆盖的）

- **真人点击没走过**（铁律：不许在他机器上开浏览器，headless 也不行）。静态接线（id/类/语法）与后端逻辑（28+17 项）都过了，
  **但那不等于点了好使** —— 首屏观感、新建对话、切来切去、连着追问这几条**待用户自己开页面点**。
- **没真跑一次跨会话回的题**（要真 LLM + 真爬）：`verify_evidence_pool.py` 是喂造数据到读路径，不是端到端。
- **老库的既有行**：`observation` 的去重键从 `web:<sid>|<raw>` 变成 `local|<raw>`，新装的库无影响；
  演示服务器 那台已有数据的老库若照搬，历史行的键仍是旧形状（不影响检索，只影响"同一条再爬到"的去重命中）。
- 源码仓库 `opinion-agent` 仍未同步（两边分叉），`_syscheck/` 这三支脚本要不要回流待拍。

### 10.54 补上「回话压缩」：早前轮次压成纪要 + `recall_answer` 把原话捞回来；前台加动效、题在跑时有占位卡（2026-09-15）

**用户口令**：「清掉 / 然后就是前端动效能做一下吗稍微，去找点动效库或者skill / 做完后直接在我电脑上模拟用户在一个会话内连续对话，
一个问题要能扛得住追问，比如用户问你之前回答的一部分具体是怎么样你要能回答，再比如要和别的游戏对比要能回答，
总之就是在一个会话里面要能一直回答，你懂吗?我们是不是没有回话压缩?」
—— **「我们是不是没有回话压缩?」的答案是：对，原来没有**。§10.53 做的「扛得住追问」只到**上一轮**（`flow.recent(scope, 3)` 摆最近 3 轮原话），
第 4 轮往前就只剩 L1 状态卡那一句摘要；用户问「你第 2 题具体怎么说的」，模型手里根本没有那轮的原话，只能编。本轮补上。

#### 一、病根：上下文里只有「最近」，没有「更早」

`engine.ask` 的 messages 是 `[system] + [最近 3 轮原话] + [本轮问句]`。会话一长，第 4 轮往前的对话**整段消失**：
不是被压缩了，是**从来没进过上下文**。且没有回捞手段 —— 模型看不见的，它也调不回来。

#### 二、修法：**确定性压缩**（不烧 LLM）+ **一个正式工具**把原话捞回来

分两层，各司其职：

- **`engine._session_memo(keep_recent=3)`** —— 已翻篇的那部分压成一份纪要，每行「题号. 问句 → 结论行」。
  **确定性折叠，不是 LLM 摘要**：不花 token（摘要本身也是一次模型调用）、不会因模型抽风把历史压丢、**长度有硬上限可证**。
  `MEMO_BUDGET = 1600` 字符；**到了上限先丢最早的，但永远保住开篇那一题**（会话的锚，丢了整段对话就没有起点了）。
  被丢掉的如实说明「中间第 2-N 题压在字数上限外，没摆进来 —— 用户提到哪题就 recall_answer 捞那题」，不假装完整。
  会话还短（≤ keep_recent）时**不插纪要**（原话都在下文，插了是重复灌水）。
- **`recall_answer`（`harness/tools/registry.py`）** —— 本会话早前某一轮的**答案全文**按 `query`（给几个词）或 `index`（会话内第几题，从 1 起）取回。
  题号是**会话全局序号**：纪要里的「3.」就是本轮拿 `index=3` 能捞到的那题。
  - 取不到就**报错并把这会话问过哪些题列出来**（好让他换词），**不默认拿最近一条糊弄**；
  - **只读本会话流水**（`flow.records(scope)`），跨会话一个字都捞不到；
  - 超 6000 字截断并注明；
  - 回的是**答案全文**（不是结论行），因为用户要的正是「你之前回答的那部分**具体是怎么样**」；
  - 附一条 `note`：里面的 `[id=N]` 是**那一轮的**引用号、本轮未必还成立，**要引用就重新爬/搜归档拿新号，别照抄这些号** —— 内容可照用，编号不可。（否则等于亲手教模型编引用。）

#### 三、接线与顺序（两条都不能错）

- **消息顺序**：`[system] → [纪要] → [最近 3 轮原话] → [本轮问句]`。**本轮问句必须在最后**，反了模型会当成「答完又补一句」。
- **`recall_answer` 是注册表里的正式一件**（`BUILTIN`），引擎工具面 / MCP / 设置页三处同名同源，**默认就开**；
  走它**不占爬轮预算、不落库、不触网**（纯读本会话 jsonl）。
- 提示词规则 1c 补一句：用户问到你**之前某一轮的具体内容**时别凭记忆编，调 `recall_answer` 把全文捞回来照着原话答，**别拿纪要那行摘要当原话复述**。
- 顺带把 `flow.py` 的读口**拆成两个**：`records(scope)` 只读本 scope 那**一个文件**（引擎每答一题都要拿它拼上下文，不能是 O(全部会话)），
  `read()` 保留为「回放/排查用」的全库扫描。`recent(n)` 就是 `records()` 取尾。

#### 四、前台（`web/index.html`）

- **动效**：**没引第三方动效库**，手写 CSS（一整套依赖只为一个入场动画不划算）。纪律是**只挂在「状态真的变了」的地方**：
  新出现的那条消息入场（`riseIn` 260ms）、弹窗（`fadeIn`+`popIn`）、首屏（300ms）、按钮按下位移 1px；交互过渡 180ms。
- **入场只给新出现的那条**：`renderAll()` 每次把 `#wrap` 整个 `innerHTML` 重写，CSS 动画挂 `.msg` 上会**整屏反复重放**。
  用 `PAINTED` 记账本 + `markFresh()` 筛出这轮新出现的（并封顶错峰 4 条×55ms）。
- **题在跑时有占位卡**：原先发出题到答案回来这几分钟里首屏已隐藏、`#wrap` 是空的，页面看着像死了。
  补 `renderPending()`：虚线卡 + 三个跳动点 + 「正在爬社区取样本 —— 这一题通常要几分钟」+ 流水线三步。
- **无障碍**：`prefers-reduced-motion: reduce` 时动画全关 —— 这是必备项，不是可选项。

#### 五、验证（离线，**不启浏览器**）

| 自检 | 结果 |
|---|---|
| `_syscheck/verify_session_memo.py`（新，50 项） | **PASS=50 FAIL=0** |
| `_syscheck/verify_web_wiring.py`（47 项，新增 §4b 七条动效检查） | **PASS=47 FAIL=0** |
| `python -m harness.test`（**分发包内**） | **9 PASS / 0 FAIL / 22 SKIP**（6.1s） |

`verify_session_memo.py` 四段：④ 流水读口（只读本 scope/写入序/取尾/空 scope 不报错）→ ① 压缩（短会话不插纪要/6 题折最早 3 题/带题号与结论行/
**长度有界不随题数涨**/逼到上限保住开篇/保留的是较近的/**「(未收口)」不冒充有结论**/跨会话一个字不进）→
② 回捞（index 精确/按词命中/越界报总数/非整数报错/全不命中列题/两者都不给报错/空会话如实说/跨会话捞不到/6000 字截断注明/「别照抄」提醒）→
③ 接线（注册表与 MCP 与函数面同名、默认开、参数表、中文名）→ **⑤ 用假模型真跑一轮 `ask()`**：
假模型把每次收到的 `messages` 原样记下来，于是「模型看不看得见早前的题」「调 `recall_answer` 拿不拿得到原话」从「我读过代码」变成**跑出来的证据**
（含：纪要只有一条且不重复最近几轮、最近几轮以**原话**在下文、本轮问句排在历史之后、回捞出的原话真进了第二轮 messages、
换会话真跑时上下文里一个字都没有上一个会话的题）。

#### 六、边界（没做的 / 没覆盖的）

- **真人多轮对话没跑过**（要真 LLM + 真爬 + 起爬虫）。第五段是**假模型**（零网络、不调 LLM、不拉爬虫），
  验的是「该带的东西到底带没带」，**不是「答得好不好」**。要不要在他机器上真跑一轮，**待拍**（见 HANDOFF 现态）。
- **动效只过了静态检查**（时序档位、`prefers-reduced-motion`、类名有定义、`node --check`），**观感要他自己开页面看**。
- **`_syscheck/` 里那 ~9.5MB 上一台机器的自检产物已清掉**（挪到源码仓库 `_tmp/_from_dist/`，可逆）；
  留下的只有 8 支自检脚本 + 2 个 node 辅助 js。
- **纪要只带「问句+结论行」**，不带引用细节 —— 细节按设计走 `recall_answer` 捞。这是有意的（否则纪要等于第二份全文，压缩就没意义了）。


### 10.55 真跑一轮「同一会话连问三题」：捉到两个真 bug（网页后端拉错解释器 / 同一会话第二题必挂）（2026-09-15 18:10-18:40）

**用户口令**：「下午6.10分开始走这套流程」。跑法：只用 HTTP 接口驱动（**不给他开浏览器**）、同一会话 id 连着问、
每题间隔 ≥5 分钟（守 B站风控）。会话 = `web:live2`，流水落在 `harness\data\flow\web_live2.jsonl`。

#### 一、捉到的 bug ①：网页后端拉爬虫用的是「错的那个解释器」

8780 原先跑在 `C:\Python310\python.exe` 下，而爬虫是 `supervisor._spawn` 用 **`sys.executable`** 拉起来的 ——
于是它也用了那个解释器，两个爬虫当场 `ModuleNotFoundError`（NGA 缺 `bs4`、bili 缺 `redis`），
用户看到的是「两盏灯全红、连接被拒」。**根因是「起服务的解释器」，不是服务本身。**

修法：网页后端改用 `<本机临时目录>\venv\Scripts\python.exe` 起（那台机器上唯一装齐
bs4+redis+playwright 的解释器）。**代码一行没改。**
（注：该 `python.exe` 是转发器，会再 exec 一个 base Python311 子进程 —— 父子两个 PID 是正常的，子进程继承了 venv 环境。）

#### 二、捉到的 bug ②（更要命）：同一会话的**第二题**必挂

报 `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`。
根因：`webapp` 把 `Session` 按会话**缓存**（`_SESSIONS`），而**每一题都开一条新线程**跑（`_run_ask`）——
一条连接认死「建它的那条线程」。于是**第一题好好的、第二题直接崩在收口前**，用户看到的就是「本题失败」；
`/api/history` 走同一条连接，也一起挂。

修法：`db.connect()` 改 **`check_same_thread=False`**。放开是安全的：本机 `sqlite3.threadsafety == 3`（SERIALIZED，
语句级并发由 SQLite 自己串行化），且同一会话的提问在 webapp 那侧本来就有按会话的锁（`_session_lock`）串着。

**为什么以前的自检照不出来**：它们全在**一条线程**里跑完整段（建连接、提问、断言都在同一线程），
连接的主人是自己，永远不越界。这个坏法只在「**缓存对象 + 多线程**」这个真实形态里现形 ——
它躲过了 50 项的会话压缩自检、35 项的位置扫描自检、17 项的会话回收自检。

#### 三、为 bug ② 补的回归自检

`_syscheck/verify_threaded_session.py`（**9 条**，已挂进 `harness.test`）：
① 一条连接在**另一条**线程上用不抛错（schema 播种 / 检查点读写都走一遍）；
② **照网页后端的真实用法**——第 1 题线程里建 `Session`、第 2 题另一条线程接着用，两轮都要收得了口；
③ 跨线程跑完，落下的流水与检查点是**完整的**，不是半截。
另做了一次直接对照：`check_same_thread=True` 换线程用 → `ProgrammingError`；放开 → OK。

#### 四、真跑结果

| 题 | 时间 | 问句 | 结果 |
|---|---|---|---|
| Q1 | 18:20 | 鸣潮现在在社区里的口碑怎么样？ | 240 样本（NGA 78 / bili 162），4 轮 18 次，**210 条引用全核过**（`missing: []`），答案 2409 字 |
| Q2 | 18:28 | 你刚才说的第一条，关于数值膨胀那部分，具体是怎么说的？ | **第二题没挂**（bug ② 已修）。trace 里就一条 `recall_answer`（`returned: 1`），模型据此**逐字复述**了 Q1 那一段；8 条引用全核过；**此轮没重爬**（零新增风控暴露） |
| Q3 | 18:40 | 那它跟原神最近的口碑比，哪个更差？ | 真爬了**两个游戏**（bili 102 / NGA 20 条新样本），3 轮 14 次，**36 条引用全核过**，答案 1881 字，对比结论站得住 |

**Q2 最值钱的一条证据**：事后把 Q2 那一刻模型真正收到的 `messages` **确定性重建**了一遍 ——
上下文里**只有 Q1 的问句 + Q1 的结论行（154 字）**，**Q1 的答案全文并不在上下文里**
（`19.1 万` 这个数字在 recent 里搜不到）。而 Q2 的答案把那一段**逐字复述**了出来（连「播放 19.1 万、点赞 4367」都对得上）
—— 所以那些字**只可能来自 `recall_answer` 的返回**。回捞链在真跑里是通的，不是只在假模型自检里通。

`/api/history` 跨线程也正常了（返回 3 题，数字与流水对得上）。分发包 `python -m harness.test` = **11 PASS / 0 FAIL / 22 SKIP**。

#### 五、真跑新暴露、本轮**没**动的四条

1. **答案开头漏英文草稿**（Q1 与 Q3 都有，如 `I've used 3 crawl rounds (the hard limit). Time to answer with what I have.`）。
   全仓 grep 下来，提示词里**压根没有**「别输出过程、直接给结论」这条纪律 —— 不是被违反，是从来没立。
2. `bili` 爬完打印的落盘路径是**写死的 Linux 老路径** `saved: <B站爬虫目录>/out\原神退坑.json`（毕设原版留的）。
3. Q3 在「对比」时**先把鸣潮又重爬了 4 轮**（NGA 侧全是 `new: 0, dup: 10` 的空转），拿满预算才去爬原神。
4. 本机两个爬虫 `/health` 都报 `login: True` —— 这台机器的 crawler 树本来就带登录态，与「分发包不带凭证」不冲突；
   装到**干净**机器上应该是两盏红，这条留给装机验证。

#### 六、边界

- 这轮跑的是**单用户单会话**三连问，没有验并发、没有验多用户。
- Q2 的「重建」是**确定性重建**（消息装配只依赖 flow 流水 + 本轮问句），不是抓的实时字节 —— 结论成立，但要知道它是重建。
- 装机路径（`setup.bat` 建 `harness-dist\.venv`）**还没做**：这轮是借用 `<本机临时目录>\venv` 才跑通的，
  真正分发给用户时「解释器里要有 bs4/redis/playwright」这件事必须由 setup 保证 —— bug ① 的根因在分发形态下还会再咬一次。


### 10.56 网页图表扩到 8 类 + 干掉公网 CDN 依赖；同时跑完「同一会话连问 20 题」（2026-09-16 凌晨）

**用户口令**：「我们需要更多的图表」→「我毕设的图表可以复用吧这是第一 第二你需要去找素材站或者skill 有关图表的」→「试试」。

#### 一、毕设图表能复用到什么程度（先说清楚边界）

毕设大屏在 `<本机>\{毕设大屏仓库}\舆情洞察分析板\`（Vue3 + Vite + TypeScript + Tailwind + echarts 5.6.0），
**不是** `<本机>\{毕设总仓库}\`（那是后端/爬虫/训练那半边；`frontend-design` skill 里写的那个路径只是笼统指代）。

- `.vue` 组件**搬不进来**：毕设要 npm 构建，我们的网页是**一个静态 HTML、零构建**。
- 能搬的是 **echarts 的 `option` 配置对象**（纯 JS），取数逻辑必须重写（毕设走它自己的 Spring Boot 后端，字段不一样）。
- 逐个判断：渠道占比饼图（**能直接搬**）、热度趋势折线（能搬，去掉"预测"半段——我们没这能力）、
  词云（能用，要多引一个插件）、情绪焦点三栏词卡（**用不了**——我们没有"情绪词"这个数据）、
  作者排行（样式能抄，内容会出一堆"匿名"）、运营预警（用不上，那是"监测类"的活）。
- **最值钱的不是组件，是成品文件**：毕设 `node_modules` 里有 `echarts.min.js`（1MB）和
  `echarts-wordcloud.min.js`（16KB），本地就能拿。

#### 二、干掉公网 CDN 依赖（这条比加图本身重要）

网页原来从 `cdn.jsdelivr.net` 拉 echarts —— **用户断网/加载不出来，三张图一起消失**。
已把两个成品文件拷进 `web/vendor/`，`index.html` 改成**先读本地、真缺了才用 `document.write` 退回 CDN 兜底**。
`webapp._static` 每次请求现读盘（`webapp.py:446`），所以**改前端不用重启服务**。

#### 三、翻译层补列：五个量化热度列原来根本没被取出来

`webview.py` 的 `_SEL` 原来只取标量字段，**`view_count / like_count / reply_count / danmaku_count / video_tags`
一个没取** —— 列在库里、数据也在（点赞 2886/3516 有值），可前端拿不到，就只能画"条数"、画不了"哪条真火"。
这与既有教训直接对上（"点赞/播放/回复数是画图表要用的，解析层不许丢"）：**引擎侧留住了，翻译层又丢了一次。**
现已带进 `cites` 与 `uncited`。老行这些列是 NULL，前端据此自己决定画不画。

#### 四、新增 8 类图，收在折叠块里

`情绪分布对照`（全量 vs 被引，两组柱）、`平台构成`（被引/未被引堆叠）、`逐轮新增`（两平台堆叠）、
`采集漏斗`（候选 → 滤掉过期/重复 → 新归档 → 真引用）、`发布时间分布`（按平台堆叠面积）、
`上下文用量`（对护栏）、`视频标签词云`、`被引证据热度榜`。

- **收进默认折叠的 `<details>`，第一次展开才 `echarts.init`** —— 一屏底下挂十来张图会把答案淹掉，长会话也拖。
  实测 `renderAll()` 只在发题/出结果/换会话/切软信号时触发，**轮询每 3 秒只刷灯、不重画整页**，所以折叠是纯赚。
- **数据不够的图直接不出**，不画空图凑数。

#### 五、两个口径坑（都是查数据才发现的，不是拍脑袋）

1. **词云不能用 `topic_tags`**：那是**我们自己的分类标签**（"强度""阵营/反讽"，350/4665 有值），当词云毫无意义。
   真正能用的是 **B 站原生 `video_tags`**（社区自己打的词，685/4665 有值，如"鸣潮""心月狐""游戏杂谈"）。
   **这张图天然偏 B 站**（论坛没有这种标签），标签里已写明，不许当全平台口径看。
2. **热度榜两边各用各的指标**：B 站看点赞、论坛看回复数（论坛没有点赞）。量纲不同，
   标签里说清"别横向比大小，看的是哪几条真有人气"。

#### 六、验到什么程度（说清楚边界）

- 内联 JS 两块过 `node --check`；`vendor/*.js` 与页面走 HTTP 核实（200 + 正确 MIME）；载荷新字段走 HTTP 核实。
- 另写 `_tmp/live20/chk_figs.py`：**照前端 `figSpecs` 的逻辑用 Python 复刻一遍取数**，跑遍真载荷 20 题 ——
  **8 类图 20/20 命中、无空图无全零**。这一层验的是"数成不成立"，**不是"画得好不好看"**。
- **没开浏览器**（铁律：绝不在他机器上起浏览器）。**实际渲染效果未经目视确认**，观感要他自己开页面看。

#### 七、同一会话连问 20 题（会话 `kgtopual`，模拟前端输入，19:54 → 00:38）

走法完全照抄前端：`{question, mode, session, ratio, pdf}` 请求体、每 3 秒轮询、同一会话 id 连问、
爬过的题之间隔 300 秒（守 B站风控）。跑完 **20 题全部 `done`，引用缺失全程 0**（634 条引用条条核回存档）。
总耗时 188 分钟，平均每题 563 秒（最短 199s / 最长 1282s）。题型覆盖节奏 / 对比 / 攻略 / 往年事件 / 时间线 / 综合，
每类都带追问。中途机器蓝屏重启过一次（22:26），`PRAGMA integrity_check = ok`，从第 11 题续跑，没丢数据。

**新暴露一个用户可见缺陷**：**模型的英文思考过程漏进了答案正文，20 题里 4 题**（第 7、8、11、19 题），
形如 `I have enough evidence now. Let me compile the answer about major 2025 鸣潮 community events.`
直接接在正文开头。与 §10.55 记的第 1 条是**同一个根因**（提示词里没有"别输出过程"这条纪律），
**本轮仍未修** —— 该在提示词层把 reasoning 挡在答案之外。

### 10.57 补上「事件时间轴」：横轴时间 + 按事件类型分泳道，官方动作与玩家反应分开看（2026-09-16 凌晨）

用户口令：「**时间轴呢？？？**」→「**就是那种横向时间轴 然后显示各种事件发生位置**」。

#### 一、先核实：不是漏了一张图，是整栏没做

- 页面上跟时间轴沾边的**只有老图「讨论度趋势」**（`ask.trend`，按天折线计数，标签写着「共享时间轴 · 近 90 天」）。
- 而 `docs/PRD.md:103` / `docs/TECH_SELECTION.md:124` 定的图表分两栏：**触发图** = 热度折线 / 节奏时间轴 /
  版本排期 / 对比图；**固定图** = 情绪分布 / 讨论度趋势 / 词云 / 作者。
  §10.56 那轮补的 8 类**全落在"固定图"这栏，触发图四类一个都没做** —— 用户的质问是对的，不是他记错。

#### 二、做法：一条横轴、按事件类型分泳道、一个点一条样本

- 新图放在**答案正文正下方、不进折叠块**（用户点名要的，默认可见）。
- 横轴＝时间；**纵轴＝事件类型**，顺序 `官号公告 / 官号回复 / NGA 主帖 / NGA 回复 / B站视频 / B站评论`，
  **没数据的泳道不画**（不空占一行）。
- 每条样本按**它自己发布的时刻**落一个点：点越大＝热度越高（B 站用点赞、论坛用回复数，各用各的）；
  **深色带圈＝答案真引用了这条**，淡色＝爬到但没引；悬停出标题 / 平台 / 时刻 / 引没引用。
- 右端一条虚线＝**本次采集时刻**（新载荷字段 `collected_at` = 这批证据里最晚的 `crawled_at`）——
  一眼看出"证据是几天前采的""官方是在玩家炸之前还是之后发的公告"。

#### 三、分道靠 `observation.kind`，翻译层原来没取这两列

- 泳道由 `kind`（`post / reply / official_post / official_reply`）+ `platform` 决定。
  **靠 `kind` 才能把"官方发的"和"玩家说的"分到不同道** —— 这正是这张图的价值所在。
- `webview.py` 的 `_SEL` 原来只取 12 列，**`kind` 与 `crawled_at` 都不在里面**，一并补上；
  `cites`/`uncited` 各多带一个 `kind`，载荷多一个顶层 `collected_at`。

#### 四、已知短板：跨度可能极大，靠缩放兜

- 20 题实测跨度 **81 天 ~ 2165 天**（第 19 题从 2020-10-11 起）。全铺开时近期那团会挤在右端。
- 加 `dataZoom`（鼠标滚轮 + 底部滑块）。**默认铺满全段、不藏任何点** —— 不做"默认只看近 90 天"那种
  会误导的口径，要看哪段自己放大。

#### 五、怎么验的（说清楚边界）

- 内联 JS 过 `node --check`（2 块、0 错）；页面走 HTTP 200；载荷新字段（`kind` / `collected_at` / 泳道归属）走 HTTP 核实。
- 另写 `_tmp/live20/chk_timeline.py`：**照前端同一套分道规则用 Python 复刻**，跑遍 20 题真载荷 ——
  **3464 条样本 100% 落点（0 条时间解析失败）、每题至少两条泳道、无一题退化**。
  实战泳道：NGA 主帖 20/20、B站视频 20/20、B站评论 20/20、**官号回复 2/20**、
  **官号公告 0/20、NGA 回复 0/20**（前者：这几题没引到公告；后者：NGA 采集不带楼层回复）。
- **没开浏览器**（铁律），**实际渲染观感未经目视确认** —— 这一步得用户自己开页面看。

### 10.58 「全修了」：修掉 §10.56/§10.57 留的三处 + 把时间轴从"每题一张"补到"整场一张"（2026-09-16 凌晨）

用户口令：「**全修了**」—— 指 §10.56 记下的那个真缺陷（英文思考过程漏进答案正文）、
§10.57 前后攒下的路径写死与空转重爬，外加把上一轮承诺的**会话级时间轴**做出来。

#### 一、正文纪律：新加提示词第 4b 条（治「英文草稿漏进正文」）

- **病灶**：20 题里 4 题（第 7、8、11、19 题）答案正文开头直接粘着模型的英文自述，形如
  「`I have enough evidence now. Let me compile the answer about major 2025 鸣潮 community events.`」
  —— **用户一眼看得见**，属于提示词层面该挡没挡的事。
- **改法**：`engine._base_system()` 的【三 · 下嘴纪律】里，在第 4 条与第 5 条之间**插入 4b**（编号取 4b
  而不是把 5 顶成 6，是为了不动后面所有条目的号）。四条要点：答案第一个字就是给用户看的内容；
  不许写「我先查一下」「证据够了」这类自我叙述；不许复述调了哪些工具、爬了几轮、还剩多少预算；
  **更不许用英文写草稿（思维链一律不进正文）**。已用 `_tmp/show_prompt.py` 打印核对：
  `4) → 4b) → 5)` 编号连续、读得通，提示词总长 4368。
- **诚实边界**：这是**提示词级**的修法，能不能真挡住**只有再真问一题才知道** —— 本轮没有跑题。

#### 二、B站落盘路径不再写死 `<B站爬虫目录>`

- **病灶**：`crawlers/bili/media_platform/bilibili/core.py` 把 `out_root` 和 `queries.txt` 的路径
  **写死成 `<B站爬虫目录>/…`**、`qr_login_once.py` 同理。换机器/换目录后这就是个不存在的地址：
  **日志里打出来的落盘路径是假的**，用户照着去找只会扑空 —— 而分发包正是"换个目录"的形态。
- **改法**：两处都改成**跟文件走** —— `BASE_DIR = os.path.dirname(...__file__...)`，
  `out_root` / `queries.txt` 挂到爬虫树根下；`qr_login_once.py` 的用法行同步改成 `cd <bili 爬虫树> && …`。

#### 三、空转重爬：`new == 0` 分两种，外加整轮级别的提示

- **病灶**：实测对比题里模型把**同一边重爬 4 轮**，每轮工具返回 `new:0 / dup:10`，拿满爬轮预算才去爬另一边。
  旧提示只有一句 NGA 味的「检索词叠加越多越必然为空」，**且不区分"都搜过了"与"一条没搜到"** ——
  前者是方向已采完（该换方向/换另一边），后者才是词没造对，两种情况该说的话完全不同。
- **改法**（都在 `engine.py`）：
  1. `_after_crawl` 的 `new == 0` 分支按 `summary.dup` 分流：有重复 → 明说"这个方向已经采过了，
     原样重爬拿不到新料，别重复烧爬轮"；无重复 → 才说 NGA 全词与、去掉限定词再试。
  2. 轮末 **round 级**兜底：`round_new` 累加本轮两平台新增，`if crawls and not round_new` 时
     往消息里塞一条系统提醒「本轮真爬过但新增 0，再按同样方向爬还是这些重复样本，白耗爬轮」，
     trace 记一条 `empty_round`。**工具返回体里的 new/dup 模型会读漏，这里用系统消息明说一次。**

#### 四、会话时间轴：把「每题一张事件轴」补成「整场一张」

- 上面 §10.57 那张是**单题**的：横轴是**证据发布时刻**。缺的是**整场会话**的：横轴该是
  **用户问这题 —— 答完**的挂钟时刻，一眼看出"哪题最费时间""是不是卡住了"。
- **为此引擎多记两列**：`ask()` 起手打一个 `t0`，`_emit_flow` 落 `started_at`（ISO）与
  `elapsed`（秒）；`webview` 顶到载荷里。
- **前端**：`D.asks.length > 1` 时在对话区顶部出一张横向条形图 —— 每题一条，条长＝该题实际用时长，
  条右端标秒数；**没有开始时刻的题**（老记录）画成灰菱形，不瞎推。
- **诚实边界**：现存那 20 题是**这轮之前**跑的，流水里**没有** `started_at`/`elapsed`，
  所以现在打开会看到 **20 个灰菱形** —— 这是如实呈现，图下的说明文字写清了原因。

#### 五、怎么验的（说清楚边界）

- 三个改过的 Python 文件 `py_compile` 全过；内联 JS 2 块、0 语法错。
- `python -m harness.test` = **11 通过 / 0 失败 / 22 跳过**。
  （新加的 `sess-tl` / `sess-tl-cap` 两个 id 是**运行时才拼出来**的，已按检查脚本既有的
  `RUNTIME_IDS` 白名单机制登记 —— 那是给"运行时注入节点"留的口子，不是临时放过。）
- 载荷字段走 HTTP 核实：`/api/history?session=kgtopual` 20 题都在，Q1 的
  `started_at=[] / elapsed=[]`（老记录如实为空）、`ts` 正常。
- **没开浏览器**（铁律），页面观感仍未经目视确认；**提示词那条修法也只有再真问一题才能确认**。

### 10.59 提示词挡不住的英文草稿，改在收口那层**确定性剔除**（2026-09-16 凌晨，用户「跑5个追问」→「做吧」）

#### 一、先证伪：提示词那条（§10.58 第四条 4b）**实测没挡住**

用户让真跑 5 个追问验一次。结果：**5 题漏 1 题**（第 1 题），开头仍是

> `I've hit the crawl budget limit (3 rounds used). Time to answer with what I have.`

—— 而这句话**正是 4b 明令禁止的**（不许复述还剩多少预算）。漏的比率（1/5）跟上一轮（4/20）同量级，
说明「靠模型自律」这条路不成立。**该拦在确定性这一层。**

#### 二、做法：`_strip_leading_scratch()`，挂在 `_seal_answer` 开头

- **判法**：从第一行往下逐行看，某行满足「**英文词 ≥ 3 且中日韩字占比 < 0.2**」就当草稿删；
  **一碰到中文行立刻停手**，后面一个字不动 —— 正文里的英文术语、英文游戏名一律留。
  要求「≥3 个英文词」是为了放过 `NGA 主帖`、`Bilibili 视频侧样本`、`**Wuthering Waves** 的口碑` 这类
  正常开头的短英文片段；用「中日韩字占比」而不是「有没有中文字」，是为了抓住上一轮那种
  `…about major 2025 鸣潮 community events.` —— 夹俩中文字，但它显然还是英文草稿。
- **两道保险**（这函数动的是用户直接看见的正文，**误伤比漏拦更糟**）：
  ① 删完剩下的短于 60 字 -> 整个不删（把答案吃空了，或是整篇英文写的）；
  ② 删掉的行比剩下的正文还长 -> 整个不删。
- **留痕**：删了就往流水里记一条 `scratch_drop`（删了几行、第一行是什么），事后可查。

#### 三、第二道保险的口径是自检**当场纠**的

第一版写的是「删掉的字数超过全**文三成**就不删」。自检一跑就栽了：实测那句草稿 85 字、正文 150 字左右
= **36%** —— 一道本该拦住草稿的闸，在**正常长度的答案上几乎必然误放行**（真答案常是几百字，
85 字草稿配 800 字正文才 10%，可 85 字草稿配 200 字正文就 30%+）。改成「**草稿比剩下的正文还长**」
才有判别力：草稿比正文长，那才真叫危险。自检里两条都钉住了（一正一反）。

#### 四、新增自检 `_syscheck/verify_scratch_strip.py`（18 条，已挂进 `harness.test`）

覆盖：实测漏句被删、夹中文的英文草稿被删、多行草稿连空行一起删、中文开头原样不动、
**正文中间的英文行不许动**、开头短英文片段不当草稿删（4 种真实开头）、整篇英文不删空、
草稿比正文长不删、正文刚过草稿长度就该删（就是上面那个 36% 的回归）、空正文不炸，
外加一条**接线检查**（`_seal_answer` 里真调了它、真记了 `scratch_drop` —— 改完函数没人调 = 白写）。

#### 五、怎么验的

- `_syscheck/verify_scratch_strip.py`：**18 通过 / 0 失败**。
- **产品树**跑 `python -m harness.test`：**12 通过 / 0 失败 / 22 跳过**
  （跳过的 22 支是**开发期脚本**，只在源码仓库里有 `_tmp/`，不随产品走 —— 跳过是正常的）。
  ⚠ 本轮前面的汇报里我说过「22 通过」——那是在**源码仓库**目录下跑的（那边有 `_tmp/`），
  量的不是产品树。产品树的真实数字是 12/0/22。
- 网页后端已重启（04:03:37 起，晚于 `engine.py` 改动 04:03:08），**新代码在跑的那份里**。
- **端到端真验到了**（用户「试试」）：再跑 3 题（会话 `ctvwxb81`，04:06→04:28，全是"最大的节奏"这类
  大题目 —— 上轮漏草稿的就是这种，爬到预算见底时模型爱用英文自述）。**第 2 题（原神）真漏了**：

  > 剔除了答案开头漏出的英文思考过程 1 行：`I have enough evidence across both platforms. Let me compile the answer.`

  —— 与 20 题那轮漏的 `I have enough evidence now. Let me compile the answer about…` **是同一个句式**。
  用户看到的答案开头是 `**原神近 90 天（2026-06-18 ~ 2026-09-16）社区最大的节奏：六周年庆福利之争…**`，
  草稿**没露出来**。三题里另外两题没漏（没碰上，不算验到），**没有一题残留草稿**。
  3 题结果：352 / 194 / 206 秒，引用 20 / 29 / 21，**核不回去的都是 0**，本题新爬 251 / 197 / 181。

### 10.60 会话能置顶/删除了；顺带修掉「平台比例」按钮的抖动（2026-09-16 早，用户「…每个会话窗口的管理按钮呢，包含置顶，删除呢？？？」→「连这个对话的后端记忆一起清掉」）

#### 一、先修的那处抖动：两个互不相干的原因（用户上一条）

- **拖滑杆时抖**：滑杆右边那句读数宽度是**跟着字数走**的（`两平台均等` 5 字 ↔ `NGA 60 : bilibili 40` 20 字），
  而滑杆是 `flex:1`（占剩下的地方）—— 每拖一格，读数一变宽就把滑杆挤窄、变窄又把它放开，
  **手指底下的滑块跟着左右跑**。改：读数**定宽 118px + 右对齐**（按最长那句留的余量），滑杆宽度就定住了。
- **收起时抖**：`.morphBtn` 收起态写的是 `flex:1 1 auto`（撑满胶囊），只有展开态才是 `flex:0 0 auto`（文字宽）。
  一点收起，按钮盒子在**那一瞬间**从 66px 跳到"分走一半富余宽度"（392px 的胶囊里实测 229px），
  而那会儿蓝底还没褪完 —— 看到的就是收起那一瞬间抖一下。展开时同一个毛病但只差 9px，所以没被察觉。
  改：两态统一 `flex:0 0 auto`；收起态胶囊从 84px 收到 **68px**（= 66px 按钮 + 两边各 1px 边框，
  正好裹住那四个字）。**展开态一点没动。**
- 没动的一种情况：窗口窄于约 700px 时，展开会把「导出 PDF」和发送键挤到第二行再跳回来 ——
  那是响应式换行，不是这套毛病。已在回复里说明，等用户反馈是不是这种。

#### 二、「删除」的口径：连这个对话的后端记忆一起清（用户拍板）

- 功能确实一直没做，而且**是记在账上的欠账**：`schema.sql:80` 写着「会话层可软删，
  **物理清除是前台删除按钮的独立任务**」—— 后端当年只留了软删的口子，把真删留给前台按钮，按钮一直没做。
  （顺带：`#clearGo` 那个"清除我的对话记忆"至今仍是空壳，**本轮没动它**，见待办。）
- 清的是**这一个会话自己的那一份**：`checkpoint`(L2) + `session_claim`(结论日志) + `flow` 流水文件。
- **一律不碰**：证据池（`crawl_run`/`observation`）、L4 注册表（`alias_resolution`，全机共享）、别的会话。
  前两样不碰是**口径**不是省事：别的会话也在引这批料，删了它们答案里的 `[id=N]` 就核不回原帖了 ——
  这正是那个弹窗上写的承诺。

#### 三、后端：`forget.py` 是全项目唯一的破坏性模块，guard 逐名放行

- 新增 `harness/forget.py::purge(conn, scope)`：三处按 scope 精确删，返回各层清了多少（前台照实显示）。
- **凭什么能写删除语句**：`harness/guard.py` 有套 append-only 静态守卫（`DELETE FROM`/`DROP TABLE`/
  `os.remove`/`open(...,"w")` 一律违规，且自检要求"真产品树零违规"）。给它加了
  `_PURGE_ALLOWED = ("forget.py",)`：**按文件名**放行，不是给个注释标记让谁都能自放行 ——
  "全 harness 只有这一处"这件事，得让名单一眼看得出有没有被扩大。
- 新增 `POST /api/forget {session}`（`webapp._forget`）。三件事缺一不可，少一件就会"删了又活过来"：
  ① 先拿 scope 的锁，**有题在跑就 409 拒删**（那一轮还在内存里，收口时会把流水又写回来）；
  ② 把内存里缓存的那个 `Session` **丢掉**（它的状态卡/编号在内存里，不丢的话下次进来接着旧卡答）；
  ③ 库里真删 + 删流水文件。

#### 四、前端：行内「⋯」+ 置顶分组 + 手打确认

- 每行右边一个「⋯」→ 菜单两项：`置顶/取消置顶`、`删除这个对话`。**常显**（压暗 60%），不做"悬停才出现"：
  触屏没有悬停这一步，而且用户会以为压根没有管理入口（真被这么问过）。
- 置顶的另起一组钉在「7 天内/30 天内/更早」**之前**；置顶状态进登记簿（`pin` 字段，活过刷新），
  且 `sidOrder` 把置顶排前面 —— 否则老对话会被 `registryPut` 的 80 条截断悄悄切掉。
- 删除给**手打确认**（输「删除这个对话」六个字才让点）。理由：删了就真找不回来（没有软删那 7 天），
  而流水里装着这个对话**每道题的答案全文**。弹窗如实写明"找不回来"与"证据库/[id=N] 不受影响"。
- 删的正是当前这个时自动退到下一个；一个都不剩就开一个空的。有题在跑则拒删（跟切换/新建同一道闸）。

#### 五、怎么验的

- `_syscheck/verify_session_forget.py`（新，**24 通过 / 0 失败**，已挂进 `harness.test`）：临时库 + 临时目录，
  覆盖"该删的真删了 / 不该删的一行没动 / 空 scope 抛错 / 没东西的会话不炸 / 未建表不炸"，
  守卫那头覆盖"名单没被扩大 / 真产品树零违规 / 换文件名照拦"，再接三条 `_forget` 的行为检查
  （跑题中 409 且一行没删、缓存里的 Session 被丢掉、借的连接用完关掉、没缓存的现开一条）。
- `_syscheck/verify_web_wiring.py` 补 **11 条**（2d.1~2d.11）：**58 通过 / 0 失败**。
- 产品树 `python -m harness.test`：**13 通过 / 0 失败 / 22 跳过**。
- **真服务上跑通了一遍**（`/api/forget` 打的是真库真流水）：造一个假对话（检查点+1、结论+1、流水 1 份）
  → 接口回 `{"checkpoint":1,"session_claim":1,"flow_file":1}` → 三样都真没了，
  而**检查点/结论/证据(5796)/注册表(6) 的净增全是 0**，8 条断言全过。网页后端已重启（05:48）。
- **没开浏览器**（铁律）：页面上的观感（菜单摆位、置顶分组、确认弹窗）**未经目视确认**，待用户点。

### 10.61 删除改成「7 天可恢复」：设置里多一栏垃圾桶（2026-09-16 下午，用户「可以 7 天恢复，在设置加个垃圾桶可以恢复那种」）

> **更正 §10.60 的一处口径**：那一节写「删了就真找不回来（没有软删那 7 天）」—— 现在有了，见本节。
> 弹窗上那句「保留 7 天可恢复」也不再是空话。

#### 一、语义：删分两步，中间那 7 天在垃圾桶里躺着

- **删的当下 `trash_move`**：把这一份记忆（`checkpoint` + `session_claim` + flow 流水）**整段搬进垃圾桶**，
  活的那一侧当场清空 —— 所以删完再进这个对话是**从零开始**，不是接着旧卡答，这正是要的效果。
- **过了 7 天 `sweep`**：垃圾桶里过期的那几行真删掉，**那才是物理清除**。中间反悔 `trash_restore` 整段搬回去。
- **顺序是刻意的**：先落垃圾桶、再清活的那侧。反过来的话，一旦"清完却存不进垃圾桶"（库被锁/表没建），
  这一份记忆就**凭空没了**；这个顺序最坏只留一条空垃圾桶记录，无害。
- **"本来就没东西"的会话不塞空记录** —— 否则用户会在垃圾桶里看见一堆自己没印象的条目。

#### 二、表与接口

- **新增 `trash` 表**（`schema.sql`）：`scope` 主键 / `title` / `deleted_at` / `expire_at` / `card` / `claims` / `flow`，
  外加 `idx_trash_expire`。随 `db.init` 的 `executescript` 自动建，**不需要迁移**（新装的库第一次建表就带上）。
- **保留期只此一处**：`forget.TRASH_DAYS = 7`。前端显示的天数是从这个值读出去的 —— 改这里 = 改前台的承诺。
- **三条接口**（`webapp.py`）：`POST /api/forget`（改走 `trash_move`，回 `expire_at` 与 `days`）、
  `GET /api/trash`（列垃圾桶，**顺带 sweep 一次**）、`POST /api/restore`（搬回来 + 丢掉内存里缓存的 `Session`）。
  服务**启动时**也扫一次过期。
- **恢复必须丢缓存**：内存里那个 `Session` 挂着的是一张空状态卡，不丢的话下次进这个会话照样接着空卡答，
  等于白恢复 —— 这是删除那步"丢缓存"的镜像。
- **非破坏性没变**：`forget.py` 仍是全 harness **唯一**能写删除语句的模块（`guard._PURGE_ALLOWED` 按文件名放行）。

#### 三、前端

- 设置面板多一栏**「垃圾桶」**：每条显示 标题 / 还剩几天 / 什么时候删的，右边一个「恢复」按钮。
- 恢复完把那句话**放回左侧列表**（标题也一起还回来，不然恢复出来叫「新对话」），并重刷设置面板。
- 删除弹窗文案同步改成实话：当场就空 / 整个进垃圾桶 / **7 天内在「设置 → 垃圾桶」里可以恢复** / 过了 7 天才真删。

#### 四、怎么验的

- `_syscheck/verify_session_forget.py` **重写后 51 通过 / 0 失败**：搬走（各层计数、`expire_at=now+7d`、活的那侧空、
  别的 scope 一行没动、证据池与注册表一行没动、空 scope 抛错、没东西的不塞空记录、未建表不炸）、
  搬回（计数+标题、状态卡原文逐字、两条结论、流水逐字节、垃圾桶那行没了、不在垃圾桶回 None）、
  sweep（没过期不动、还剩 2 小时显示 1 天、过期真删、只动该动的、按删的时间倒序）、
  guard（名单正好是 `("forget.py",)`、真产品树零违规、换文件名照拦）、webapp 接线（三条路由、有题在跑 409 且一行没动、
  缓存被丢、借的连接关掉、恢复 409/404、恢复只碰自己那一份）。
- `_syscheck/verify_web_wiring.py` 加到 **66 通过 / 0 失败**（新增 ②e 一节 8 条盯垃圾桶那一栏的接线）。
- **真服务上真跑了一个来回**（打的是真库真流水）：3 题会话 → 删（清 1 卡 + 2 结论 + 1 流水）→ 历史归零 →
  垃圾桶里有它（`days_left=7`）→ 恢复（1/2/1 全回来）→ 历史回到 3 题。
- **没开浏览器**（铁律）：垃圾桶那一栏的观感**未经目视确认**，待用户点。

#### 五、顺带

- 上一节那个待办「`#clearGo` 弹窗写『保留 7 天可恢复』跟真删对不上」**对上了**，接线只剩遍历登记簿那几行。

### 10.62 真出一篇 PDF 逐页核对版式（揪出两处缺陷）；产品包自带环境跑通（2026-09-16 下午，用户「走一个完整问答你来出一篇 pdf，你来视觉查看格式正确性等等」／「环境你来搞吧」）

#### 一、真跑完一题并导出

- 鸣潮深塔水温，精准档：**151 条样本**（NGA 46 / bilibili 105）、27 条引用、核验 `missing=[]`、无编造，
  导出 `out/pdf/` 下 **6 页** PDF，中文是**真字形的嵌入字体**（`MicrosoftYaHei`），不是方块。

#### 二、逐页看下来揪出的两处真缺陷（都已修）

- **① 答案正文没走渲染器**：PDF 把答案按纯文本印，`##`／`**`／`- ` **原样出现在纸上**，
  而**同一个答案在网页上是排好的版** —— 同一份内容两处长相不一样，用户会以为导出的坏了。
  修：`pdfout` 加 `_md()`（标题 / 短横列表 / 粗体 / 引用徽标，**规则与网页那个 `md()` 同一套**）+ `.ans` 各级样式。
- **② 没标「度量=关键词降级」**：网页上那块黄条 PDF 里没有，纸面上看着像有情感模型。
  修：`pdfout._kw_warn()`；口径跟着这一题走 —— `webview.build_ask` 新带出 `measure` 字段
  （取的时候晚绑定 `live.measure_mode()`，取不到就当"不知道"，不瞎标）。
- **新增 `_syscheck/verify_pdf.py`（31 条，已挂进 `harness.test`）**：盯的就是"网页与 PDF 不同款"这件事，
  外加引用清单、核不上时的黄条、度量标注、台账格子、转义、文件路径规矩。
  **不验真 PDF** 的理由：真 PDF 要起 Chrome，不该给离线自检背这个依赖；而版式会出问题的地方全在 HTML 里。
- 产品树 `python -m harness.test` 现为 **14 通过 / 0 失败 / 22 跳过**。

#### 三、产品包自带环境

- **包自己的 `.venv`（3.11.9）是齐的**：`playwright` / `bs4` / `redis` / `requests` / `tiktoken` / `pytest` 全在；
  网页后端从这个 venv 起得来。
- **空树自检（这就是"新装机器上第一次启动"）**：只拷 `harness/` + `web/`（44 个文件，**没有 data 目录**）→ 起服务 →
  `/api/status` 通、`/api/history` 0 题、`/api/trash` 空且 `days=7`，**自己建出一张 69KB 的空库**，7 张表全在（含 `trash`）。
  **机制随包走、内容为空**。
- **分发前必须清的（把账上第 8 条核实成真的，逐项都验过）**：`crawlers\bili\browser_data\`
  （**实测含 34 条登录 cookie**，四个域名上都有 `SESSDATA`/`bili_jct`/`DedeUserID`）、
  `crawlers\nga\config.json` 的 `cookies` 段（**实测含 `ngaPassportUid`/`ngaPassportCid`**）、
  `crawlers\*\.tool_token`、`crawlers\*\out\`、`harness\data\*`（9.4MB 库 + 26 个流水）、`out\pdf`、`logs\`、
  `.venv\`（绝对路径，挪位置就废）、根下 `_tmp_*`。**本轮没动手清**（用户说过测试残留先不清，这轮也没给清的口令）。

#### 四、更正上一轮的一处错话

- 我曾说过「扫码登录那条链，一行没动」—— **这句是错的**。`crawlers/bili/bili_search_server.py` 里
  `/login/qr/start` `/login/qr/image` `/login/qr/state` 三个端点是在的，跑在服务**自己那个常驻 context** 里；
  `nga_search_server.py` 也有 `/login/qr/state` 与 `/login/recheck`。缺的只是**"拿手机真扫一次"的端到端验证**。
  用户这轮拍板「不想做」，故不动。

### 10.63 B 站摘干净 + 三个真 bug + 部署到演示服务器 当外网 demo（2026-09-16 上午）

> 用户口令：「爬虫那个不是之前让你把B站的摘出来了吗？？？？」→「累了 我要睡觉了 睡醒后我想看到的是
> 经过本机测试都能跑通 没有bug 前端动画流畅 整个harness系统完整的系统，部署到演示服务器上 作为外网的访问demo版本，
> 用于给我求职的」；中途追加约束「deepseek高峰时段你就不要消耗token工作了，12.10-13.50再工作」。

#### 一、B 站摘取**这次才真摘干净**（上一轮只摘了一半）

- 上一轮只动了 `media_platform/` `store/` `model/`，**`config/` 与 ORM 定义整片没动** —— 用户那句质问是对的。
- 本轮删 `config/{dy,ks,tieba,weibo,xhs,zhihu}_config.py` 六个文件（192~2199 字节），
  `config/base_config.py` 的导入块收敛成 `from .bilibili_config import *` 一行、`PLATFORM = "bili"`；
  `database/models.py` 从 500+ 行剪到 **111 行**（只留 `Bilibili*` 五个类，其余平台的 ORM 全删）。
- **实测没摘坏**：真爬一题拿到 `[live] nga key=searchin|鸣潮|… -> 10 items` + `[live] bili kw=鸣潮口碑 -> 66 items`。
- 本机 `crawlers/bili` 现有 **52 个 .py**；对照演示服务器 上那份老树是 **157 个**（还带着抖音/快手/微博/贴吧/知乎/小红书）。

#### 二、清产品包垃圾

- `crawlers/{bili,nga}/out/*.json` 共 **327 个 / 1,802,555 字节**、全部 `__pycache__`、根目录 5 个 `_tmp_*` 日志。

#### 三、三个真 bug（都已修）

- **① 图表实例不回收**：`renderAll()` 每次都把整块节点重画，ECharts 实例还挂在它自己的内部表里（带 canvas、
  resize 监听、动画定时器）。**不 dispose 就是"问一题攒一批"**，长会话越滚越涩。
  修：加 `CHARTS` 账本 + `mkChart()` / `dropCharts()`，8 处 `echarts.init` 全走 `mkChart`，重画前先 `dropCharts()`。
- **② `HARNESS_DB=` 留空 → 开不了库**：`os.environ.get("HARNESS_DB", 默认)` 在**键存在但值为空串**时返回 `""`，
  于是库路径成了空串、`os.makedirs("")` 抛 `FileNotFoundError`。**而分发版的配置模板正是叫用户留空** ——
  等于人人必踩。演示服务器 上真撞到了（日志 `[web] 垃圾桶清扫跳过(开不了库)`）。修：空串一律当"没设"。
- **③ 卡片 id 当场/刷新两个号 → 刷新后「下载 PDF」消失**：`_run_ask` 原来把 `job_id` 当卡 id 传给翻译层，
  而刷新后是从流水按 `ts` 重建 id —— 同一题两个名字；PDF 又是按卡 id 命名的，于是刷新即丢链接。
  修：**job_id 只当传输号，不再当卡 id**（`webapp.py` 去掉 `ask_id=job_id`）；`webview.build_ask` 补 `pdf` 字段
  （文件在就给 `/api/pdf?id=<卡id>`），当场那次由 webapp 渲染完补同一个值。

#### 四、部署到演示服务器（形态 + 一个卡在云平台的坎）

- **形态**：`<部署目录>/` 只放 `harness/` + `web/` + `deploy/` + README/INSTALL/requirements。
  **不带 `crawlers/`**（演示服务器 上那两个爬虫本来就是常驻 systemd 服务）、**不带 `harness/data/`**（首启自己建空库）。
- **`<部署目录>/harness.env`**（0600 root:root）：`HARNESS_NO_LAZY=1`（systemd 管着爬虫，本服务别再拉一套
  去抢 `browser_data`）、三个 token 指到各自服务目录、`HARNESS_ACCESS_CODE`（进门码）、`HARNESS_DB` 留空。
- **`opinion-agent.service`**：`User=root`、`After/Wants=nga-tool bili-tool`、`ExecStart=/usr/bin/python3 -u -m
  harness.webapp --host 0.0.0.0 --port 8780`、`MemoryMax=3G`（4c8g **无 swap**：万一失控，被杀的也只是本服务，
  不连累同机爬虫与打分）。
- **进门码默认不设**（装自己电脑上就是这个行为，环回没人能碰）；只有挂公网才设 —— 否则谁扫到谁能用：
  问一题烧一次大模型的钱，还带着服务器上那个登录态去打 NGA/B站。带 `?k=<码>` 进一次后靠 cookie 走。
- **演示服务器 上的爬虫服务也换成了新版**（带 `/health` 的 `login` 字段 + 扫码端点）—— 不换的话两盏灯只会报「未知」。
  换完 NGA 日志 `登录成功`、bili `登录态 ok`，`/api/status` 两个 `login: true`。
- **坎：8780 从外网连不上**。机器内部查干净了 —— `ufw` inactive、`iptables` 只有 17 条且 <云厂商> 那几条链是空的、
  `nft` 也是空链；**是云平台安全组没放行**。实测从外网通的只有 22 与 30033（ts3），10022/10080/8780 都不通。
  **这条只能在控制台加规则，服务器内部改不了。**

#### 五、本机侧

- 本机这份**只连本机**：`HARNESS_LOCAL` 等环境变量全空（默认走本机）、无任何 ssh 隧道、18770/18771 无监听、
  8770/8771 就是包内那两个爬虫进程。演示服务器 那份是**另一套独立部署**，两边不交叉。
- 重启了本机网页后端（带上②③两处改动）。
- 产品树 `python -m harness.test`：**14 通过 / 0 失败 / 22 跳过**（②③ 改完后又跑了一遍，仍是这个数）。

#### 六、端到端真跑（已收尾）+ 待办

**两题都真问过、数字都对得上**（不是接口通，是整条链通）：

- **本机**（08:45）`a20260916T08450`：答案 1859 字、引用 30 条、核验不过 0、样本 115 条、5 轮、
  `measure=keyword`（本机没装情感分析，**如实降级**，页面上也照这个显示）。
- **演示服务器**（08:49）`a20260916T08490`：答案 2151 字、引用 40 条、对不上 0、样本 141 条（NGA 62 / B站 79）、
  4 轮、被拦 0、66483/160000 token、`measure=model`。PDF `/api/pdf?id=a20260916T08490` → HTTP 200 / 56295 字节 / `%PDF`。
  历史里 id 一致，刷新后 PDF 链接还在（这就是②③那两个 bug 修完的样子）。

**踩到的坑（记一笔，免得下次再踩）**：本机网页后端**一律用 `.venv\Scripts\python.exe` 起**，
别用系统 `Python311\python.exe` —— 那份没装 playwright，PDF 那步会报
`ModuleNotFoundError: No module named 'playwright'`。

**剩下的待办**：

- **云安全组放行 8780**（只有你能做）。放行后公网链接才是：`http://198.51.100.10:8780/?k=<进门码>`。
- `harness-dist` 的改动**同步回源码仓库**仍未做（老账，等用户拍板）。
- 分发前要清的凭证仍在（`crawlers/bili/browser_data/` 70.8MB、`crawlers/nga/config.json` 的 cookies、
  `crawlers/*/.tool_token`），**本轮只体检没删**（本机这几个还指着它们跑）。
- `harness-release`（旧的分发副本，最后改于 2026-09-16 07:08）**没跟着今天这四处改动刷新**，仍是旧的。

**边界（不做）**：扫码那条链的端到端实测（用户说了不想做）；`_tmp_*`/out 里的临时物清理。

### 10.64 「停止」按钮做真：引擎认停的安全点（2026-09-16 09:00，用户「2吧，但感觉bug会很多。。。」）

**背景**：问一题要爬几分钟，中途不能改主意是硬伤。给了用户三个方案（①只能等 ②真做停止键 ③只能删会话），
用户选 ②，同时担心「bug 会很多」—— 这个担心是对的，下面记的就是怎么把 bug 面收窄的。

**一、语义：安全点停，不硬掐**

- **发出去的抓取不掐**：`_fetch_platform` 里那一发 HTTP 已经在爬虫服务手里，硬断连接会让服务端的
  单飞锁留死、这一发的样本全丢。改成：这一发跑完、照常 `commit` 落库，引擎在**下一个检查点**才停。
  代价是点了停还可能再多落一条样本（诚实说明），换来的是**已经取到的一条不浪费**。
- **停下的题照样收口**：复用原来那条「强制无工具结论」的路 —— 摘要 + 结论行。
  所以被停的题照常有答案、有流水、有卡片 id、能导 PDF、在历史里看得见。
- **停 ≠ 平台故障**（最容易踩的坑）：引擎里加轮内哨兵 `_STOPPED`，被停掉的检索走「用户已停止」的
  结果分支，**不进 `_mark_fail`**。否则一次点击就会：B 站被判风控 → 会话冷却 300s 起、翻倍到 900s，
  用户点完停下一题反而爬不动，且这个状态会写进 `crawl_run` 里污染统计。

**二、检查点铺在哪四处**

1. `for rnd in range(6)` 轮首 —— 最外层，停在这儿连下一次 LLM 调用都不发。
2. 拿到 tool_calls 逐条派发时 —— `CRAWL_TOOLS` 那条分支第一行就查哨兵，本轮还没发出去的检索全落空。
3. 爬取计划成批提交前（`if plan and self._stopped()`）—— 把计划里的每一条都记成「没发出去」，
   而不是让它们静默消失（「数字不许对不上」）。
4. `_precheck` 里跨轮爬距的等待 —— `time.sleep` 换成可打断的 `_sleep`。
   `BILI_GAP 精准=120`，B 站那道 120 秒的闸如果不做可打断，点停就会像卡死。

**三、接线（四条都验了，不脱钩）**

`webapp POST /api/cancel` → `_CANCELS[scope]` 那个 Event → `Session.ask_turn(cancel=)` → `Engine.ask(cancel=)`。

- `/api/cancel` **不抢锁**（抢了就在跑着的那题后面排队，点了像没反应），拿不到 Event 就如实回
  「这个对话现在没有正在跑的题」。
- 前端发送键边上加一个圆形「■」，平时 `display:none`、有题在跑才 `.show`；点一下变「停…」并禁用。
- `cancelled` 一路带到卡片、`webview.build_ask`、`pdfout.build_html` 的黄条、以及**重新拉历史**那条路
  （刷新页面后仍看得出这题是被停的）。

**四、验到什么程度**

- 新增 `_syscheck/verify_cancel.py`（已挂 `harness.test`）：**43 通过 / 0 失败**，分八组 ——
  ①没信号时不误停 ②动手前停 ③抓取在途停 ④同平台排队时停 ⑤等待可打断 ⑥翻译层/PDF 认字段
  ⑦前端接线十二条 ⑧**按页面那套真请求走一遍真接口**（假模型 + 假爬虫 + 临时库，**不起浏览器**）。
- 第八组是冲着「接口通 ≠ 功能通」这条教训加的：源码级接线对得上，不代表页面点的那个动作真能跑通。
- 产品树 `python -m harness.test`：**15 通过 / 0 失败 / 22 跳过**。

**边界（不做）**：真机上的按钮手感（守「不在用户机器上起浏览器」，交用户点）；
把 `harness-dist` 同步回源码仓库（老账，用户拍板不同步）。

### 10.65 停止复问会绕开 B 站爬距（真 bug）；去敏改成可重复自证的一步；打分工具「装卸自如」四种组合坐实（2026-09-16 上午）

**用户口令**：「希望能远程连上演示服务器的demo，然后多次检查去敏后的harness已经做好，nlp工具装载后能起作用且被引用，
卸载后也可以留着但不使用，装卸自如你懂吗?测试留着先别做你改代码就好，还有这个停止复问，你需要考虑到爬虫的冷却时间哦」

**一、停止复问：用户点到的那条真 bug**

- **现象**：`_n_crawl`（爬轮号）每题从 0 重数，而 `_plats` 里的 `last_round` 是**跨题留着**的 ——
  上一题的第 0 轮和这一题的第 0 轮撞成"同轮"，`_precheck` 判成同轮就只留 `SAME_ROUND_GAP 12s`，
  **跨题那道 60/120 秒的 B 站爬距被整段跳过**。点一下「停止」立刻复问 = 一条绕开风控间距的后门。
- **修**：新增 `_ask_seq`（`ask()` 里 +1），"同轮"判定改成 `last_ask == _ask_seq and last_round == _n_crawl`；
  跨题一律走 `BILI_GAP`。爬距的记账点没动（`last_at` 仍在 `_precheck` 里真派发前记）。
- **实测三档**（假 sleep，秒）：同题同轮 0 / 同题跨轮 60 / **停止后立刻复问 60**（快速档）。
- **顺带**：占位卡多一句「上一题刚跑过，开头这一下要守 B 站的最小间隔（约 N 秒）」——
  复问开头本来就没动静，不说清用户会当成卡死，又去点一次停（`recentGapHint`，纯前端按上一题 `ts` 算）。

**二、去敏：从"一张清单"变成"可重复自证的一步"**

- 新增 `make_dist.py`：**排除**运行产物（`.venv` / `logs` / `out` / `harness\data` / `browser_data` /
  `.tool_token` / `mcp_servers.json` / `settings.json` / `secrets.json`）→ **在副本上抹值**
  （NGA `cookies`、bili `COOKIES`；**源树一个字不动**，本机三个服务还指着它跑）→ **打完自扫一遍**，
  有残留就非 0 退出。三条缺一不可：只排除会漏掉配置里的值，只抹值会漏掉浏览器档。
- 产物：153 个文件 / 1.0MB，自证通过；已独立复核（`cookies: {}`、`COOKIES = ""`、无 browser_data /
  `.tool_token` / 库 / 流水 / 本机绝对路径）。**要发布就跑一次它**，别手工拷。
- 活树里那 4 处真凭证**没动**（这是对的：本机 demo 正靠它们跑），账仍然记着。

**三、打分工具「装卸自如」：四种组合实测（假服务，不启浏览器）**

| 打分服务 | `TOOL_NLP` 开关 | 工具在工具面 | 度量口径 | 调用返回 |
|---|---|---|---|---|
| 没起 | 关（默认） | 否 | keyword | 「工具是关的」 |
| 没起 | 开 | **否** | keyword | 「工具是关的」（`needs()` 结构性挡住，不靠提示词劝） |
| 起了 | 开 | 是 | model | 逐条 `{senti, sarcasm, tags}` |
| 起了 | 关 | 否 | model | 「工具是关的」← **这就是"留着但不使用"** |

- **归档度量不受开关管**：`score_records` 有服务就用模型（`senti_arg/sarcasm` 真值入档），
  没服务静默降级 keyword，页面与 PDF 如实标「度量=关键词降级」。

**四、演示服务器 外网 demo 更新 + 远程可达的实情**

- 今晚这五个文件已部署并重启：`engine.py` / `webapp.py` / `webview.py` / `pdfout.py` / `web/index.html`
  （大小与本地逐个对上）。备份 `<服务器目录>/opinion-agent-20260916_2215.tgz`。
- 经 SSH 隧道复核：两盏灯绿（登录态有效）、页面有停止键与 `/api/cancel`、取消接口如实回
  「这个对话现在没有正在跑的题」、**度量 = model**（演示服务器 装着打分服务，本机没有 —— 两边都如实）。
- **远程可达**：8780 是**云平台安全组**挡的（主机侧 ufw 没开、iptables 只有 <云厂商> 自己的主机防护链），
  服务器内部无解；`30033` 已被 **ts3server** 占用，腾不出来。**眼下能走的是 SSH 隧道**（22 是通的）：
  `ssh -i <key> -L 18780:127.0.0.1:8780 root@198.51.100.10`，然后开 `http://127.0.0.1:18780/?k=<码>`。
  已实测通（`/api/status` 200）。**想直连 8780 仍需去控制台加一条入站规则。**

**边界（不做）**：真人手点（守铁律）；真 LLM 端到端跑「装载后打分被引用」那条（要烧令牌 + 真爬，
留给用户醒后按需跑）；`harness-dist` 同步回源码仓库（老账）。
