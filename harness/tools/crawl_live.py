# -*- coding: utf-8 -*-
"""模型面工具: crawl_nga / crawl_bili —— 真爬工具, 按平台拆成两个独立可装卸单元(替代原单 crawl_live)。
另有 crawl_official(官号探针, 舆论类专用): 爬官方账号最近动态 + 各自评论, 见 official_tool_def/_official。
CRAWLERS 注册表驱动: 每条目 = {tool, platform, description}。装/卸一条目 = 增删该平台爬虫的
模型可见能力面(未来独立包成 tool 让用户选装时, 只需同步 sources 的 CRAWLERS + 此处注册表)。
落档(record_crawl)是自动副作用, 不由模型决定存不存;
返回"汇总 + 证据"(工具侧瘦身: 每条只留 id + 短片段, 原文在 archive 可回捞)。

执行拆两段(引擎并发安全的关键, SEAM #2 provider 仍是 (query,game,platform,limit)):
  fetch(ctx, params)    —— 只调 provider 拿 items(纯网络/纯生成, 不碰 DB, 可放线程池并发);
                            真爬失败抛 CrawlError(空列表 = 真没搜到, 平台是好的, 别当错)。
  commit(ctx, params, items) —— 串行落库(record_crawl) + 组 summary/evidence; 必须主线程单写 conn。
  call(ctx, params)     —— 兼容旧单工具调用方(fetch 后立即 commit)。"""
import os
import sys

from ..sources import mock_nga
from .. import archive

# 注册表: 每个注册项 = 一个平台的真爬工具(模型工具面 = 这里 + engine 的 search_archive)
CRAWLERS = [
    {"tool": "crawl_nga", "platform": "NGA",
     "description": "实时爬取 NGA 上某游戏某话题的论坛帖+热评一轮并自动归档, 返回相关样本(每条带id)供作答。"
                    "需要 NGA 社区当下的讨论/风评时用这个。检索词 = 游戏名(game 参数) + 一个短关键词(query), "
                    "游戏名由引擎自动拼在前面、不用写进 query。**一轮内对 NGA 发多个短检索词**(每平台至少 3 个, "
                    "如 query=\"男六星\"/\"男干员\"/\"男六星强度\", 每词一次调用), 别把整句塞进一次 query——"
                    "长串在 NGA 是全词与, 叠得越多越必然为空; 同轮多次调用只算 1 个爬轮。"},
    {"tool": "crawl_bili", "platform": "bilibili",
     "description": "实时爬取 bilibili 上某游戏的视频(标题/简介/评论区)一轮并自动归档, 返回相关样本(每条带id)供作答。"
                    "需要 B站当下的风评声音时用这个。**不用给检索词**: 检索词由引擎按游戏本体名(一个词)"
                    "自动发, 所以一轮调一次就够(同一轮重复调只会重爬同一批); 同轮与 NGA 一起调仍只算 1 个爬轮。"
                    "高热度视频还会自动附上它的字幕全文 —— 引用时按该条的 id 引。"},
]
TOOL_BY_NAME = {c["tool"]: c for c in CRAWLERS}


def official_tool_def():
    """官号探针工具定义(不在 CRAWLERS 注册表里: 那条链是 (query,game,platform) 形状, 官号探针没 query)。
    这是「舆论类问题专用」的取证手段 —— 看官方账号在题目时段内动态的评论量涨落, 高峰=可能有节奏。"""
    return {"type": "function", "function": {
        "name": "crawl_official",
        "description": "爬某游戏**官方账号**在题目时间段内的动态 + 各自评论, 自动归档, 用于回答"
                       "「有什么节奏/争议」「官方什么态度、发了什么」「活动/版本日程公告」这类题。"
                       "**时段跟着题目走**: 问你年前那个版本的事就取那段时间的动态(引擎按题里的时间"
                       "锚点自动定窗口, 你不用在 query 里写年份)。探针给每条动态算评论量基准线(窗口内"
                       "中位数), 明显超基准的标为「高峰」= 疑似节奏发酵处, 高峰动态多采评论(25 条)、"
                       "平峰只 5 条 —— **平峰动态的评论区同样要看**(节奏常在平峰动态底下发酵)。"
                       "返回里 kind=official_post 是官方口径, kind=official_reply 是评论区态度, "
                       "两者都要引 [id=N]。**本 ask 只能调一次**; 常规社区讨论仍用 crawl_nga/crawl_bili。",
        "parameters": {"type": "object", "properties": {
            "game": {"type": "string",
                     "description": "游戏名(规范名; 支持原神/明日方舟/明日方舟：终末地/崩坏：星穹铁道/鸣潮等官号)"},
        }, "required": ["game"]}}}


def crawl_tool_defs():
    """注册表 -> 模型可见 tool def 列表(两独立工具, 各固定一个平台, 不暴露 platform 枚举参数)。
    B站不收 query: 检索词由引擎按游戏本体名发(见 CRAWLERS 里的说明), 见下。"""
    defs = []
    for c in CRAWLERS:
        props = {"game": {"type": "string",
                          "description": "游戏名(规范名; 昵称如三蹦子先还原成崩坏三再填, 别让昵称进检索词)"}}
        req = ["game"]
        if c["platform"] != "bilibili":
            props["query"] = {"type": "string",
                              "description": "话题短关键词, 必填: **只放一个 3-6 字的短词**(具体活动/版本/角色名/争议点等), "
                                             "**不要写游戏名**(游戏名走 game 参数, 引擎会自动拼在它前面)。"
                                             "整句要拆成多个短关键词, 每个一次调用、同轮连发, 别拼成长串。"}
            req = ["query", "game"]
        defs.append({"type": "function", "function": {
            "name": c["tool"], "description": c["description"],
            "parameters": {"type": "object", "properties": props, "required": req}}})
    return defs


SUB_PER_ROUND = 2       # 每轮最多取几条字幕(用户定的额度); 取到才扣, 没取到不扣
SUB_MAX_MINUTES = 30    # 时长门槛: 首 P 超这个分钟的就不取 —— 长片的字幕不是"这段在说什么", 是整部片子


def fetch(ctx, params):
    """阶段1(线程安全): 只调 provider 拿 items, 不碰 DB。真爬失败抛 CrawlError; 空列表=真没搜到。
    since/until 由引擎按问句时间锚点注入(非模型参数), 透传给 provider 做时间窗取数。
    keywords(B站): 引擎按游戏本体名拼好的检索词表, 走它而不是模型给的 query。
    B站拿到 items 后顺手补字幕(也是纯网络取数) —— 见 _attach_subtitles 里为什么必须在这一步做完。"""
    provider = ctx.get("provider") or mock_nga.crawl
    items = provider(**{k: params.get(k)
                        for k in ("query", "game", "platform", "limit", "since", "until", "windows",
                                  "keywords")
                        if k in params})
    if params.get("platform") == "bilibili":
        _attach_subtitles(ctx, items, params.get("exclude_before"))
    return items


def commit(ctx, params, items):
    """阶段2(主线程串行): record_crawl 落库 + 组 summary/evidence。引擎并发跑完 fetch 后逐个调, 保 sqlite conn 单写。"""
    conn = ctx["conn"]
    scope = ctx.get("scope") or "prod"
    res = archive.record_crawl(conn, params, items, scope=scope)
    ev = [_ev(r) for r in archive.rows_of_run(conn, res["run_id"])]
    return {
        # shown = 本条返回里给模型看的证据条数。09-12 起不再按条数截断, 恒等于 new(本轮新样本全量进上下文);
        # 字段与引擎侧那条"只展示前 N 条"的提示都保留 —— 万一以后重新设闸, 模型不至于把"我没看到"读成"社区没有"。
        "summary": {"run_id": res["run_id"], "new": res["new"], "dup": res["dup"],
                    "leak": res.get("leak", 0), "stale": res.get("stale", 0),
                    "shown": len(ev),
                    "game": params.get("game"), "platform": params.get("platform"),
                    "query": params.get("query"),
                    "total_obs": archive.count(conn, scope=scope, game=params.get("game"))},
        "evidence": ev,
    }


def official(ctx, params):
    """官号探针取数(模型工具面 crawl_official 的实现)。与 fetch 同规矩: 只取数不碰 DB, 失败抛 CrawlError。
    provider 可注入(ctx["official_provider"], 测试用假数据); 未注入时走真爬 provider。
    真爬模式下 mock provider 不存在, 断网/测试环境会返回 [] 并在 stderr 留一行 —— 不会真发请求。"""
    game = (params.get("game") or "").strip()
    if not game:
        return []
    fn = ctx.get("official_provider")
    if fn is None:
        if not os.environ.get("HARNESS_LIVE"):
            print("[live] 非 live 模式且没注入 official_provider: crawl_official 返回空", file=sys.stderr, flush=True)
            return []
        from ..sources import live
        fn = live.crawl_official
    # 时段由引擎按题目的时间锚点注入(params.since/until); 缺省时由服务端取近一个版本周期
    return fn(game, since=params.get("since"), until=params.get("until"))


def call(ctx, params):
    """向后兼容旧单工具调用方: fetch + commit 同步完成(= 改版前 crawl_live.call)。"""
    items = fetch(ctx, params)
    return commit(ctx, params, items)


def _attach_subtitles(ctx, items, before=None):
    """给这一轮 B站 的高热度视频补字幕全文(写进 items[i]["sub_text"], 随落库一起进 observation)。

    为什么必须赶在落库**之前**做完: observation 是只增不改的账(库里那条静态检查盯着 UPDATE)——
    落库之后再回填就得改写已入库的证据行, 那是这套数据层不许干的事。字幕本来就是纯网络取数,
    放在 fetch 这半步正合适(这半步的规矩就是"只取数、不碰 DB")。

    挑而不全取: 一条字幕动辄几千字, 全取会把这轮的上下文挤爆, 抢的本来就是"高热度视频"这条线
    (所以按 view_count 从高到低, 冷门视频的字幕对回答没多少增量)。

    before(入档时间窗下界, 同 params["exclude_before"]): 早于它的高播放视频**先摘掉** —— 它们
    落了库也会被入档那道闸门(stale)丢掉, 进不了上下文、也不会出现在引用卡片上, 给它取字幕纯属
    白花额度白打平台(2026-09-17 实测撞上: 2 条额度里 1 条花在一条 7 月的老片上, 那条随后就被丢了)。
    判定沿用那道闸门自己的尺子(archive._stale), 免得两处口径各说各话。历史题不给下界 = 不摘。

    为什么记着取过的(_sub_done): 同一批热门视频**每一问**都会排在搜索结果前面, 不记着就会反复
    去取 —— 而且取回来还会被去重挡掉(第二条起统统算 dup, 落不了库), 白烧额度、白打平台。
    记住之后, 额度才留给这一轮真正的新视频(它跨 ask 保留, 见 Engine.__init__)。

    只对 HARNESS_LIVE 生效: 非 live(测试/离线)时这是纯网络调用, 一条都不该发。
    失败一律吞掉(只留日志): 字幕是加料不是主料, 取不到答案照出。
    """
    left = ctx.get("_sub_left")
    left = SUB_PER_ROUND if left is None else left      # None = 没扣过 = 满额
    if left <= 0 or not os.environ.get("HARNESS_LIVE"):
        return
    done = ctx.setdefault("_sub_done", set())
    since = (before or "")[:19]         # 与 record_crawl 里那道闸门取同一截
    cands = [it for it in items
             if (it.get("kind") or "") == "post" and str(it.get("raw_id") or "").startswith("BV")
             and it["raw_id"] not in done]
    vids = [it for it in cands if not archive._stale(it, since)]
    if len(vids) < len(cands):
        print("[字幕] 跳过 %d 个早于时间窗的高播放视频(取了也会在入档时被丢掉)"
              % (len(cands) - len(vids)), file=sys.stderr, flush=True)
    if not vids:
        return
    vids.sort(key=lambda it: -(it.get("view_count") or 0))
    from ..sources import live          # 函数内 import: 非 live 环境不必碰这一层
    got = live.bili_subtitles([it["raw_id"] for it in vids], limit=left, max_minutes=SUB_MAX_MINUTES)
    by_bv = {g.get("bvid"): (g.get("text") or "").strip() for g in got if g.get("bvid")}
    for it in vids:
        done.add(it["raw_id"])          # 取没取到都记上: 这条没字幕, 下一轮别再问一遍
    n = 0
    for it in items:
        t = by_bv.get(it.get("raw_id"))
        if t:
            it["sub_text"] = t          # 字幕有自己的一格, 不走 text 那个 2000 字上限
            n += 1
    ctx["_sub_left"] = left - n


def _ev(r):
    text = (r["text"] or "").replace("\n", " ")[:600]
    title = (r["title"] or "")[:120]
    e = {"id": r["id"], "platform": r["platform"], "kind": r["kind"],
         "title": title, "text": text, "author": r["author"],
         "published_at": r["published_at"], "senti": {0: "负", 1: "中", 2: "正"}.get(r["senti_arg"]),
         "sarcasm": r["sarcasm"], "tags": r["topic_tags"],
         # 量化热度: 采集当时快照, 平台/条目类型没有的项为 None(=该条无此数据, 不是 0)
         "view": r["view_count"], "like": r["like_count"], "reply": r["reply_count"],
         "danmaku": r["danmaku_count"], "video_tags": r["video_tags"]}
    if r["sub_text"]:                           # 空的时候不出这一格: 绝大多数行没有字幕, 别白占上下文
        e["sub"] = r["sub_text"]
    return e
