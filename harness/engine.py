# -*- coding: utf-8 -*-
"""引擎: DeepSeek function-calling 回合循环 + 记忆钩子(L1状态卡 / L3自动落档 / 会话日志)。
模型可见工具面 = {crawl_nga, crawl_bili, search_archive, crawl_official}(窄); 落档是 crawl_ 工具的自动副作用;
crawl_official = 官号探针(舆论类专用, 每 ask 一次, 占 1 爬轮, 平台钉死 bilibili; 窗口按题目时间锚点定);
结论 materialize 进状态卡, 换话题翻篇 close 进会话日志, 复盘按需回捞。
mode: 快速=单次证据上限5, 精准=15(参数闸门在服务端强制, 模型不可超)。

引擎级闸门(断路器与退避锁合一为平台状态机):
  ① 轮预算(硬): CRAWL_BUDGET 是"本 ask 爬轮"硬上限 —— 一"轮" = 一条 assistant 消息里发过 ≥1 个
     成功真爬(crawl_nga / crawl_bili / crawl_official); 同轮 NGA+bilibili 双平台各爬一次算 1 轮(平台不二选一),
     且同一平台一轮内可发多个短检索词(拆词多次爬, 见提示词规则 6b), 这些调用仍只算 1 轮,
     用尽即 block 强制作答。全被平台闸门挡(熔断/冷却/真爬失败)的轮不耗预算。
     达标不硬停: SAMPLE_TARGET 只作参考量(软), 够不够由 LLM 每轮自判, 每次真爬结果带
     ask_total(本 ask 两平台新增累计) 供它与参考量比对。
  ①.5 并发安全: 同一轮内多个 crawl 的 fetch(provider 抓取, 不碰 DB)跨平台并发、**同平台串行**
     (服务端单飞, 并发同平台会被 429 挡回; 同平台相邻调用留 SAME_ROUND_GAP 秒);
     落库(record_crawl)与平台计数/熔断/冷却更新全部回主线程串行 —— sqlite conn 单写不跨线程。
  ② 平台状态机: 一次 CrawlError -> 本 ask 熔断该平台(ask_fail, 每 ask 清); bili 额外叠会话冷却
     5min 起、会话内连熔断翻倍封顶15min(成功归零), 冷却中 bili 调用 defer; NGA 只熔断不冷却
     (live provider RAISE CrawlError, 空返回=真没搜到)。某平台被挡就转另一平台 / search_archive;
     不做 46开 配比硬 gate(两侧收量天然不均, 只要求作答时如实注偏)。
  ③ bili 爬距: 跨轮/跨 ask 快速60s / 精准120s; 同轮内同平台的多次短词调用只留 SAME_ROUND_GAP(12s)。
     键在真实 crawl 请求时刻、跨 ask 持久; 该等则等, 不许为省时间并发打同一平台;
  ④ 工具轮耗尽强制一次无工具收口, 结论不悬空占位;
  ⑤ 上下文护栏: **这一轮真要发出去的那一份**超 _in_ceiling(≈模型窗口的 16%/32%, 只在异常工具
     风暴触发)就提前收口作答。护栏只管"出网那一炮"的尺寸, 不管累计 —— 正常题累计二三十万、
     单炮十几万, 不算越线(cumulative 不是每条 API 的限额)。
     触发后**收口那一次出网也必须压到线下**: 从最早的工具结果起留头/退占位(_shrink_to_ceiling)。
     平时不裁剪任何证据轮 —— 最终答案聚合引用各轮 run(实测精准档答集跨全部爬轮), 裁剪会断
     [id=N] 引用链, 故单 ask 内证据保真不折叠; 省 token 的正解是少绕圈(轮数), 不是截原文。"""
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
import re
import time
import urllib.request
import uuid

from . import archive
from . import cooldown
from . import flow
from . import llmstream
from .cites import cite_ids, has_cite, normalize_junk, strip_bad  # 多形态引用解析([id=N] / 成组 / 区间 / 混写), 三处调用点共用一份
from .context import token_count  # cl100k 离线计量(context.py), 本 ask 引擎侧输入估算
from .evidence import pool_clause  # 证据按"池"读(跨会话可回捞), 记忆才按 scope 隔离
from .quotecheck import check_answer as check_quotes   # 引文挂靠校验: 号对得回、但引文挂错了帖子
from .quotecheck import repair_answer as repair_quotes  # 同上的"改"那一半: 就地改挂到真出处
from .registry import find_in_text, sibling_terms  # L4 黑话注册表(无表安全回落 []): 昵称 <-> 规范游戏名
from .state_card import StateCard
from .session_log import SessionLog
from .tools import crawl_live as crawl_tools, search_archive, registry as tool_registry
from .mcp import client as mcp_client
from .sources import CrawlError

# 大模型的接法(地址/模型名/密钥/窗口)见 settings.py 的「大模型」组 —— 用户能自己换服务商,
# 所以这几样**每次调用现读**, 不定死成 import 时的常量(换了要重启才生效就太蠢了)。

CRAWL_TOOLS = crawl_tools.TOOL_BY_NAME            # tool名 -> {platform}, ask 用它映射平台(不再暴露 platform 参数给模型)

# 工具面**不再写死在这个文件里**: 自带工具的唯一正源是 tools/registry.py(引擎、MCP 服务端、
# 设置页三处读同一份), 用户在设置页关掉哪个, 哪个当场不进工具面 —— 结构性防线, 不靠提示词劝。
# 用户自己加的外部工具服务(见 mcp/client.py)也在 ask 开始时并进来, 名字带 mcp__ 前缀。

# 覆盖度与采信纪律(两模式共用): 这几条针对的是"引文没问题、但话说过头"的一类错 ——
# 模型取到的样本只是检索词的命中面, 却按"我阅尽全社区"的口吻下全称判断(实测 Q4/Q8 据此谎报"未取到"),
# 或只采信支持侧、把单游戏意见说成整体(Q7/Q10/Q12/Q14)。措辞必须落到可执行动作上, 不写空话。
_COVERAGE_RULES = (
    "【覆盖度与采信纪律】\n"
    "11) 覆盖度: 每次真爬取到的样本**已全量给你**(返回体的 shown=本轮条数, total_obs=该游戏本题已归档总数), "
    "但取样面仍受检索词限制 —— 没取到只说明「这个检索词没命中」。因此断言某声音「不存在/没人提」时, "
    "只能写成「本轮取到的样本中未见」, 严禁写成「社区没有」这类全称判断; "
    "想坐实缺失, 换个检索词再爬, 或用 search_archive 把取样面扩大再下嘴。\n"
    "12) 数量口径: 报条数时单平台用返回体的 plat_total(本平台累计), 总量用 ask_total(两平台累计); "
    "别把 ask_total 当某一平台的条数 —— 它挂在单平台返回体上, 极易读串。\n"
    "13) 反面声音: 样本里若出现与主结论相反的意见, 必须在答案里单独交代并引 [id=N], "
    "不许只挑支持侧的料; 正反都有就说正反都有。\n"
    "14) 范围自觉: 证据只来自一两个对象时, 写明样本构成(如「样本主要来自 X/Y 两个版」), "
    "别把它们的看法说成整体; 不指定游戏的题尤其要核一遍每句话背后的样本是哪几个游戏。\n"
)


_SOFT_SIGNAL_HINT = (
    "【软信号提示】每条样本/证据行都附字段: senti=情绪(负/中/正)、sarcasm=1 表示模型判为可疑反讽(阴阳怪气/正话反说常见)、tags=话题标签。"
    "读文本时结合它们判社区风向——sarcasm=1 的正面措辞('策划真懂玩家'这类)常是反讽, 别当正面证据采信; senti=负 提示负面吐槽。"
    "但软信号是初筛提示、本身可能判错(反讽字段精度有限), 判向最终以原文为准, 拿不准就按规则 4 明说, 别被单条软信号带偏。\n"
    "【量化热度字段】bili 条目另带 view=播放量、like=点赞数、reply=回复数、danmaku=弹幕数、video_tags=视频自带标签; "
    "NGA 主帖带 reply=回复数, 热评带 like=点赞数。这些是**采集当时**的平台计数快照, 可当'热度/声量'的论据引用, "
    "也可供下游画图表; 字段为 null 表示该条平台没这个数(老归档行也没补录), 是'无此数据'不是 0, 别当 0 读、更别自己估个数填上。\n"
)


def _base_system(mode, budget, tgt):
    """快速/精准两套独立系统提示(前端可选项, 行为与预算不同 -> 不共用一套)。
    按用途分六段(取证/平台/纪律/检索词/采多少/覆盖度), 只为让模型分得清该在哪里使劲;
    规则编号全篇连续 —— 段间交叉引用(如软信号块的「按规则 4 明说」)靠编号定位, 改结构时别断号。"""
    head = (
        "你是游戏社区问答助手, 只用证据作答。规则分六段, 编号连续。\n"
        "**先看范围(在动手之前先判这一条)**: 这里只答**游戏社区相关**的问题 —— 某游戏的风评/活动/版本/角色/"
        "争议/官方态度这类。问的与游戏无关(天气、写代码、算数、翻译、闲聊、时政新闻、其它领域…)时, "
        "**一个工具都不要调**(尤其别为了显得完整去爬一个不相干的帖子), 直接回一句话说明这里只答游戏社区的问题, "
        "并给一句正确问法的例子(如「这游戏最近风评怎么样」); 最后一行照规矩写 「结论: 与游戏无关，未作答」。\n"
        "【一 · 取证与引用】\n"
        "1) 需要最新/社区当下动态的题, 先用 crawl_nga 与 crawl_bili 拿实时样本再答; 该游戏同话题归档里已够近时可 search_archive 省爬。\n"
        "2) 每条证据都带 id, 答案里用 [id=N] 标注出处(多条可写 [id=N, M] / [id=N-M]); 括号里**只放 id**, "
        "别混写别的说明 —— [id=283，sarcasm=1] / [id=49，like=530] 这类整段不算引用, 等于没标出处"
        "(引擎会把这种写法里的 id 抠出来用, 但你写的那些说明就白丢了)。热度/时间这类话要写在**括号外面**。"
        "不许无证据断言。\n"
        "2b) **一条引文挂一个号, 号要紧挨着那句引文**(最容易做错的一条): 引号里的话说完, [id=N] 就跟在**它自己**"
        "后边 —— 别把一段话里并排的好几句引文攒起来、只在段尾挂一个号。那样读者顺着段尾那个号去核对, "
        "前面几句全都对不上。一句话综合了多条证据, 就把号并成 [id=N, M] 挂在这句末尾; "
        "但**每一句引语各归各的号**, 引语出自哪条就挂哪条。\n"
        "1b) **官号探针 crawl_official(只给舆论类题用)**: 问「有什么节奏/争议/瓜」「官方什么态度、发了什么」"
        "「活动/版本日程、公告」这类题时, 额外调一次 crawl_official —— 它拉官方账号在**本题时间段内**的动态"
        "(题里给了时间锚点就取那一段, 问版本就取一个版本周期), 按评论量中位数算基准线, 明显超基准的标「高峰」"
        "(=疑似节奏发酵处): 高峰动态采 25 条评论、平峰只 5 条 —— **平峰动态的评论区也要看**, 节奏常在平峰动态"
        "底下发酵, 别只盯高峰。返回里 kind=official_post 是**官方口径**(动态正文/公告原话), "
        "kind=official_reply 是**评论区态度**, "
        "两者都要引 [id=N] 并分开说(「官方说 X, 评论区主要在骂 Y」)。**本 ask 只调一次, 且占 1 个爬轮**; "
        "常规玩家讨论仍走 crawl_nga/crawl_bili。探针返回 error 说明官号没认准或取不到(服务端不猜账号), "
        "如实说「官号探针没取到」, 别当成「官方没发东西」。\n"
        "1c) **追问**: 本会话前几轮的问答会摆在本题之前(更早的题压成一份**纪要**: 题号 + 问句 + 结论行)。"
        "用户用短句追问(「那它最近呢」「还有别的吗」「为什么会这样」「XX 呢」)时, 先认出他接着问的是哪件事、"
        "哪个游戏, 接着上一轮的茬答, **别当成一道新题从头来过**; 也别把上一轮的结论原样复述一遍当答案。\n"
        "    用户问到你**之前某一轮的具体内容**(「你刚才说的第二点具体是怎么说的」「你当时引的是哪条」"
        "「把上一条展开讲讲」)时, 别凭记忆编 —— 调 recall_answer 把那一轮的答案**全文**捞回来(按纪要里的题号或几个词), "
        "照着原话答; 纪要只有结论行, 细节在那份全文里。\n"
        "    上一轮引过的证据仍在本机历史归档里(归档是**跨会话共享**的, 换个会话也查得到), "
        "要用就先用 search_archive 把它重新捞出来、核过原文再引 [id=N] —— 不许凭记忆复述上一轮引了什么。\n"
        "【二 · 平台与轮次】\n"
        "3) 平台不作二选一: 同一轮内让 NGA 与 bilibili 都取到——同一条消息里并发调 crawl_nga 与 crawl_bili, 两平台证据都取到再综合, "
        "同轮双平台只算 1 个爬轮预算; 某平台熔断/冷却被挡就用另一平台或 search_archive, 别对同一平台反复重试硬挤。\n"
        "【三 · 下嘴纪律】\n"
        "4) 证据不足以坐实(反串/带节奏/没搜到)时明说不确定性, 给候选不硬结论。\n"
        "4b) **正文只写答案, 不写过程**(最容易犯、用户一眼就能看见的一条): 答案第一个字就是给用户看的内容 —— "
        "开头不许写「我先查一下」「证据够了」「让我组织一下」这类自我叙述, 不许复述你调了哪些工具、爬了几轮、"
        "还剩多少预算, 不许交代打算怎么组织下文, **更不许用英文写草稿**(思维链一律不进正文)。"
        "过程留给后台日志, 用户只看答案。\n"
        "5) 答案最后一行必须是: 结论: <一句话>, 供引擎回捞。\n"
        "【四 · 检索词怎么造 —— 一轮对同一平台发多个短词】\n"
        "6) 泛问拆解: 用户问\"XX 版本/游戏最近怎么样\"这类一篮子问句(常含 新活动及其风评/角色强度与风评/优化改动/负面吐槽/剧情 等各方面)时, "
        "先在心里列方面, 把每方面拆成具体关键词组(具体活动名、角色名+强度、剧情、优化、吐槽点), 分多次 crawl_nga/crawl_bili/search_archive 各取证据, "
        "别拿用户整句原话当检索词; 某方面没取到样本就明说\"该方面样本不足/未覆盖\"。\n"
        "6b) 一轮发多个短词(这条最容易做错, 务必照做): **NGA** 的检索词永远是 **游戏名 + 一个短关键词** 拼出来的 —— "
        "游戏名走 game 参数、由引擎自动拼在关键词前面, **你不用把游戏名写进 query**; query 里**只放那一个短关键词**(3-6 个字, 别是整句)。"
        "**一轮里要发多个这样的短词, 至少 3 个**, 词与词拆开、各发一次调用(同轮双平台仍只算 1 轮预算)。\n"
        "    错: 把整句黏成一串当 query —— query=\"男干员待遇强度争议\" 这种长串, 搜出去就是「明日方舟男干员待遇强度争议」, "
        "一整轮白爬。\n"
        "    对: 游戏=明日方舟, 同一轮分 4 次调 crawl_nga —— query=\"男六星\" / \"男干员\" / \"男六星待遇\" / \"男六星强度\", "
        "实际搜出去的是「明日方舟男六星」「明日方舟男干员」…, 每个都短、都能命中。\n"
        "6c) **B站(bilibili)不一样, 别照 NGA 那套拆词**: crawl_bili 没有 query 这一格 —— 检索词由引擎按"
        "**游戏本体名**发(一个词, 不拼话题词、也不带昵称), 你只要在一轮里调**一次** crawl_bili。把话题词塞给 B站"
        "(「本体+话题词」)只会搜到一堆泛话题热门视频, 跟提问没关系; B站的料靠本体名这一搜 + 高热度视频的字幕全文提供。\n"
        "    同一平台一轮内的多次调用**间隔 10-15 秒**(服务端单飞, 并发会被 429 挡回去, 别并发打同一平台); "
        "**只有轮与轮之间**才需要长等。同一条消息里多发几个 crawl 只算 1 个爬轮预算, 所以一轮塞满短词不吃亏。\n"
        "7) 黑话/昵称: 用户用圈内昵称指代游戏(如 三蹦子=崩坏三、终末地=明日方舟终末地)时, 用你的知识还原成规范游戏名填 game 参数, "
        "别让昵称进检索词; 拿不准昵称对应哪个游戏时, 用关键词先爬/搜社区核实再定, 不确定就如实说明。\n"
        "7b) 兄弟作分家: 同 IP 的衍生作与本家是两个社区(如 明日方舟 与 明日方舟终末地), 各有各的版本节奏与风评。"
        "问的是哪个游戏, 检索词里就只能出现那个游戏自己的话题词; 拿兄弟作当关键词会被拦截, 混进来的兄弟作样本也会被丢弃, "
        "别拿另一边的社区声音给这边下结论。跨 IP 的横向对比(鸣潮 vs 原神)不在此限, 正常取两边证据。\n"
        "7c) 时效题的时间锚点: 每条证据行都带 published_at。问句出现「近期/最近/当下/这期/最新」这类时效词时, "
        "系统已把当前时间与时间窗下界告诉你, 窗外的旧样本已被丢弃——你必须先核每条证据的日期, 答案里写清证据落在哪个日期区间; "
        "严禁把窗外(几个月甚至几年前)的事说成「近期最热/最近发生」。哪天确实搜不到近料, 就写「近期无有效样本」, "
        "别拿老料凑数; 老料只能用来说明来龙去脉, 并明确标注那是旧事。\n"
    )
    if mode == "精准":
        tail = (
            "【五 · 采多少(精准模式: 彻底全面, 代价是深)】\n"
            "8) 本 ask 最多 %d 个爬轮(硬上限, 用尽必须停爬直接作答; 一轮 = 同轮 crawl_nga+crawl_bili, "
            "NGA 可在一轮内发多个短检索词、B站一轮一次, 双平台都爬仍算 1 轮)。每次真爬取到的样本**全部**给你, 不用省着看。\n"
            "9) 一篮子问句的各侧面(活动及风评/角色强度/优化改动/负面吐槽/剧情等)尽量都取证覆盖, 每侧面都有 [id=N] 引用才算覆盖; "
            "参考样本量约 %d(本 ask 两平台新增累计, 见每次真爬返回的 ask_total): 达到或接近即各侧面应已取样充分, 可转收口作答; "
            "个别侧面实在搜不到样本就明说\"该方面样本不足/未覆盖\", 不硬编。\n"
            "10) 别为凑够 %d 条而硬爬: 侧面已覆盖或有把握即可停, 把剩余爬轮留给真正缺证据的侧面。\n"
        ) % (budget, tgt, tgt)
    else:
        tail = (
            "【五 · 采多少(快速模式: 快答为重, 求当下社区最热的声量)】\n"
            "8) 本 ask 最多 %d 个爬轮(硬上限, 用尽必须立即停爬直接作答; 一轮 = 同轮 crawl_nga+crawl_bili, "
            "NGA 可在一轮内发多个短检索词、B站一轮一次, 双平台都爬仍算 1 轮)。每次真爬取到的样本**全部**给你, 不用省着看。\n"
            "9) 一篮子问句不必全侧面铺开: 抓当下社区最热的 1-2 个侧面(活动风评/角色强度/优化/吐槽等)重点取证作答即可, "
            "其余侧面一句带过或明说未覆盖; 参考样本量约 %d(看 ask_total): 接近即多数话题已够答, 别再为覆盖而加爬。\n"
        ) % (budget, tgt)
    return (head + tail + "[六 · 覆盖度与采信纪律]\n" + _COVERAGE_RULES + _SOFT_SIGNAL_HINT
            + "[当前状态卡]\n%s\n[按需回捞的历史结论]\n%s")

GAME_HINTS = ("鸣潮", "原神", "王者荣耀", "明日方舟终末地", "崩坏三", "绝区零", "明日方舟")

# 引擎级闸门参数(bili 风控基线, 重跑即标定实验)
SAMPLE_TARGET = {"快速": 120, "精准": 500}  # 每 ask 参考样本量(软): 不硬停, 供 LLM 每轮比对 ask_total 自判够不够
CRAWL_BUDGET = {"快速": 3, "精准": 5}      # 本 ask 爬轮硬上限(按"轮"计: 同轮 NGA+bili 双工具并发各爬一次=1; 用尽即 block)
BILI_GAP = {"快速": 60, "精准": 120}        # 跨轮/跨 ask 的 bili 最小间距(秒, 风控型; 同轮内改用 SAME_ROUND_GAP)
SAME_ROUND_GAP = 12                         # 同一平台"同一轮内"多次调用的间隔(秒): 服务端单飞, 并发会被 429 挡回, 只能串行发
COOL_START = 300                            # 平台会话冷却起始时长(秒)
COOL_MAX = 900                              # 会话冷却翻倍封顶(秒)

def _in_ceiling(mode):
    """单轮待发上下文 token 护栏(09-12 重定档: 证据不再截断后留足余量; 仅异常工具风暴触发, 不裁证据)。

    按用户所配模型的窗口折算(1M 窗口 → 快速 160000 / 精准 320000), 换 128k/32k 那种小窗口的
    模型时会等比例收紧 —— 不然一题就能把上下文顶爆。窗口值见 settings.py「大模型」组的 LLM_WINDOW。
    """
    from .settings import llm_window
    return max(8000, int(llm_window() * (0.16 if mode == "快速" else 0.32)))

# NGA 占比入参(语义 A: 样本证据配比)。单标量定两平台: NGA 占比%, 余量=bili。
# 100=只 NGA, 0=只 bili, 50=均等(≈现"双平台每轮都爬"行为)。前端旋钮值将来直接映射本参数。
# 0/100 端点硬压另一边(_precheck 顶部); 内部 1..99 由 _ratio_nudge 每轮软引导(只提示不改调度); 熔断/冷却永远优先于比例。
DEFAULT_NGA_RATIO = 50
RATIO_TOL = 5                                # 平台占比误差容忍(百分点): 内部 1..99 软引导, 偏离超此差才提示, 不硬门


def clamp_ratio(x):
    """任意输入 -> 0..100 整数 NGA 占比; 非法回落默认 50。"""
    try:
        return max(0, min(100, int(round(float(x)))))
    except (TypeError, ValueError):
        return DEFAULT_NGA_RATIO


def _est_input_tokens(messages):
    """context.py cl100k 引擎侧计量: 对本 ask 实际发送的消息 content 逐条估算。
    只统计 content 文本(不含工具 schema/元数据) = 真实输入的下界 -> 与 provider 用量双保险,
    不会比真输入更早触发护栏; provider 缺 usage 时它是唯一护栏。"""
    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            total += token_count(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    total += token_count(b["text"])
    return total


_TOOL_KEEP = 3000      # 压缩时每条工具结果先留这么多字
_CUT_TAIL = "\n…(后文因本问上下文上限被截断; 需要可 search_archive 回捞)"
_CUT_GONE = "{\"note\":\"(这条较早的证据已因本问上下文上限略去; 需要可 search_archive 回捞)\"}"
_SHRINK_TO = 0.9       # 压到上限的这个比例为止: _est_input_tokens 只数 content, 是真输入的**下界**,
                       # 真请求还带工具 schema, 留一成就不会被下界骗过去。


def _shrink_to_ceiling(messages, ceiling):
    """护栏硬顶(§10.50): 这一轮真要发出去的消息超了 ceiling, 就**从最早的工具结果起**往下压,
    直到估算用量落回线下。返回 [(第几条, 原字数, 新字数), ...]; 没超 / 压不动则返回 []。

    为什么不是"到点就停"就完事: 原先的护栏只是**不再加料**并跳出循环 —— 可跳出之后那次
    "强制收口" 调用的还是同一份超长 messages, 于是真正发出去的那一炮照样越线(q3 就是: 快速档
    16 万的上限下, 跳出后仍发了一份更大的)。「拦住」得管到每一次真实出网。

    为什么不整条删: OpenAI 协议的 tool 消息必须与前面 assistant 的 tool_calls 一一对应,
    删一条就把整轮请求弄成非法(400)。所以只压内容、留壳。
    从最早的压起: 最近几轮的证据正是模型此刻在用的, 最后才动它。"""
    idxs = [i for i, m in enumerate(messages)
            if isinstance(m, dict) and m.get("role") == "tool" and isinstance(m.get("content"), str)]
    target = int(ceiling * _SHRINK_TO)
    if not idxs or _est_input_tokens(messages) <= target:
        return []
    cuts = []
    # 两段压: 先把每条长的留头(证据还在, 只是短了), 还不够才把最早那几条退成占位。
    # 分两段是因为"留头"有个地板 —— 每条最少留 _TOOL_KEEP 字, 条数一多就压不到线下,
    # 那时只能牺牲最早的几条(它们离当前作答最远)。
    for i in idxs:
        if _est_input_tokens(messages) <= target:
            break
        old = messages[i]["content"]
        if len(old) <= _TOOL_KEEP:
            continue
        messages[i]["content"] = old[:_TOOL_KEEP] + _CUT_TAIL
        cuts.append((i, len(old), len(messages[i]["content"])))
    for i in idxs:
        if _est_input_tokens(messages) <= target:
            break
        old = messages[i]["content"]
        if old == _CUT_GONE:
            continue
        messages[i]["content"] = _CUT_GONE
        cuts.append((i, len(old), len(_CUT_GONE)))
    return cuts


def _norm_game(g, hint):
    g = (g or "").strip()
    return g if g else (hint or "未知")


# 用户点了「停止」时, 本轮还没发出去的那几条检索的占位标记。
# 用它而**不是**拿 CrawlError 顶: CrawlError 意味着"平台挂了", 会顺手熔断该平台并记一次风控冷却 ——
# 用户停个题不该让 bilibili 冷却 5 分钟。故单列一个哨兵, 在收集点单独认。
_STOPPED = object()


def _sib_hit(text, terms):
    """检索词是否越界到本游戏的兄弟作; 命中返回那个外表名, 否则 None。"""
    t = text or ""
    for term in (terms or ()):
        if term and term in t:
            return term
    return None


# 时效词: 命中才给"近期"语义的时间窗。历史题(如"米池怎么演变")不带这些词, 老样本照留。
_RECENCY_CUES = ("近期", "最近", "近来", "这阵子", "这几天", "这两天", "现在", "当下", "这期",
                 "最新", "目前", "当前", "这几天", "这个版本")
RECENCY_DAYS = 90


_CST = datetime.timezone(datetime.timedelta(hours=8))   # 中国无夏令时, 固定 +8 即精确


def now_str():
    """'现在'的锚点(北京时区) —— 必须与 NGA/bili 的 published_at(北京墙钟)同一把尺, 否则时间窗比错。

    钉死 +8 而不是取宿主本地时区: 本机与演示服务器现在恰好都是 CST, 但搬进 UTC 容器/跑在别处就会
    静默差 8 小时(窗口漂、给模型看的"当前时间"也错), 且不会报错 —— 这类静默错最难查。
    HARNESS_NOW 可钉死(回放/测试用)。"""
    return (os.environ.get("HARNESS_NOW") or "").strip() or \
        datetime.datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S")


def recency_since(user, now=None):
    """问句带时效词 -> 返回 (下界字符串, 天数); 无时效词 -> (None, 0)。
    锚点是真实"现在" —— 模型看不到日期就无从判"近期", 所以这个下界同时进提示词和入档闸门。"""
    if not any(c in (user or "") for c in _RECENCY_CUES):
        return None, 0
    base = datetime.datetime.strptime((now or now_str())[:19], "%Y-%m-%d %H:%M:%S")
    return (base - datetime.timedelta(days=RECENCY_DAYS)).strftime("%Y-%m-%d %H:%M:%S"), RECENCY_DAYS


# 显式时间锚点(历史题): "三个月前"/"去年"/"2024年6月" 这类。与 _RECENCY_CUES("近期")互斥。
_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_RE_MONTHS_AGO = re.compile(r"(半|\d{1,2}|[一二两三四五六七八九十]+)\s*个月前")
_RE_YEARS_AGO = re.compile(r"(半|\d{1,2}|[一二两三四五六七八九十]+)\s*年前")
_RE_YMD = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")
_RE_Y_ONLY = re.compile(r"(\d{4})\s*年(?![^，。；\s]{0,3}\d)")


def _cn_int(s):
    """'三'/'12'/'11' -> int; '半' -> None(调用方按半年处理); 认不出 -> None。"""
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    m = re.match(r"^([一二两三四五六七八九])?十([一二三四五六七八九])?$", s)
    if m:
        return (int(_CN_DIGIT[m.group(1)]) if m.group(1) else 1) * 10 + \
               (int(_CN_DIGIT[m.group(2)]) if m.group(2) else 0)
    return _CN_DIGIT.get(s)


def _month_bucket(y, m):
    """(年, 月) -> (该月首日, 该月末日); m 越界自动跨年。"""
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    last = (datetime.date(ny, nm, 1) - datetime.timedelta(days=1)).day
    return "%04d-%02d-01" % (y, m), "%04d-%02d-%02d" % (y, m, last)


def time_windows(user, now=None):
    """问句里的显式时间锚点 -> [(since, until), ...] 每段 'YYYY-MM-DD'; 无锚点 -> []。
    单锚点 = 单窗(与旧口径一致); **多锚点**(对比题, 如「今年…跟去年…比」)= 每段各一窗,
    交爬虫分段各翻一窗 —— 只开一个"并集大窗"会因翻页按末回降序而只取到最新一截, 早那段全丢。
    口径(整月/整年, 不做"某点前后 N 天"的含糊解释):
      N个月前/半年前 -> 那一个自然月; N年前/去年/前年/今年 -> 那一整年; YYYY年M月 -> 那个月; YYYY年 -> 那一年。"""
    u = user or ""
    base = datetime.datetime.strptime((now or now_str())[:19], "%Y-%m-%d %H:%M:%S")
    wins = []
    m = _RE_MONTHS_AGO.search(u)
    if m:
        n = _cn_int(m.group(1))
        n = 6 if n is None else max(1, min(120, n))
        wins.append(_month_bucket(base.year, base.month - n))
    if "去年" in u:
        wins.append(("%04d-01-01" % (base.year - 1), "%04d-12-31" % (base.year - 1)))
    if "前年" in u:
        wins.append(("%04d-01-01" % (base.year - 2), "%04d-12-31" % (base.year - 2)))
    if "今年" in u:
        wins.append(("%04d-01-01" % base.year, "%04d-12-31" % base.year))
    m = _RE_YEARS_AGO.search(u)
    if m:
        n = _cn_int(m.group(1))
        if n is None:                      # "半年前" = 6 个月前的那个月
            wins.append(_month_bucket(base.year, base.month - 6))
        else:
            n = max(1, min(30, n))
            wins.append(("%04d-01-01" % (base.year - n), "%04d-12-31" % (base.year - n)))
    m = _RE_YMD.search(u)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            wins.append(_month_bucket(y, mo))
    m = _RE_Y_ONLY.search(u)
    if m:
        y = int(m.group(1))
        if 2000 <= y <= base.year:
            wins.append(("%04d-01-01" % y, "%04d-12-31" % y))
    out, seen = [], set()
    for w in sorted(wins):
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def time_anchor(user, now=None):
    """兼容旧签名: 有锚点 -> (since, until) = 各窗并集(最早起点 .. 最晚终点); 无锚点 -> (None, None)。
    单锚点题 = 该窗本身; 多锚点(对比题)的并集只用于提示词文案与入档闸门 —— 实际取数走
    time_windows 分段各翻一窗(见 run_query 的分段窗导航), 不靠这个并集取数。"""
    ws = time_windows(user, now)
    if not ws:
        return None, None
    return min(w[0] for w in ws), max(w[1] for w in ws)


_XML_TAG = re.compile(r"<\s*/?\s*(?:tool_calls|tool_call|invoke|function_call|function)\b[^>]*>", re.I)


def _scrub(t):
    """删模型误当正文输出的工具调用脚手架(<tool_calls>…</tool_calls>、孤儿 </tool_calls>、未闭合 <tool_calls…),
    折叠多余空行, 防脏文本落纸。只删标签不裁正文 —— 标签后的真结论保留。"""
    s = _XML_TAG.sub("", t or "")
    s = re.sub(r"<\s*(?:tool_calls?|invoke|function_call)[^>]*$", "", s, re.I)  # 截断残留尾巴
    s = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", s)
    return s.strip() or "(空)"


def _tool_intent(s):
    """结论正文是『我去搜/爬/补样本』的意图残句(收口失败的典型漏网) -> True。只查开头 30 字, 降低误伤真结论。"""
    head = (s or "").strip()[:30]
    return (bool(re.match(r"^[\s\S]{0,16}(让我|我先|我再|我要|待我|预算已用完|预算用完|crawl预算)", head, re.I))
            or "search_archive" in head.lower() or "crawl_" in head.lower())


def _conclusion(s):
    """取最后一个『结论: …』行正文; 没有结论行 / 正文是工具意图 -> None。"""
    hit = ""
    for ln in (s or "").splitlines():
        st = ln.strip()
        if st.startswith("结论"):
            hit = st.lstrip("结论：:").strip()
    if not hit or _tool_intent(hit):
        return None
    return hit


def _sanitize_cites(answer, conn, scope):
    """收口证据校验(§M1.3): 把对不回**本机证据池** observation 的 [id=N] 剔除 —— 工具只向模型面
    暴露池内的 obs id(见 tools/search_archive.py + crawl_live._ev), 故编造的引用 = 模型幻觉, 不许落纸。
    返回 (清理后正文, 被剔 id 列表); 无引用或全有效则原样。

    口径是**池**不是 scope: 检索与归档都跨会话共享同一池, 模型上一轮/上一个会话查到的 id
    本身就还在池里, 引它是本分(用户要的"之前查过的还能回去查到"), 不是幻觉。实验池(exp:*)
    仍自成一体 —— 标定数据不许被真实会话引到。

    引用形态统一走 cites.py(认 [id=N] / [id=N, M] / [id=N-M] / 混写): 原先各处的严格单引用
    正则看不见成组与区间, 那些 id 既不进校验也不进纪律判定 = 等于未校验(实测命中 2 题);
    最阴的是混写「见 [id=12] 与 [id=13, 14]」——只校验 12, 13/14 无声放行。"""
    if not answer:
        return answer, []
    ids = cite_ids(answer)
    if not ids:
        return answer, []
    cond, sargs = pool_clause(scope)
    q = "SELECT id FROM observation WHERE " + cond + " AND id IN (%s)" % \
        ",".join("?" * len(ids))
    good = {r[0] for r in conn.execute(q, sargs + ids)}
    bad = [n for n in ids if n not in good]
    if not bad:
        return answer, []
    return strip_bad(answer, bad)


_ABSTAIN_MARK = ("证据不足以", "无法坐实", "无法确认", "未能", "未检索到",
                 "未找到", "样本不足", "不足以", "(未收口)", "无直接")


def _discipline_ok(answer):
    """出关第二层·硬尾纪律(§10.24): 有 [id=N] 引原文, 或明说判不了(abstain 措辞)。
    措辞镜像 runner/evalgold.ABSTAIN_MARK, 两处不许漂移 ——
    _tmp/verify_determiner_discipline.py 每次自检断言两份一致。"""
    return has_cite(answer or "") or \
        any(m in (answer or "") for m in _ABSTAIN_MARK)


def _drop_empty_concl(txt):
    """清掉悬空的空『结论:』/『结论：』行(模型收口失败但只留了个空标记), 返回余下正文。"""
    keep = [ln for ln in (txt or "").splitlines()
            if not re.match(r"^\s*结论\s*[:：]\s*$", ln)]
    return "\n".join(keep).strip() or "(空)"


_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z']{1,}")
_CJK_CHAR = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]")


def _cjk_ratio(s):
    """非空白字符里中日韩字占多少 —— 判「这行是不是写给中文用户看的正文」。"""
    body = [c for c in (s or "") if not c.isspace()]
    return len([c for c in body if _CJK_CHAR.match(c)]) / len(body) if body else 0.0


def _strip_leading_scratch(answer):
    """删掉答案**开头**漏出来的英文思考过程(§10.59); 返回 (新正文, 删掉的行)。

    病灶: 模型收口前爱先用英文自述一句, 实测「I've hit the crawl budget limit (3 rounds used).
    Time to answer with what I have.」直接粘在正文第一行 —— **用户一眼看得见**。提示词里明令禁止
    (下嘴纪律 4b)照样漏(5 题漏 1 题, 与 20 题漏 4 题同量级), 说明这条不能靠自律, 得拦在确定性这层。

    判法: 从第一行往下走, 每行满足「英文词 >= 3 **且**中日韩字占比 < 0.2」就当草稿删; 一碰到中文行
    立刻停手 —— 后面一个字不动, 正文里的英文术语/游戏名一律留。两道保险防误伤: 删完剩下的短于 60 字,
    或删掉的行比剩下的正文还长, 就**整个不删** —— 宁可真漏一句没拦住, 也不能把答案吃空。

    (原先第二道保险写的是"删掉超过全文三成", 自检里当场就栽了: 实测那句草稿 85 字、正文 150 字左右,
    36% —— 一道本来该拦住草稿的闸, 在**正常长度的答案上几乎必然误放行**。改成"比剩下的还长"才有判别力:
    草稿比正文长, 那才真叫危险。)"""
    lines = (answer or "").splitlines()
    cut, seen = 0, []
    for ln in lines:
        st = ln.strip()
        if not st:
            cut += 1
            continue
        if _cjk_ratio(st) >= 0.2 or len(_ASCII_WORD.findall(st)) < 3:
            break
        seen.append(st)
        cut += 1
    if not seen:
        return answer, []
    rest = "\n".join(lines[cut:]).strip()
    if len(rest) < 60 or sum(len(x) for x in seen) > len(rest):
        return answer, []
    return rest, seen


class Engine:
    def __init__(self, conn, provider=None, scope="prod", official_provider=None, gate=None):
        self.conn = conn
        self.scope = scope
        # 跨会话的爬取闸门(见 harness/crawlgate.py): 网页后端把**同一个**实例发给每个会话,
        # 于是不同会话爬同一个平台会排队而不是抢单飞(抢输的那个会被 429 误判成风控)。
        # None = 不排队(CLI / 标定 / 自检那条路没有第二个会话, 行为与从前一模一样)。
        self._gate = gate
        self.card = StateCard()
        self.log = SessionLog(conn, scope=scope)
        if provider is None and os.environ.get("HARNESS_LIVE"):
            from .sources import live
            provider = live.crawl
        self.ctx = {"conn": conn, "provider": provider, "scope": scope,
                    "official_provider": official_provider,
                    # 字幕: 已问过字幕的视频(bvid)与本轮剩余额度。**跨 ask 留着** —— 同一批热门视频
                    # 每一问都会排在前面, 不记着就会反复去取, 取回来还都被去重挡掉(见 crawl_live)。
                    "_sub_done": set(), "_sub_left": None}
        self._n = 0
        # 跨 ask 持久的平台状态机(每题复用同一 Engine 才生效): 断路器+退避锁合一
        self._mode = "快速"
        self._n_crawl = 0                  # 本 ask 已真爬轮数(预算闸门, 每 ask 重置; 同轮双平台=1)
        self._ask_seq = 0                  # 本题序号(每 ask +1): 判"同轮"要连 ask 一起认, 见 _precheck
        self._plats = {}                   # platform -> {ask_fail, fail_streak, cool_until, last_at, new}
        self._nga_ratio = DEFAULT_NGA_RATIO  # 每 ask 有效 NGA 占比(用户旋钮入参, ask() 里更新)
        self._official_used = False        # 官号探针本 ask 成功过就不再放行(贵: 一个版本周期的动态 + 各自评论)
        self._official_tries = 0           # 本 ask 官号探针尝试次数(失败也给一次补救机会, 见分支注释)
        self._tools = None                 # 本 ask 的工具面(ask 开始时现组; None = 用到时再组)
        self._routes = {}                  # 外部工具名 -> (哪个服务, 它原本叫什么), 见 _ext_defs
        self._cancel = None                # 本 ask 的"停止"信号(threading.Event), 由调用方传入; None = 不可停
        # 本 ask 的"暂停"信号(threading.Event)。与 _cancel 是**两件事**, 别看它们都只是打断循环:
        # 「停止」= 到此为止, 拿已取到的证据收口作答(留下一份短答案);
        # 「暂停」= 先搁着, **不出答案** —— 已爬到的样本照旧在库, 下次接着爬再一并作答。
        # 合成一个信号就没法区分结尾该不该落答案了。
        self._pause = None
        self._resume_from = None           # 本 ask 是接着哪条暂停记录跑的(None = 全新的一道题), 见 _precheck        # 本 ask 的实时事件通道(网页那条 /api/ask 的推送): 每 ask 由调用方传入, None = 不推。
        # **回调必须是线程安全的** —— 爬取那段的 tool-start 是从并发抓取的 worker 线程里推的。
        self._on_event = None
        # 本题的 token 消耗(三档分开: 缓存命中/未命中/输出)。每炮 LLM 回来都累进这里, ask 开头清零。
        # 放实例上而不是循环变量里: 收口那次「重写」是在 _final_answer 里另发的炮, 循环里够不着它,
        # 而它的 prompt 是整份上下文, 常常是本题输入量最大的一炮。
        self._tk_hit = self._tk_miss = self._tk_out = 0
        self._tk_model = ""                # 本题实际用的模型名(页面上要说清是哪家的账)

    def set_scope(self, scope):
        """换记忆归属: 先把旧 scope 的活跃结论翻篇进**旧**日志(边界即提交点),
        再切卡/编号/日志/ctx 到新 scope(逐题隔离标定时每题一套, 不串题)。
        平台冷却(_plats)刻意保留: 跨题沿用 B站风控退避, 不因换 scope 丢节奏。"""
        if scope != self.scope:
            self._flush_active()
            self.scope = scope
            self.card = StateCard()
            self._n = 0
            self.log = SessionLog(self.conn, scope=scope)
            self.ctx["scope"] = scope

    def _flush_active(self):
        """把当前 scope 所有仍活跃的结论翻篇进会话日志(scope 边界收口用)。"""
        if not self.card.active:
            return
        topic = self.card.anchor["game"] or ""
        for cid in list(self.card.active):
            claim = self.card.close(cid)
            if claim:
                self.log.append(cid, claim, topic=topic)

    # ---------- 工具面 ----------
    def _ext_specs(self):
        """用户在设置页加的外部工具服务里, 这一轮该用的那些(开着的、填全了的)。"""
        from . import settings
        out = []
        for s in settings.mcp_servers():
            if not s.get("enabled"):
                continue
            if s["transport"] == "http":
                if not s.get("url"):
                    continue
            elif not s.get("command"):
                continue
            out.append(s)
        return out

    def _ext_tools(self):
        """外部服务各自提供哪些工具(带缓存; 连不上的那个当没有, 不拖垮本 ask)。"""
        got = {}
        for s in self._ext_specs():
            got[s["name"]] = mcp_client.tools_cached(s)
        return got

    def _ext_defs(self, ext):
        """外部工具 -> 模型可见的定义, 外加一张**名字对照表**。

        名字是压过的(中文服务名会压成一段哈希), 光看模型回过来的那个名字还原不出原名 ——
        所以组名的时候就在这儿把「这个名字 -> (哪个服务, 它原本叫什么)」记下来, 回话时查表。
        表跟着本 ask 的工具面走, 不存盘、不跨 ask。"""
        defs, route = [], {}
        for s in self._ext_specs():
            for t in (ext.get(s["name"]) or []):
                wire = mcp_client.wire_name(s["name"], t.get("name"))
                while wire in route:           # 理论上撞不上; 真撞了就错开, 绝不共用一个名字
                    wire += "_"
                route[wire] = (s, t.get("name"))
                defs.append(mcp_client.openai_def(s, t, wire))
        return defs, route

    def _tool_defs(self, ext_tools=None):
        """本 ask 给模型看的工具面 = 自带(按设置页开关) + 外部工具服务(按用户加的清单)。
        每次都现组 —— 用户在设置页一改, 下一题就生效, 不用重启。"""
        ext = self._ext_tools() if ext_tools is None else ext_tools
        ext_defs, self._routes = self._ext_defs(ext)
        return tool_registry.defs() + ext_defs

    def _call_external(self, name, args):
        """外部工具(mcp__…): 按对照表转给对应服务。失败如实报错, 不抛(抛了整题作废)。"""
        hit = self._routes.get(name)
        if hit is None:
            # 正常走不到(名字是从本 ask 的工具面里来的)。真到这儿就是不猜 —— 猜错等于
            # 把话递给另一家服务, 宁可让模型换个工具。
            return {"error": "工具面里没有 %s 这个名字(设置可能刚被改过, 下一题会重新组)" % name}
        spec, remote = hit
        out, err = mcp_client.call_tool(spec, remote, args)
        if err:
            return {"error": "外部工具 %s 没调成: %s" % (spec.get("name"), err)}
        return out if out is not None else {"note": "外部工具返回空"}

    # ---------- LLM ----------
    def _emit(self, ev):
        """给网页那条实时通道推一个事件。没接通道(None)就是空操作 —— CLI/标定/自检一点不受影响。
        推事件本身失败绝不能把这一题搞挂: 它是"看着好看", 不是答题必需的。"""
        cb = self._on_event
        if cb is None:
            return
        try:
            cb(ev)
        except Exception:
            pass

    def _llm(self, messages, tools=True, stream=False):
        """stream=True: 走流式, 每收到一段正文就推一个 text-delta 出去(前提是接好了 _on_event)。
        返回体与非流式**同形状**, 所以主循环不必分叉(拼装规则见 harness/llmstream.py)。

        流式重试有个额外讲究: 已经往外吐过字了再重试, 同一段话会在页面上出现两遍 ——
        所以那一下先推一个 delta-reset, 让前端把这一轮的草稿丢掉再重来。"""
        from .settings import llm_cfg
        url, model, key = llm_cfg()
        body = {"model": model, "temperature": 0.3, "messages": messages}
        if tools:
            body["tools"] = self._tools if self._tools is not None else self._tool_defs()
        if not stream:
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            last = None
            for _ in range(3):
                try:
                    with urllib.request.urlopen(req, timeout=120) as r:
                        return self._count(json.loads(r.read().decode("utf-8")), model)
                except Exception as e:
                    last = e
                    time.sleep(2)
            raise RuntimeError("LLM call failed: %s" % last)
        last = None
        for _ in range(3):
            sent = False

            def on_delta(t):
                nonlocal sent
                sent = True
                self._emit({"type": "text-delta", "text": t})

            try:
                return self._count(llmstream.complete(url, key, body, on_delta=on_delta), model)
            except Exception as e:
                last = e
                if sent:
                    self._emit({"type": "delta-reset"})
                time.sleep(2)
        raise RuntimeError("LLM stream failed: %s" % last)

    def _count(self, resp, model):
        """把这一炮的 token 消耗记进本题的账。

        **每一炮都必须过这里**: 收口那次「重写」也是一炮(在 _final_answer 里发出去的), 它的 prompt
        是整份上下文, 常常是本题输入量最大的一炮。漏记它, 页面上那个消耗数就少一块。

        三档分开记(缓存命中/未命中/输出): 命中与未命中的**单价与性质都不同**, 混成一个 prompt_tokens
        总数, 事后谁也拆不回来。

        服务商没给缓存拆分(只回 prompt_tokens)时整份算未命中 —— 那是多的那一档, 页面上会如实标
        "未拆分"。服务商偶尔根本不回 usage: 那一炮记 0, 不猜。

        记账本身出岔子绝不能连累答题: 兜底包住, 任何情况都把 resp 原样交回去。"""
        try:
            u = (resp or {}).get("usage") or {}
            hit = int(u.get("prompt_cache_hit_tokens") or 0)
            miss = int(u.get("prompt_cache_miss_tokens") or 0)
            if not hit and not miss:
                miss = int(u.get("prompt_tokens") or 0)
            self._tk_hit += hit
            self._tk_miss += miss
            self._tk_out += int(u.get("completion_tokens") or 0)
            if model:
                self._tk_model = model
        except Exception:
            pass
        return resp

    def _tk_total(self):
        """本题到目前为止累计的 token 消耗, 供实时画面边爬边显示(还没收口时就知道用了多少)。"""
        return {"prompt_tokens": self._tk_hit + self._tk_miss, "completion_tokens": self._tk_out}

    def _final_answer(self, messages, raw):
        """最终答案收口: 删工具残留; 若没有干净『结论:』行(或结论是『我去搜』的意图残句) ->
        把失败草稿回喂一次, 紧致重写强制以『结论:』收口。保证展示/落库的不再是搜索意图残句。"""
        txt = _scrub(raw)
        if _conclusion(txt):
            return txt
        messages.append({"role": "assistant", "content": txt or "(空)"})
        messages.append({"role": "system",
                         "content": "上一条回答没有以『结论: <一句话>』收口, 或还在表达搜索/爬取意图。"
                                    "现在工具全部禁用: 只用已有证据作答, 可给一两句要点(逐条 [id=N]), "
                                    "最后一行必须是『结论: <一句话>』。证据不足就写: 结论: 证据不足以坐实…。"
                                    "禁止提及工具、搜索、预算、归档等动作。"})
        # 上面那份草稿已经流给前端了(正因为它没有干净结论行才走到这里), 下面这一炮是**重写** ——
        # 先叫前端把草稿丢掉, 否则"没结论的草稿"会和"重写后的正文"在页面上接成一长串。
        self._emit({"type": "delta-reset"})
        resp = self._llm(messages, tools=False, stream=self._on_event is not None)
        txt2 = _scrub(resp["choices"][0]["message"].get("content") or "(空)")
        if _conclusion(txt2):
            return txt2
        # 二次仍无干净结论(如只剩空『结论:』标记): 清空标记返回正文, 落库走 _materialize 的 (未收口)
        return _drop_empty_concl(txt2)

    def _seal_answer(self, answer, trace, crawled=False):
        """收口证据校验(§M1.3): 剔除对不回本机证据池的 [id=N](工具只暴露池内 obs id,
        故这是模型幻觉引用), trace 记 cite_drop, 答案附更正注。直接作答与强制收口两出口都调。

        第二层·硬尾纪律(§10.24): crawled=本题真爬过样本; 有样本却 0 引用又没明说判不了 = 违纪律。
        第三层·引文挂靠: 号都对得回的引用, 引文本身可能挂错了帖子(实测 6 题各中一处, 全是相邻 id 抄岔)。
        第三层先**改**再**标**(§10.50): 引文在本池里唯一落在别处时, 就地补挂/改挂真出处 ——
        这是确定性改正(依据是本题存档原文, 不是模型又说了什么), 与 _sanitize_cites 剔幻觉号同一路数;
        改不动的(查无出处/多处命中)才标注出来交人核。"""
        # 开头漏出的英文思考过程先删(§10.59): 提示词禁不掉, 只能在这层确定性剔除。
        answer, scratch = _strip_leading_scratch(answer)
        if scratch:
            trace.append({"event": "scratch_drop", "count": len(scratch),
                          "note": "剔除答案开头漏出的英文思考过程 %d 行: %s"
                                  % (len(scratch), scratch[0][:80])})
        # 先把「[id=49，like=530]」这类混写抠成 [id=49]: 不抠的话整条引用解析不出来 = 等于没标出处,
        # 而且前头那句引文会被算到下一条真引用名下, 复核时误判成"号挂错了"(§10.50 实测 q1 全部 24 处)。
        answer, junk = normalize_junk(answer)
        if junk:
            trace.append({"event": "cite_normalize", "count": junk,
                          "note": "抠掉 %d 处引用括号里混写的说明, 只留 id" % junk})
        answer, dropped = _sanitize_cites(answer, self.conn, self.scope)
        if dropped:
            trace.append({"event": "cite_drop", "dropped": dropped,
                          "note": "剔除 %d 条对不回本机证据池的引用" % len(dropped)})
            answer = (answer + "\n\n(证据校验: 已剔除 %d 条对不回本机存档的引用 %s)"
                      % (len(dropped), ",".join("#%d" % n for n in dropped))).strip()
        if crawled and not _discipline_ok(answer):
            trace.append({"event": "discipline_flag",
                          "note": "本题有真爬样本, 但答案 0 引用且未明说证据不足"})
            answer = (answer + "\n\n(纪律校验: 本题已取到真爬样本, 但答案未给 [id=N] 引用、"
                              "也未明说证据不足 —— 请按引用或避答口径复核)").strip()
        answer, fixed = repair_quotes(answer, self.conn, self.scope)
        if fixed:
            trace.append({"event": "cite_repair", "items": fixed,
                          "note": "改挂 %d 处引文到真出处" % len([f for f in fixed if "now" in f])})
        mis = [r for r in check_quotes(answer, self.conn, self.scope) if r["verdict"] != "ok"]
        if mis:
            trace.append({"event": "quote_misattributed", "items": mis,
                          "note": "%d 处引文不在所引帖子里, 实际出自别处" % len(mis)})
            detail = "; ".join("「%s」应在 [id=%s] 而非 [id=%d]" % (r["quote"], r["actual"], r["id"])
                               for r in mis[:3])
            answer = (answer + "\n\n(引文校验: %d 处引文不在所引帖子里 —— %s%s; 请核对后改引)"
                      % (len(mis), detail, " 等" if len(mis) > 3 else "")).strip()
        return answer

    # ---------- memory ----------
    def _load_relevant(self, user, hint):
        """回捞已翻篇结论。题面(widen)给的主题词越全, 追问能捞回的东西越多 ——
        原先只有"本轮 hint + 题面里点名的游戏"两路, 追问("那它最近呢?")一个词都不落,
        于是上一轮翻篇的结论全捞不回来(见 _recent_turns 的同类说明)。"""
        topics = [t for t in [hint] if t]
        topics += [g for g in GAME_HINTS if g in (user or "")]
        topics += [t for t in [self.card.anchor.get("game")] if t]   # 会话锚定的游戏(追问里往往不再点名)
        hits = self.log.query(topics)
        if not hits:
            return "(无)"
        return "\n".join("claim %s: %s | %s | %s" % (c, h["q"], h["concl"], h["ev"])
                         for c, h in hits.items())

    def _recent_turns(self, n=3):
        """本会话前几轮问答(最近 n 轮), 拼成 messages 里的 user/assistant 对。

        为什么需要它: 引擎每轮只发「一条 system + 一条 user」, 压根没有消息历史 —— 上一轮问了什么、
        得出过什么结论, 全靠状态卡与结论日志兜。而那两样都是**按主题词回捞**的(见 _load_relevant),
        换个问法就捞不着: 用户接着问"那它最近呢?", 一个主题词都不落, 上下文里就只剩一张空卡 ——
        表现就是答非所问、像换了个没记性的助手。

        这里补的是**逐轮流水**(flow, 按 scope 隔离成文件), 与本会话一一对应, 绝不跨会话串。
        assistant 那侧只带**结论行**、不带答案全文: 全文里全是 [id=N], 原样回灌等于当场教模型
        "照抄这些号", 而那一轮的证据这轮未必还在手上; 结论行足够接住话茬, 真要料它自己去 search_archive 捞。"""
        try:
            recs = flow.recent(self.scope, n)
        except Exception:
            return []
        out = []
        for r in recs:
            q = (r.get("question") or "").strip()
            if not q:
                continue
            out.append({"role": "user", "content": q})
            out.append({"role": "assistant", "content": (r.get("concl") or "").strip() or "(未收口)"})
        return out

    MEMO_BUDGET = 1600        # 纪要字数上限(字符): 到了就先丢最早的那些题, 会话再长也撑不爆上下文

    def _session_memo(self, keep_recent=3):
        """本会话**已翻篇**的那部分压成一份纪要 —— 这就是"回话压缩"。

        为什么需要: 上下文里只有最近 keep_recent 轮的原话(见 _recent_turns), 再往前的靠状态卡
        按主题词回捞, 而追问往往一个主题词都不落(「那你刚才说的第二点呢?」) —— 于是模型根本不
        知道自己早前答过哪几题, 表现就是"我们刚才聊过什么"答不上来、或拿最近一轮硬套。

        压缩是**确定性**的(不烧 LLM、不额外发请求): 每题一行「序号. 问 → 结论」。逼到字数上限时
        **先丢最早的题、永远保住会话开篇那一题**(那是整个对话的锚), 所以长度有界、不会随会话长大。
        要哪一题的原文细节, 模型自己调 recall_answer 把全文捞回来 —— 那才是没压缩的那份。"""
        try:
            recs = flow.records(self.scope)
        except Exception:
            return ""
        older = recs[:-keep_recent] if keep_recent > 0 else recs
        if not older:
            return ""                              # 会话还短, 原话都在下文, 不用纪要
        lines = []
        for i, r in enumerate(older, 1):
            q = re.sub(r"\s+", " ", (r.get("question") or "")).strip()
            c = re.sub(r"\s+", " ", (r.get("concl") or "")).strip()
            if not q:
                continue
            if len(q) > 60:
                q = q[:60] + "…"
            if len(c) > 90:
                c = c[:90] + "…"
            lines.append("%d. %s → %s" % (i, q, c or "(未收口)"))
        if not lines:
            return ""
        head = lines[0]                            # 会话开篇: 无论怎么丢都留着
        tail, used = [], len(head)
        for ln in reversed(lines[1:]):
            if used + len(ln) + 1 > self.MEMO_BUDGET:
                break
            tail.append(ln)
            used += len(ln) + 1
        kept = [head] + list(reversed(tail))
        body = "\n".join(kept)
        if len(tail) < len(lines) - 1:
            # 行首题号是**会话内序号**(与 recall_answer 的 index 同尺), 所以丢的是第 2..(n-len(tail)) 题
            body += "\n(中间第 2-%d 题压在字数上限外, 没摆进来 —— 用户提到哪题就 recall_answer 捞那题)" % (
                len(lines) - len(tail))
        return ("(本会话**早前**的问答纪要: 会话至今共 %d 题, 下面这 %d 题是其中**已翻篇的早前轮次**"
                "(行首题号 = 会话内第几题, 与 recall_answer 的 index 同一把尺); "
                "最近 %d 轮的原话在下文照常给出, 不在这里重复。"
                "用户说「你之前说的」「刚才那点」时, 指的就是下面这些题; 要某一题的**原话细节**"
                "(具体怎么措辞的、引了哪条帖子), 调 recall_answer 按序号或词把那一轮的答案全文捞回来, "
                "**别拿这行摘要当原话复述**。)\n%s" % (len(recs), len(kept), keep_recent, body))

    # ---------- L4 黑话还原 ----------
    def _alias_context(self, user, game_hint):
        """问句含注册表黑话(星铁/三蹦子…)且调用方没给显式 game_hint -> 还原成规范游戏名。
        返回 (game_hint, note): note 是追加给模型的系统提示(检索用规范名); 无命中/无表回落 (原样, None)。
        GAME_HINTS 子串探测(会话层)够不着昵称, 这里补最后一档: 昵称 -> 规范名进 game_hint 与检索。"""
        if game_hint:
            return game_hint, None
        hits = find_in_text(self.conn, user or "")
        if not hits:
            return None, None
        top = hits[0]
        return top["game"], "(用户黑话 %r 指 %s: 检索/归档一律填规范名 %s, 别让昵称进检索词)" % (
            top["alias"], top["game"], top["game"])

    def _materialize(self, answer, user, ev):
        self._n += 1
        cid = "M%d" % self._n
        # 结论只认干净『结论:』行; 没有/是意图残句 -> 诚实占位, 绝不拿残句或散文冒充历史结论
        concl = _conclusion(answer) or "(未收口)"
        self.card.materialize(cid, (user or "")[:80], concl, ev=ev)

    def _emit_flow(self, mode, user, game_hint, answer, ev, trace, hit, miss, out, rounds, ceiling,
                   est=None, t0=None, cancelled=False):
        """每题一条 jsonl 流水(scope 隔离文件, §10.8): answer 全文 + trace + 引用 + 用量。
        复盘/verifier/回放数据底; HARNESS_FLOW=0 可关。失败静默, 不影响问答主链。
        t0 = 本题起跑时间戳(见 ask 开头): 落 started_at/elapsed —— 只有它才能画出每题的**实际用时长**,
        光有收口时刻(ts)只知道"什么时候答完", 不知道"爬了多久"。

        hit/miss/out = 本题三档 token 的**真数**(缓存命中/未命中/输出, 见 _count)。缓存那两档必须分开落:
        命中的那部分往往占大头, 只存一个 prompt_tokens 总数, 事后谁也拆不回来看不出真实构成。"""
        try:
            flow.append(self.scope, {
                "mode": mode, "question": (user or "")[:500],
                # 与开头那条"开跑"标记共用的号: 靠它才认得出哪些开跑标记真收口了(见 flow.pending)
                "qid": getattr(self, "_qid", None),
                "game_hint": game_hint or "",
                "answer": answer, "concl": _conclusion(answer) or "(未收口)",
                "ev": ev, "trace": trace,
                # 用户点「停止」提前收口的那一题: 前端/PDF 据此标"只用了当时已取到的证据",
                # 免得这份答案被当成"跑完全程"的答卷。落流水而不是只挂在返回体: 刷新页面后还得认得出来。
                "cancelled": bool(cancelled),
                "rounds": rounds, "ceiling": ceiling,
                "started_at": (datetime.datetime.fromtimestamp(t0).isoformat(timespec="seconds")
                               if t0 else None),
                "elapsed": (round(time.time() - t0, 1) if t0 else None),
                "usage": {"prompt_tokens": hit + miss, "completion_tokens": out,
                          "cache_hit_tokens": hit, "cache_miss_tokens": miss,
                          "est_input_tokens": est if est is not None else 0,
                          "model": self._tk_model},
            })
        except Exception:
            pass

    # ---------- 「停止」/「暂停」 ----------
    def _stopped(self):
        """用户点了「停止」没。cancel 为 None(CLI/测试没传) -> 永远 False, 行为与从前一模一样。"""
        c = self._cancel
        return c is not None and c.is_set()

    def _paused(self):
        """用户点了「暂停」没。同 _stopped: 没传信号就永远 False。"""
        p = self._pause
        return p is not None and p.is_set()

    def _broken(self):
        """这一圈该收手了没(停止或暂停)。循环里问的都是这个 —— 两者在"别接着爬了"这一步上
        完全一样, 差别只在跳出循环之后: 停止要落答案, 暂停不落。"""
        return self._stopped() or self._paused()

    def _sleep(self, secs):
        """可被「停止」/「暂停」打断的等待: 分片睡, 最长 0.5 秒就能察觉。

        为什么不能直接 time.sleep: bili 的跨轮爬距最长要等 120 秒(风控型), 用户按了停止却还得
        干等两分钟 —— 那不是"停", 是"排队等停"。分片睡只在有信号时才多这点开销(每 0.5 秒醒一次),
        两个信号都没传时退化成原来的整段睡。"""
        evs = [e for e in (self._cancel, self._pause) if e is not None]
        if not evs:
            time.sleep(secs)
            return
        end = time.time() + secs
        while True:
            left = end - time.time()
            if left <= 0:
                return
            for e in evs:
                if e.wait(min(left, 0.5)):
                    return

    # ---------- 跨会话的爬取闸门 ----------
    def _gated(self, plat, fn):
        """把一次"要打平台"的动作放进跨会话闸门里跑(公用时排队, 见 harness/crawlgate.py)。

        gate 为 None(CLI/标定/自检)就直接跑 —— 那条路没有第二个会话, 行为与从前一模一样。
        被「停止」或「暂停」打断等待时返回 _STOPPED: 调用方据此把这次算成"没发出去"(绝不能当平台出错,
        否则点一下停止就把 bili 冷却五分钟)。**暂停的旗也要传进去** —— 只在门口等「停止」的话,
        前面那个人还在爬时按「暂停」要等到自己进门才生效, 看着就是卡死。
        """
        if self._gate is None:
            return fn()
        # 排队这件事**不在这里推事件**: 页面靠 /api/status 的 gate 视图显示"排队中/前面几个",
        # 那个数就是引擎真会等的数(同一个 view()) —— 这里再推一遍只是同一句话画两遍。
        if not self._gate.acquire(plat, self.scope,
                                  # 间距只有 bili 有(NGA 那侧没有风控型的爬距概念); 与 _precheck 同一个数
                                  gap=BILI_GAP.get(self._mode, 60) if plat == "bilibili" else 0.0,
                                  cancel=self._cancel, pause=self._pause):
            return _STOPPED
        try:
            return fn()
        finally:
            self._gate.release(plat)

    # ---------- 引擎级爬取闸门 ----------
    def _plat(self, plat):
        """取/建平台状态: 断路器(ask_fail, 每 ask 清) + 冷却(cool_until/fail_streak, 跨 ask)。

        cool_until/fail_streak 是**全机器一份**的(见 harness/cooldown.py): 被平台盯上的是这台机器的
        出口, 不是某个对话。这里那份只是**镜像**(写的时候一并更新, 给状态视图和人看); **判定一律
        以 cooldown 为准**(见 _precheck), 否则 B 会话拿着自己那份没更新过的镜像照样去爬。"""
        st = self._plats.get(plat)
        if st is None:
            st = self._plats[plat] = {"ask_fail": None, "fail_streak": 0,
                                      "cool_until": 0.0, "last_at": 0.0, "new": 0,
                                      # 上次真爬所在的"ask 序号 + 爬轮号": 判"同轮"用(同轮短等, 跨轮/跨 ask 长等)
                                      "last_round": None, "last_ask": 0}
        return st

    def _bili_keywords(self, game):
        """B站这一轮的检索词: **只有游戏本体名一个词**。

        口径是用户定的(2026-09-17): B站检索词就是本体本身, **不拼话题词** —— 「本体+话题词」拼出的
        长串只会返回泛话题热门视频(实测)。模型给的 query 在 B站这条线上不采纳(工具面里也没有 query 这一格)。

        **别名曾经一并发过(本体名 + 一个注册表别名), 2026-09-17 实测后撤掉**: 黑话在 B站 是**同名异物**
        的重灾区 —— 「三蹦子」搜出来全是三轮车(关税、断货那些几十上百万播放的车视频), 既是样本污染
        (那些闻和崩坏三没关系), 又会把服务拖到超时。注册表里的别名**只用于认出问句在说哪个游戏**
        (见 registry.find_in_text), 不再当检索词。要重新加回, 得先有"这个词在 B站 指的就是这家"的证据。
        """
        g = (game or "").strip()
        return [g] if g else []

    def _precheck(self, plat):
        """平台预检(主线程, 把本轮每个要爬的平台在并发调度前都按序跑一遍):
        断路器(本 ask) -> 会话冷却(bili) -> bili 爬距。
        挡: 返回阻塞消息 dict(没真爬, 不耗预算); 放行: 返回 None。"""
        st = self._plat(plat)
        now = time.time()
        # 平台比例端点硬压(语义 A 端点): 100=只NGA(禁 bili) / 0=只bili(禁 NGA)。
        # 内部 1..99 每轮证据配比偏向留阶段二; 熔断/冷却仍在此之下各自生效(比例挡的不是"失败")。
        if self._nga_ratio >= 100 and plat == "bilibili":
            return {"platform_error": "本 ask 用户设 NGA 占比 100%%(只爬 NGA): crawl_bili 已禁用, 别调; 转 crawl_nga/search_archive/作答",
                    "platform": plat, "note": "平台比例端点: 本 ask 单平台"}
        if self._nga_ratio <= 0 and plat == "NGA":
            return {"platform_error": "本 ask 用户设 NGA 占比 0%%(只爬 bilibili): crawl_nga 已禁用, 别调; 转 crawl_bili/search_archive/作答",
                    "platform": plat, "note": "平台比例端点: 本 ask 单平台"}
        if st["ask_fail"]:
            return {"platform_error": st["ask_fail"], "platform": plat,
                    "note": "本 ask 该平台已熔断: 用另一平台或 search_archive/直接作答"}
        # 冷却读**全机器那一份**(cooldown), 不读本会话那份镜像: 冷却的由头是这台机器被平台盯上了,
        # 别的对话刚吃过冷却, 这个对话也照样不能爬(见 harness/cooldown.py)。
        cool_left = cooldown.remaining("bilibili")
        if plat == "bilibili" and cool_left > 0:
            return {"platform_error": "bilibili 风控冷却中(还需~%ds), 本问别再爬 bilibili: 转 NGA 或 search_archive/作答" % int(cool_left),
                    "platform": plat}
        if plat == "bilibili":
            # 爬距分两档: 跨轮/跨 ask 用长等(风控); 同轮内同平台的多次短词调用只留 SAME_ROUND_GAP,
            # 且真正的串行发生在抓取阶段(_fetch_platform), 那里才是请求真正发出的时刻。
            # 「同轮」必须连 ask 序号一起认: 爬轮号每 ask 从 0 重数, 只看轮号的话, 上一题的第 0 轮
            # 与这一题的第 0 轮会撞成"同轮" —— 点「停止」立刻复问就成了绕开 bili 爬距的后门。
            same_round = (st.get("last_ask") == self._ask_seq and st.get("last_round") == self._n_crawl)
            # 续爬放行: 这一跑是接着上次暂停那道题, 且本 ask 还没爬过这个平台 —— 那就不等跨 ask 爬距。
            # 为什么该放: 暂停/续爬是**同一道题**的一次中断, 不是两次相邻的提问; 而且安全点保证
            # 上一次抓取早已结束落库, 中间还隔着一次模型调用和用户离开的那段时间。卡着一分钟爬距,
            # 用户看到的就是「继续」按下去先干等 —— 那不叫继续。
            # **只放爬距, 不放冷却**(st["cool_until"]): 那是真被抓过一次留下的风控信号,
            # 按一下按钮就洗掉, 等于拿服务器 IP 去赌, 不是这件事该省的。
            first_of_resume = bool(self._resume_from) and st.get("last_ask") != self._ask_seq
            if not same_round and not first_of_resume:
                gap = BILI_GAP.get(self._mode, 60)
                if st["last_at"] and now - st["last_at"] < gap:
                    self._sleep(gap - (now - st["last_at"]))   # 精准档最长 120s: 必须能被「停止」打断
            st["last_at"] = time.time()                     # 键在真实请求时刻(≈调度前)
            st["last_round"] = self._n_crawl
            st["last_ask"] = self._ask_seq
        return None

    def _mark_fail(self, plat, e):
        """一次真爬抛 CrawlError(平台挂, 非"没搜到") -> 熔断本 ask + (bili, 且是风控型)叠冷却。

        主线程串行调用: fetch 的异常在并发收集点收敛回主线程。
        **冷却只认风控型失败**(e.throttle, 由 live._raise_if_err 判定): 冷却的语义是"被平台盯上了,
        这台机器退一会儿", 服务没起/token 读不到/连不上这些跟平台无关的失败也照记五分钟, 用户看到的
        就是"爬虫根本没起, 界面却说被风控了", 而且真爬起来之后还得白等五分钟。熔断(ask_fail)照旧
        一律记 —— 本 ask 里这个平台确实已经不行了, 跟是不是风控无关。"""
        st = self._plat(plat)
        st["ask_fail"] = "%s" % e
        if plat == "bilibili" and getattr(e, "throttle", False):
            # 连熔断翻倍封顶 COOL_MAX, 成功一次归零; NGA 只熔断不冷却
            st["cool_until"] = cooldown.mark_fail("bilibili", COOL_START, COOL_MAX)
            st["fail_streak"] = cooldown.streak("bilibili")
        return {"platform_error": "%s" % e, "platform": plat}

    def _after_crawl(self, args, res):
        """一次成功真爬后的主线程收尾: 平台新样本累计 + 软注挂到返回体。

        plat_total 与 ask_total 必须**同时**给: ask_total 是两平台累计, 却挂在单平台返回体上, 只给它
        会诱导模型把「NGA 那次返回里的 244」读成「NGA 有 244 条」(实测 Q13 把样本量虚报 1.8 倍)。
        shown 是本题展示给模型看的前 N 条; 少于归档量时明说, 免得模型把「我没看到」当「社区没有」。"""
        plat = args["platform"]
        st = self._plat(plat)
        if plat == "bilibili":
            cooldown.mark_ok("bilibili")                    # bili 成功归零(全机器那份); NGA 从不叠 streak
            st["fail_streak"] = 0
        new = res["summary"].get("new", 0)
        st["new"] += new
        res["plat_total"] = st["new"]                       # 本平台本题累计新增: 报"NGA 多少条"用这个
        tot = sum(s["new"] for s in self._plats.values())
        res["ask_total"] = tot                              # 两平台累计: 软参考, 不是某个平台的条数
        notes = []
        shown = res["summary"].get("shown")
        if shown is not None and new > shown:
            notes.append("本轮新归档 %d 条, 只向你展示前 %d 条(其余仍在本题存档, 可 search_archive 回捞); "
                         "你没看到 ≠ 社区没有" % (new, shown))
        if new == 0:
            # 0 新增分两种, 提示完全不同: "都搜过了(重复)"是方向已采完, "一条没搜到"才是词没造对。
            dup = res["summary"].get("dup", 0)
            if dup:
                notes.append("本平台本轮 0 新增(其中 %d 条是本题早先已归档过的重复): 这个方向已经采过了, "
                             "原样重爬拿不到新料 —— 换关键词方向、或去爬题目要求的另一边, 别重复烧爬轮" % dup)
            else:
                notes.append("本平台本轮 0 命中: NGA 版面检索是「全词与」, 检索词叠加越多越必然为空 —— "
                             "去掉限定词, 只留游戏名+1 个核心词再试, 别原样重试")
        tgt = SAMPLE_TARGET.get(self._mode, 120)
        if tot >= tgt:
            notes.append("本 ask 累计新增 %d ≥ 参考量 %d: 各需覆盖的侧面若都已取到证据, 即可收口作答, 不必为凑量继续爬" % (tot, tgt))
        if notes:
            res["note"] = " ".join(notes)
        return res

    def _ratio_nudge(self):
        """阶段二内部引导(1..99, 软): 用本 ask 两平台累计新增证据量算 NGA 占比,
        偏离目标 >RATIO_TOL 个百分点且建议平台当前未熔断/冷却时, 返回一句提示让模型下轮优先补欠配比边;
        否则 None。只提示不改调度, 不保证精确(±5 即达要求), 别为凑比例硬爬。
        端点 0/100 由 _precheck 硬压管, 不在这。"""
        r = self._nga_ratio
        if not (0 < r < 100):
            return None
        nga = self._plats.get("NGA", {}).get("new", 0)
        bil = self._plats.get("bilibili", {}).get("new", 0)
        tot = nga + bil
        if tot <= 0:
            return None
        share = 100.0 * nga / tot
        now = time.time()
        if share < r - RATIO_TOL:
            if not self._plat("NGA")["ask_fail"]:            # NGA 只熔断不冷却
                return "(配比提示: 当前 NGA %.0f%% < 目标 %d%%, 本轮优先 crawl_nga 补 NGA 样本; 两边都采, 别只采一边)" % (share, r)
        elif share > r + RATIO_TOL:
            st = self._plat("bilibili")
            if not (st["ask_fail"] or now < st["cool_until"]):
                return "(配比提示: 当前 NGA %.0f%% > 目标 %d%%, 本轮优先 crawl_bilibili 补 bilibili 样本; 两边都采, 别只采一边)" % (share, r)
        return None

    # ---------- one user turn ----------
    def ask(self, user, game_hint=None, mode="快速", nga_ratio=None, cancel=None, emit=None,
            pause=None, resume_from=None):
        """cancel = threading.Event(可选): 用户点「停止」时置位。引擎只在**安全点**认它 ——
        转圈的首尾、真爬发出去之前、以及任何等待里(见 _sleep)。已发出去的那一次抓取不硬掐:
        掐断只会让爬虫服务还占着单飞锁、拿到的样本也白丢; 让它跑完落库, 下一圈开头就收手。

        pause = threading.Event(可选): 用户点「暂停」时置位。安全点与收手时机跟 cancel 一模一样,
        **区别只在下场之后**: 停止落一份"用已有证据作答"的短答案, 暂停**一个字都不落** ——
        已爬到的样本本来就在库里(爬取即归档), 这题在流水里留一条 paused 记录挂着,
        用户下次回来点「继续」时从库里那批样本接着爬。所以暂停不能当停止办: 落了答案
        这题就算答过了, 再来一次就是同一题答两遍。

        resume_from = 被暂停那条流水记录(dict, 可选): 传了就是"接着上次那道题跑"。
        用它两件事: ① 提示词里点明"上次已经爬到 N 条、先 search_archive 回捞别再从头爬";
        ② 首轮的 bili 爬距放行 —— 暂停+续爬是**同一道题**的一次中断, 不是两次相邻的 ask 打平台
        (安全点保证上一次抓取早已落库), 卡着一分钟爬距等于"继续"按钮按下去先干等两分钟。

        emit = 实时事件的回调(可选; 必须线程安全, 见 Engine.__init__)。传了它就走**流式**:
        模型每吐一段正文立刻推出去, 前端边收边画; 不传则一切照旧(阻塞式、整份回来再给)。"""
        t0 = time.time()                     # 本题起跑时刻: 落进流水, 供"会话时间轴"画每题实际用时长
        # 这一题的"开跑"标记(见 flow.starts): 落一条**只有问句、没有答案**的流水, 与收口那条共用一个 qid。
        # 为什么要它: 一题要爬几分钟, 中途进程断了/被重启, 光靠收口那条流水的话这个对话在整个记忆里
        # 就什么都没发生过 —— 侧栏列不出来、点进去也没有这一题, 用户只看见自己问过的话凭空没了。
        self._qid = uuid.uuid4().hex[:12]
        try:
            flow.append(self.scope, {
                "event": "start", "qid": self._qid, "question": (user or "")[:500], "mode": mode,
                "started_at": datetime.datetime.fromtimestamp(t0).isoformat(timespec="seconds")})
        except Exception:
            pass                # 同 _emit_flow: 落流水的锅不许砸在问答主链上(磁盘满/没权限也得能答)
        self._tk_hit = self._tk_miss = self._tk_out = 0         # 本题的账清零(上一题的不许滚进来)
        budget = CRAWL_BUDGET.get(mode, 3)   # 爬轮硬上限: 同轮双平台(crawl_nga+crawl_bili 并发)=1 轮
        tgt = SAMPLE_TARGET.get(mode, 120)   # 参考量(软), 只进提示供自判, 不硬停
        self._cancel = cancel
        self._pause = pause
        self._resume_from = resume_from      # 非 None = 这一跑是接着上次暂停那道题(见 _precheck)
        self._on_event = emit
        self._mode = mode
        self._nga_ratio = clamp_ratio(DEFAULT_NGA_RATIO if nga_ratio is None else nga_ratio)
        self._n_crawl = 0
        self._ask_seq += 1                   # 新的一题: 让"同轮"判定不认上一题的轮号(见 _precheck)
        self._official_used = False          # 官号探针一次/ask: 每 ask 重新放行
        self._official_tries = 0
        self.ctx["_sub_left"] = None         # 字幕额度: None = 满额(口径在 crawl_live.SUB_PER_ROUND), 每轮重来
        for st in self._plats.values():      # ask 作用域: 每 ask 清 ask_fail 与新增计数(会话冷却保留)
            st["ask_fail"] = None
            st["new"] = 0
        # L4 黑话还原: 无显式 game_hint 且问句含注册表昵称 -> 用规范名当游戏上下文(检索/工具缺省填它)
        game_hint, alias_note = self._alias_context(user, game_hint)
        # 兄弟作防火墙: 钉死某游戏时(如 明日方舟), 检索词与归档样本都不得跑到同 IP 衍生作
        # (明日方舟终末地)去 —— 两边玩家混在同一批版块, 关键词一命中就会被当成"本家社区声音"。
        self._sib_terms = sibling_terms(self.conn, game_hint) if game_hint else []
        # 时间锚点: 模型看不到"今天"就没法判"近期" —— 2026-09 的问句会把 2025-11 的事当"最近发生"。
        # 带时效词时同时给出时间窗下界(入档闸门用它丢更早样本), 并写进提示词要求答案标样本日期区间。
        self._now = now_str()
        # 显式时间锚点("三个月前"/"去年"/"2024年6月"): 历史题按锚点开窗取数, 与"近期"的 recency 语义互斥。
        # 对比题(「今年…跟去年…比」)会命中多段 -> 分段各翻一窗(见 time_windows), 并集只用于文案/入档闸门。
        self._win_windows = time_windows(user, self._now)
        self._win_since, self._win_until = time_anchor(user, self._now)
        self._since, days = recency_since(user, self._now)
        if self._win_since:
            self._since, days = self._win_since, 0   # 入档下界 = 窗口起点(不再是"近 N 天")
        # 工具面: 每 ask 现组(设置页里改开关/加外部服务, 下一题就生效)。外部服务的工具清单
        # 在这里一次性取好并复用 —— 别在循环里反复去连(每次都可能起一个子进程)。
        ext_tools = self._ext_tools()
        self._tools = self._tool_defs(ext_tools)
        system = _base_system(mode, budget, tgt) % (
            self.card.render(), self._load_relevant(user, game_hint))
        if any(ext_tools.values()):
            system += (
                "【外部工具】本 ask 的工具面里另有一批**用户在设置里自己加的外部工具**(名字带 mcp__ 前缀)。"
                "它们是别人写的服务, 可能超时或报错 —— 那次调用失败就如实说这一回没取到, 换自带工具或按已有证据作答, "
                "别反复硬试; 也不要把外部工具返回的内容当成自带爬虫的样本去引 [id=N]。\n")
        # 本会话前几轮的问答先摆上, 再摆本轮的问句 —— 追问(「那它最近呢?」)才接得住。
        # 顺序要紧: 必须在本轮 user 之前, 否则模型会当成"答完之后又补了句"。
        messages = [{"role": "system", "content": system}]
        memo = self._session_memo()             # 早前翻篇的那些题: 压成纪要, 只带题号+结论行
        if memo:
            messages.append({"role": "system", "content": memo})
        messages += self._recent_turns()
        messages.append({"role": "user",
                         "content": "(当前时间 %s。参考样本量 ~%d, 够不够由你每轮比对 ask_total 判断, "
                                    "别再为凑数硬爬; 用户游戏上下文可能是 %s)\n%s" % (
                             self._now, tgt, game_hint or "未知", user)})
        if self._win_since:
            if len(self._win_windows) > 1:
                spans = " 与 ".join("%s~%s" % (a, b) for a, b in self._win_windows)
                messages.append({"role": "system", "content": (
                    "(本题是**跨时段对比**: 目标含 %d 段 —— %s。NGA 与 bilibili **两侧都**已按段各取一窗"
                    "(不是合成一个大窗, 免得不小心只取到最新一截)。检索词(query)里仍"
                    "**不要**写时间词(「去年」「今年」等): 时段由时间窗负责。"
                    "作答时先按每条证据的 published_at 把它归到对应时段, **两段各自归纳再对比**; "
                    "某段材料不足就直说\"该时段材料不足\", 绝不许拿另一段的料冒充。)"
                    % (len(self._win_windows), spans))})
                trace = [{"event": "time_window_multi", "windows": self._win_windows}]
            else:
                messages.append({"role": "system", "content": (
                    "(本题带显式时间锚点: 目标是 %s ~ %s 这段的社区言论。NGA 与 bilibili 都已按该时段取数, "
                    "但两侧**都只保证取到该时段里较靠前的一截**, 更深的月份可能取不全 —— 别据此断言\"那会儿没人讨论\"。"
                    "检索词(query)里**不要**再写时间词(「2023年」「去年」「半年前」等): "
                    "时段由时间窗负责, 时间词混进检索词只会把命中打成 0; 只填游戏自己的话题词(版本号/角色名/玩法)。"
                    "作答时先核每条证据的 published_at: 只有落在该时段内的才算该时段的声音, "
                    "落在时段外的要写明那是别的时期。若该时段确实材料不足, 就直说\"该时段材料不足\""
                    "(原因是社区少有人讨论、或爬虫没翻到那么深), 绝不许拿别的时段的料冒充。)"
                    % (self._win_since, self._win_until))})
                trace = [{"event": "time_window", "since": self._win_since, "until": self._win_until}]
        elif self._since:
            messages.append({"role": "system", "content": (
                "(本题带时效词: 只认 published_at >= %s 的样本(近 %d 天), 更早的已在入档时被过滤。"
                "作答时先看证据行的 published_at, 明确写出证据落在哪个日期区间; "
                "不得把窗外时间点发生的事说成\"近期/最近\"——若近 %d 天内确实没有料, 就如实说\"近期无有效样本\"。)"
                % (self._since, days, days))})
            trace = [{"event": "recency_window", "since": self._since, "days": days}]
        else:
            trace = []
        if alias_note:
            messages.append({"role": "system", "content": alias_note})
        # 平台比例前置提示: 端点=单平台让模型别再浪费回合调被禁工具(与 _precheck 硬压一致);
        # 内部 1..99 = 目标配比软引导(每轮 _ratio_nudge 再按实时偏差追一句)。
        r = self._nga_ratio
        if r >= 100:
            messages.append({"role": "system",
                             "content": "(用户平台比例: NGA 100%% → 本 ask 仅爬 NGA; crawl_bili 已禁用, 别再调 crawl_bili)"})
        elif r <= 0:
            messages.append({"role": "system",
                             "content": "(用户平台比例: NGA 0%% → 本 ask 仅爬 bilibili; crawl_nga 已禁用, 别再调 crawl_nga)"})
        elif 0 < r < 100:
            messages.append({"role": "system",
                             "content": "(用户平台占比: NGA %d%% / bilibili %d%%; 按这个配比取两侧新增样本, 偏差别超 %d 个百分点, 但某平台熔断/冷却或真没料时别为凑比例硬爬)" % (
                                 r, 100 - r, RATIO_TOL)})
        # 续爬提示: 上次暂停时已经爬到的那批样本**还在本机库里**(爬取即归档, 与这一跑无关) ——
        # 不说这一句, 模型会把它当一道全新的题从头再爬一遍, 上次那几轮的样本量和爬距全白费。
        if resume_from:
            _got, _rnd = int(resume_from.get("got") or 0), int(resume_from.get("rounds") or 0)
            messages.append({"role": "system", "content": (
                "(这题上次被用户**暂停**过, 不是新题: 当时已经爬了 %d 轮、取到 %d 条样本, 都还在本机存档里。"
                "先用 search_archive 把那批捞回来核过原文, 再只补没覆盖到的方面 —— 别从头重爬一遍。"
                "作答要求与平常一致: 只引能核回存档的 [id=N], 末行「结论: <一句话>」。)" % (_rnd, _got))})
            trace.append({"event": "resume", "rounds": _rnd, "got": _got})
        # token 那条账不在这儿累加: 每一炮回来都由 _llm 记进 self._tk_*(收口那次重写也记)。
        # 在这儿再累一遍会把同一份 token 算两遍钱。
        run_ids, rounds = [], 0            # trace 上面已起(时效窗事件)
        if alias_note:
            trace.append({"event": "alias_resolve", "note": alias_note[:90]})
        in_est, last_prompt = 0, 0             # in_est=累计估算(报告用); last_prompt=上轮 provider 报的真实 prompt 尺寸
        ceiling = _in_ceiling(mode)
        stopped = False                        # 用户点过「停止」: 跳出循环后走"用已有证据收口"那条路
        paused = False                         # 用户点过「暂停」: 跳出循环后**不落答案**, 只挂一条记录
        for rnd in range(6):
            # 转圈开头是最干净的安全点: 上一轮的样本已全部落库, 此刻收手不留半截状态。
            # 第 0 圈也查 —— 用户可能在第一次模型调用还没回来时就按了停止/暂停。
            # 暂停先判: 两个按钮都按过时(手快), 以"什么答案都不落"为准 —— 宁可多留一条能续的记录,
            # 也别把一道用户明说了"先搁着"的题收成一份短答案。
            if self._paused():
                paused = True
                trace.append({"event": "paused", "round": rnd,
                              "note": "用户点了「暂停」: 停止取数, 已爬到的样本留在库里等继续"})
                break
            if self._stopped():
                stopped = True
                trace.append({"event": "cancelled", "round": rnd,
                              "note": "用户点了「停止」: 停止取数, 用已取到的证据收口"})
                break
            # 全量证据后每轮上下文都会明显长大, 护栏必须量"这一轮真正要发出去的那一份"
            # (含刚回填的工具结果), 而不是上一轮的旧尺寸 —— 否则一轮巨型返回就能把窗口顶爆。
            if rnd > 0 and max(_est_input_tokens(messages), last_prompt) > ceiling:
                messages.append({"role": "system",
                                 "content": "(已达本问上下文输入上限 %d: 直接用已取证据作答, 别再调工具)" % ceiling})
                trace.append({"ctx": "input ceiling %d" % ceiling})
                break
            self._emit({"type": "round", "n": rnd + 1})
            resp = self._llm(messages, stream=self._on_event is not None)
            rounds += 1
            last_prompt = resp.get("usage", {}).get("prompt_tokens", 0)
            in_est += _est_input_tokens(messages)
            m = resp["choices"][0]["message"]
            tcs = m.get("tool_calls")
            # total = 本题**到这一刻为止**累计的消耗, 实时画面据此边爬边把数攒给用户看
            self._emit({"type": "usage", "prompt_tokens": last_prompt,
                        "completion_tokens": resp.get("usage", {}).get("completion_tokens", 0),
                        "total": self._tk_total()})
            if tcs:
                # 这一轮要说的只是"我这就去爬"的前言。叫前端把这段草稿丢掉 —— 否则那几句
                # "我查查社区怎么说"留在页面上, 看着像答案却不是。
                self._emit({"type": "delta-reset"})
                self._emit({"type": "tools",
                            "names": [(tc.get("function") or {}).get("name") for tc in tcs]})
            if not tcs:
                answer = self._seal_answer(self._final_answer(messages, m.get("content") or "(空)"), trace,
                                           crawled=bool(run_ids))
                ev = ",".join("run#%s" % rid for rid in run_ids) if run_ids else "cites"
                self._materialize(answer, user, ev)
                self._emit_flow(mode, user, game_hint, answer, ev, trace,
                                self._tk_hit, self._tk_miss, self._tk_out,
                                rounds, ceiling, est=in_est, t0=t0)
                return {"answer": answer, "trace": trace, "ctx": {"rounds": rounds, "ceiling": ceiling},
                        "prompt_tokens": self._tk_hit + self._tk_miss,
                        "completion_tokens": self._tk_out,
                        "est_input_tokens": in_est, "cancelled": False}
            messages.append(m)
            # 预算按"轮"计: 一条 assistant 消息里成功真爬过 ≥1 个 crawl_/ 就占 1 爬轮(同轮
            # NGA+bilibili 双平台各爬一次 = 1 轮); 预算在"成功真爬的轮"末尾 +1, 全被平台闸门挡(预检/真爬失败)的不算。
            # 并发安全: 本轮多个 crawl 的 fetch(provider 抓取, 不碰 DB)跨平台并发、同平台串行(单飞服务端);
            # 落库/计数/熔断回主线程串行(single-writer)。
            crawls = [tc for tc in tcs if (tc.get("function") or {}).get("name") in CRAWL_TOOLS]
            round_ok = not crawls or self._n_crawl < budget
            round_ran = False
            round_new = 0                             # 本轮两平台新增合计(0 = 整轮空转, 见轮末提示)
            plan, recs, results = [], {}, {}          # plan=(idx,name,args) 通过预检待并发抓; 工具返回体按原序回填
            for idx, tc in enumerate(tcs):
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                name = fn["name"]
                if name in tool_registry.BY_NAME and not tool_registry.enabled(name):
                    # 工具面里已经没有它了, 正常不该有人调 —— 真调了就如实挡回去, 别硬跑。
                    msg = "%s 现在是关的(设置页「工具」里关掉了), 用别的工具或直接作答" % name
                    results[idx] = {"error": msg}
                    recs[idx] = {"tool": name, "blocked": msg[:120]}
                    continue
                if name in CRAWL_TOOLS:
                    if self._broken():
                        # 停止/暂停后模型若还发了爬取调用(它看不到这两个按钮), 别真发出去 ——
                        # 挡成受控 tool-error, 让它这一轮空手过去, 转圈开头就收手了。
                        msg = ("用户已暂停本题: 不再真爬, 已取到的样本留在库里等继续" if self._paused()
                               else "用户已停止本题: 不再真爬, 用已有证据作答")
                        results[idx] = {"error": msg}
                        recs[idx] = {"tool": name, "blocked": msg}
                        continue
                    args["game"] = _norm_game(args.get("game"), game_hint)
                    args["platform"] = CRAWL_TOOLS[name]["platform"]   # 平台由注册表固定, 不认模型传参
                    q = (args.get("query") or "").strip()
                    if name == "crawl_bili":
                        # B站检索词不由模型给: 引擎按游戏本体名发(见 _bili_keywords)。
                        # query 一并填成那个词: record_crawl 的 source_query / 摘要 / 前端"正在爬 X"都读它。
                        args["keywords"] = self._bili_keywords(args["game"])
                        args["query"] = q = args["keywords"][0] if args["keywords"] else ""
                    if not q:
                        # NGA 侧 schema 的 query 必填; 模型偶发漏传会直接炸 live provider(query 为必填位置参),
                        # 挡成受控 tool-error 让模型补词重调, 不整题报废(勿丢进并发池)。
                        msg = ("%s 缺 game 游戏名: B站检索词由引擎按游戏本体名发, 不填游戏名就没法搜" % name
                               if name == "crawl_bili" else
                               "%s 缺 query 检索词: 给具体话题词(版本/角色/争议点)再调, 别空爬" % name)
                        results[idx] = {"error": msg}
                        recs[idx] = {"tool": name, "game": args["game"], "platform": args["platform"],
                                     "blocked": msg[:120]}
                        continue
                    args["query"] = q
                    # 兄弟作防火墙看**实际发出去的每个词**(B站那轮的词表在 args["keywords"] 里)
                    q_chk = " ".join(args.get("keywords") or [q])
                    hit = _sib_hit(q_chk, self._sib_terms)
                    if hit:
                        # 检索词越界到兄弟作: 挡成受控 tool-error 让模型改词, 不落库(污染样本进来就洗不掉)
                        msg = ("检索词 %r 越界到兄弟作 %r(本 ask 钉死 %s): 换回 %s 自己的话题词再爬; "
                               "%s 的社区声音不算 %s 的证据") % (q_chk, hit, game_hint, game_hint, hit, game_hint)
                        results[idx] = {"error": msg}
                        recs[idx] = {"tool": name, "game": args["game"], "platform": args["platform"],
                                     "blocked": msg[:120]}
                        continue
                    args["exclude_terms"] = self._sib_terms
                    args["exclude_before"] = self._since      # 空字符串 = 历史题, 入档不设下界
                    if self._win_since:                       # 显式时间锚点: 让爬虫翻页取该时段
                        args["since"] = self._win_since
                        args["until"] = self._win_until
                        if len(self._win_windows) > 1:        # 对比题: 分段各一窗, 别让爬虫只取最新一截
                            args["windows"] = self._win_windows
                    if not round_ok:
                        results[idx] = {"error": "本 ask 爬轮预算已用完(%d/%d): 别再爬了, 换 search_archive 或直接作答" % (
                            self._n_crawl, budget)}
                        recs[idx] = {"tool": name, "game": args["game"], "platform": args["platform"],
                                     "blocked": results[idx]["error"][:120]}
                    else:
                        pre = self._precheck(args["platform"])
                        if pre:
                            results[idx] = pre
                            recs[idx] = {"tool": name, "game": args["game"], "platform": args["platform"],
                                         "blocked": (pre.get("platform_error") or pre.get("note") or "")[:120]}
                        else:
                            plan.append((idx, name, args))
                elif name == "search_archive":
                    args["game"] = _norm_game(args.get("game"), game_hint)
                    args["since"] = self._since          # 时效题: 历史归档也只看窗内, 与入档闸门同一把尺
                    results[idx] = search_archive.call(self.ctx, args)
                    recs[idx] = {"tool": name, "game": args["game"],
                                 "total": results[idx].get("total"),
                                 "returned": len(results[idx].get("rows", []))}
                elif name == "crawl_official":
                    # 官号探针: 主线程串行(不并发, 避免与 bili 的 /crawl 抢服务端单飞锁)。
                    # 平台钉死 bilibili(账号解析/登录态都在 bili 侧), 走 bili 的预检(熔断/冷却/爬距)。
                    # 成功一次即锁死本 ask; 失败(账号没认准)最多再给一次机会 —— 单次探针要 20-40s,
                    # 不设上限的话模型拿着错游戏名能一直重试。
                    og = _norm_game(args.get("game"), game_hint)
                    if self._broken():
                        msg = ("用户已暂停本题: 官号探针不发了, 已取到的样本留在库里等继续" if self._paused()
                               else "用户已停止本题: 官号探针不发了, 用已有证据作答")
                        results[idx] = {"error": msg}
                        recs[idx] = {"tool": name, "game": og, "blocked": msg}
                    elif self._official_used:
                        msg = "本 ask 官号探针已经用过一次: 一个 ask 只探一次官方账号, 用已有证据作答或换 search_archive"
                        results[idx] = {"error": msg}
                        recs[idx] = {"tool": name, "game": og, "blocked": msg[:120]}
                    elif self._official_tries >= 2:
                        msg = "本 ask 官号探针已试过 2 次仍没成(账号没认准/取不到): 别再试了, 如实说「官号探针没取到」并转 crawl_nga/crawl_bili 或直接作答"
                        results[idx] = {"error": msg}
                        recs[idx] = {"tool": name, "game": og, "blocked": msg[:120]}
                    elif not round_ok:
                        results[idx] = {"error": "本 ask 爬轮预算已用完(%d/%d): 别再爬了, 换 search_archive 或直接作答" % (
                            self._n_crawl, budget)}
                        recs[idx] = {"tool": name, "game": og, "blocked": results[idx]["error"][:120]}
                    else:
                        pre = self._precheck("bilibili")
                        if pre:
                            results[idx] = pre
                            recs[idx] = {"tool": name, "game": og,
                                         "blocked": (pre.get("platform_error") or pre.get("note") or "")[:120]}
                        else:
                            self._official_tries += 1
                            # 时段跟着题目走: 显式锚点(去年/2024年6月)用该窗; 只有时效词用 recency 下界;
                            # 都没有 -> 不传, 服务端默认近一个版本周期。**不是"最近 10 条"**。
                            op = {"game": og, "platform": "bilibili", "query": "官号探针:%s" % og,
                                  "exclude_terms": self._sib_terms, "exclude_before": self._since,
                                  "since": self._win_since or self._since,
                                  "until": self._win_until}
                            # 探针单次要 20-40 秒, 前端得知道它在跑
                            self._emit({"type": "tool-start", "name": name,
                                        "platform": "bilibili", "query": op["query"]})
                            try:
                                # 官号探针打的也是 bili 的同一个服务, 同样要过闸门(否则两个人同时探号
                                # 一样会被 429 挡回, 一样被误判成风控)
                                items = self._gated("bilibili",
                                                    lambda: crawl_tools.official(self.ctx, op))
                                if items is _STOPPED:
                                    msg = "用户已停止本题: 官号探针没发出去就停了"
                                    results[idx] = {"error": msg}
                                    recs[idx] = {"tool": name, "game": og, "platform": "bilibili",
                                                 "blocked": msg[:120]}
                                    self._emit({"type": "tool-end", "name": name,
                                                "platform": "bilibili", "blocked": msg})
                                    continue
                            except CrawlError as e:
                                # 账号没认准/动态流失败不是 bili 风控事件 -> 只当普通 tool-error, 不熔断平台
                                if e.throttle:
                                    results[idx] = self._mark_fail("bilibili", e)
                                else:
                                    results[idx] = {"error": "%s" % e}
                                recs[idx] = {"tool": name, "game": og,
                                             "blocked": str(results[idx].get("platform_error")
                                                            or results[idx].get("error"))[:120]}
                                self._emit({"type": "tool-end", "name": name, "platform": "bilibili",
                                            "blocked": recs[idx]["blocked"]})
                            else:
                                self._official_used = True
                                res = self._after_crawl(op, crawl_tools.commit(self.ctx, op, items))
                                new = res["summary"].get("new", 0)
                                if not new:
                                    # 基类那条"0 命中"提示是说 NGA 检索词的, 官号探针不适用
                                    res["note"] = ("官号探针本轮 0 条新样本(这些动态此前已归档过, 属正常): "
                                                   "看返回里 is_peak 标出的高峰动态与评论即可, 别读成'官方没发东西'")
                                run_ids.append(res["summary"]["run_id"])
                                results[idx] = res
                                recs[idx] = {"tool": name, "game": og, "platform": "bilibili",
                                             "new": new, "dup": res["summary"]["dup"],
                                             "run": res["summary"]["run_id"],
                                             "ask_total": res.get("ask_total")}
                                self._emit({"type": "tool-end", "name": name,
                                            "platform": "bilibili", "new": new,
                                            "dup": res["summary"]["dup"],
                                            "ask_total": res.get("ask_total")})
                                round_ran = True
                elif name == "recall_answer":
                    # 会话回捞(本 scope 流水): 不占爬轮预算、不落库、不触网。追问要细节时走它。
                    results[idx] = tool_registry.call(self.ctx, name, args)
                    recs[idx] = {"tool": name, "returned": len(results[idx].get("items") or []),
                                 "total": results[idx].get("total"),
                                 "error": bool(results[idx].get("error"))}
                elif name == "nlp_score":
                    # 可选工具(默认不开, 本机没打分服务时也不在工具面上)。真到这儿说明本机有
                    # 服务, 按注册表点名叫它干活 —— 不占爬轮预算, 不落库, 只是个信号。
                    results[idx] = tool_registry.call(self.ctx, name, args)
                    recs[idx] = {"tool": name, "error": bool(results[idx].get("error"))}
                elif name.startswith("mcp__"):
                    # 用户自己加的外部工具服务: 转给它跑。主线程串行(不并发, 也不占爬轮预算 ——
                    # 它爬的样本不进我们的库, 引不成 [id=N], 提示词里已交代这一点)。
                    results[idx] = self._call_external(name, args)
                    recs[idx] = {"tool": name, "external": True,
                                 "error": bool(results[idx].get("error"))}
                else:
                    results[idx] = {"error": "unknown tool " + name}
                    recs[idx] = {"tool": name, "error": True}
            if plan and self._broken():
                # 预检都过了、正要发出去的那一刻被停/被暂停: 一条都别发(免得白打平台、白占爬虫单飞锁),
                # 全部按"没发出去"回填, 好让下面那条回填循环把 tool 消息配对补齐(缺一条请求就非法)。
                for idx, name, args in plan:
                    msg = ("用户已暂停本题: 这一条检索没发出去" if self._paused()
                           else "用户已停止本题: 这一条检索没发出去")
                    results[idx] = {"error": msg}
                    recs[idx] = {"tool": name, "game": args.get("game"),
                                 "platform": args.get("platform"), "blocked": msg[:120]}
                plan = []
            if plan:
                def _do_fetch(args):
                    try:
                        return crawl_tools.fetch(self.ctx, args)
                    except CrawlError as e:
                        return e

                def _fetch_platform(plat, items):
                    """同一平台本轮内的多次短词调用: 服务端单飞(并发同平台会被 429 挡回), 串行发、
                    中间留 SAME_ROUND_GAP; 跨平台之间仍并发(不同平台互不挡)。
                    每条发之前认一次「停止」—— 一轮里可能排着好几个短词, 停在第 1 个之后就不该
                    把剩下几个接着打完(那是白白多打平台、多等一分多钟)。

                    **整批占一次闸**(见 _gated): 一轮里那几个短词本来就是同一个动作, 中间只该隔
                    SAME_ROUND_GAP(12 秒), 不该被平台级的间距(60/120 秒)拆开。"""
                    def _run():
                        out = {}
                        for n, (idx, _name, args) in enumerate(items):
                            if self._broken():
                                out[idx] = _STOPPED
                                continue
                            if n:
                                self._sleep(SAME_ROUND_GAP)
                            # 真发出去之前推一条(worker 线程里推, 所以 _on_event 必须线程安全):
                            # 这一条可能要跑几十秒到几分钟, 前端靠它把"正在爬 X"那行立起来
                            self._emit({"type": "tool-start", "name": _name,
                                        "platform": plat, "query": args.get("query")})
                            out[idx] = _do_fetch(args)
                        return out

                    got = self._gated(plat, _run)
                    if got is _STOPPED:
                        # 在门口等的时候被点了「停止」: 这几条都没发出去, 与下面的 _STOPPED 同义
                        return {idx: _STOPPED for idx, _n, _a in items}
                    return got

                by_plat = {}
                for idx, name, args in plan:            # 保序: 同平台按模型给的调用顺序发
                    by_plat.setdefault(args["platform"], []).append((idx, name, args))
                with ThreadPoolExecutor(max_workers=min(len(by_plat), 4)) as ex:
                    futs = [ex.submit(_fetch_platform, plat, items) for plat, items in by_plat.items()]
                    fetched = {}
                    for f in futs:
                        fetched.update(f.result())   # 非 CrawlError 异常照原样上抛(与原串行行为一致)
                for idx, name, args in plan:                    # 主线程串行: 失败熔断 / 成功落库+计数+翻篇
                    got = fetched[idx]
                    if got is _STOPPED:
                        # 被停掉没发出去的那几条: 只当"这轮没爬成", **不是**平台出错 ——
                        # 走 _mark_fail 会顺手熔断+冷却 bili, 等于用户停一下就把平台关了五分钟。
                        msg = "用户已停止/暂停本题: 这一条检索还没发出去就停了"
                        results[idx] = {"error": msg}
                        recs[idx] = {"tool": name, "game": args.get("game"),
                                     "platform": args["platform"], "blocked": msg[:120]}
                        self._emit({"type": "tool-end", "name": name,
                                    "platform": args["platform"], "blocked": msg})
                    elif isinstance(got, CrawlError):
                        results[idx] = self._mark_fail(args["platform"], got)
                        recs[idx] = {"tool": name, "game": args["game"], "platform": args["platform"],
                                     "blocked": results[idx]["platform_error"][:120]}
                        self._emit({"type": "tool-end", "name": name, "platform": args["platform"],
                                    "blocked": results[idx]["platform_error"]})
                    else:
                        res = self._after_crawl(args, crawl_tools.commit(self.ctx, args, got))
                        for cid in self._close_thread_if_switched(args["game"], args["platform"]):
                            trace.append({"event": "thread_close", "cid": cid})
                        run_ids.append(res["summary"]["run_id"])
                        results[idx] = res
                        recs[idx] = {"tool": name, "game": args["game"], "platform": args["platform"],
                                     "new": res["summary"]["new"], "dup": res["summary"]["dup"],
                                     "leak": res["summary"].get("leak", 0),
                                     "stale": res["summary"].get("stale", 0),
                                     "run": res["summary"]["run_id"], "ask_total": res.get("ask_total")}
                        self._emit({"type": "tool-end", "name": name, "platform": args["platform"],
                                    "new": res["summary"]["new"], "dup": res["summary"]["dup"],
                                    "ask_total": res.get("ask_total")})
                        notes = []
                        if res["summary"].get("leak"):
                            notes.append("已拦截 %d 条属于兄弟作(%s)的样本, 未归档也未计入 ask_total" % (
                                res["summary"]["leak"], "/".join(res["summary"].get("leak_terms") or [])))
                        if res["summary"].get("stale"):
                            notes.append("已丢弃 %d 条早于 %s 的旧样本(本题带时效词), 别拿它们当'近期'" % (
                                res["summary"]["stale"], (self._since or "")[:10]))
                        if notes:
                            res["summary"]["note"] = "; ".join(notes)
                        round_ran = True
                        round_new += res["summary"].get("new", 0)
            for idx, tc in enumerate(tcs):                      # 工具返回按原 tool_call 顺序回填(DeepSeek 要求一一对应)
                if idx not in recs:
                    continue
                trace.append(recs[idx])
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": json.dumps(results[idx], ensure_ascii=False)})
            if round_ran:
                self._n_crawl += 1
                self.ctx["_sub_left"] = None     # 新的一轮 = 字幕额度重来(额度是按"轮"给的)
                if crawls and not round_new:
                    # 整轮真爬却 0 新增 = 空转(实测: 对比题里模型把一边重爬 4 轮, 每次 new:0/dup:10,
                    # 拿满预算才去爬另一边)。工具返回体里的 new/dup 模型会读漏, 这里用系统消息明说一次。
                    warn = ("(空转提示: 本轮真爬过, 但两平台新增 0 条 —— 再按同样的方向爬还是这些重复样本, "
                            "白耗爬轮。换成没试过的关键词方向, 或去爬题目里要求的另一边(对比题), 或直接收口作答。)")
                    messages.append({"role": "system", "content": warn})
                    trace.append({"event": "empty_round", "note": warn[:80]})
                nudge = self._ratio_nudge()       # 内部 1..99 软引导: 有真爬且偏差>容差才追一句(不改调度)
                if nudge:
                    messages.append({"role": "system", "content": nudge})
                    trace.append({"event": "ratio_nudge", "nga_ratio": self._nga_ratio, "note": nudge[:90]})
        # 用户点了「暂停」: **到此打住, 一个字都不落**。不落答案、不进状态卡、不落收口那条流水 ——
        # 落了答案这题在记忆里就算答过了, 下次"继续"会变成同一题答两遍; 而用户要的是"先搁着"。
        # 已爬到的样本本来就在库里(爬取即归档, 跟这一跑没关系), 所以"断点"是现成的, 不必另存快照。
        # 这条 paused 流水是给流水层认的: qid 挂着说明这题还没完, 侧栏据此把那道题列成可续的卡。
        if paused:
            try:
                flow.append(self.scope, {
                    "event": "paused", "qid": getattr(self, "_qid", None),
                    "question": (user or "")[:500], "mode": mode, "game_hint": game_hint or "",
                    "nga_ratio": self._nga_ratio,
                    "rounds": self._n_crawl,
                    "got": sum(int(st.get("new") or 0) for st in self._plats.values()),
                    "started_at": datetime.datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
                    "trace": trace,
                })
            except Exception:
                pass            # 落流水的锅不许砸在问答主链上(同 _emit_flow)
            return {"answer": None, "paused": True, "qid": getattr(self, "_qid", None),
                    "cancelled": False, "trace": trace,
                    "ctx": {"rounds": rounds, "ceiling": ceiling},
                    "prompt_tokens": self._tk_hit + self._tk_miss,
                    "completion_tokens": self._tk_out, "est_input_tokens": in_est}
        # 用户点了「停止」: 与"工具轮耗尽"走同一条收口路(同一套校验/落库/流水), 只换一句话交代。
        if stopped:
            messages.append({"role": "system", "content": (
                "(用户已点「停止」: 本题到此为止, 工具全部禁用。只用**已经取到的证据**作答 —— "
                "取到多少写多少, 没覆盖到的方面直说没取到; 禁止再提要不要继续爬、还剩多少预算。"
                "最后一行仍须是『结论: <一句话>』。)")})
        # 工具轮耗尽/护栏触发仍未收口: 强制一次无工具调用让结论落纸(不悬空占位, 保证记忆断言成立)
        messages.append({"role": "system",
                         "content": "已到达本轮上限, 工具全部禁用: 只能用已有证据收口。"
                                    "证据不足就写『结论: 证据不足以坐实…』。"
                                    "禁止再表达搜索/调用工具意图, 禁止输出任何工具调用标记。"})
        # 这一炮是**真出网**的: 超上限就从最早的证据压起 —— 不然"护栏触发"之后照样发一份超长的,
        # 上限等于没拦(§10.50)。压完再报一次, 免得下游以为没动过。
        cuts = _shrink_to_ceiling(messages, ceiling)
        if cuts:
            trace.append({"event": "ctx_shrink", "count": len(cuts), "ceiling": ceiling,
                          "note": "收口那一次发出去的超了上限, 压掉 %d 条较早的证据" % len(cuts)})
        resp = self._llm(messages, tools=False, stream=self._on_event is not None)
        rounds += 1
        in_est += _est_input_tokens(messages)
        answer = self._seal_answer(self._final_answer(messages, resp["choices"][0]["message"].get("content") or "(空)"), trace,
                                   crawled=bool(run_ids))
        ev = ",".join("run#%s" % rid for rid in run_ids) if run_ids else "cites"
        self._materialize(answer, user, ev)
        self._emit_flow(mode, user, game_hint, answer, ev, trace,
                        self._tk_hit, self._tk_miss, self._tk_out,
                        rounds, ceiling, est=in_est, t0=t0, cancelled=stopped)
        return {"answer": answer, "trace": trace, "ctx": {"rounds": rounds, "ceiling": ceiling},
                "prompt_tokens": self._tk_hit + self._tk_miss,
                "completion_tokens": self._tk_out,
                "est_input_tokens": in_est, "cancelled": stopped}

    def _close_thread_if_switched(self, game, platform):
        closed = []
        ag = self.card.anchor["game"]
        if ag and ag != game:
            for cid in list(self.card.active):
                claim = self.card.close(cid)
                if claim:
                    self.log.append(cid, claim, topic=ag)
                    closed.append(cid)
        self.card.anchor["game"], self.card.anchor["platform"] = game, platform
        return closed
