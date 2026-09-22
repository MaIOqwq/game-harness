# -*- coding: utf-8 -*-
"""翻译层: 把「一条 flow 流水 + 该 scope 的归档样本」翻成前端认的题目形状。

算法照搬一份离线抽取脚本(那份是按固定 tag 从认证库抽的),
这里改成按传入的 scope 实时算。前端要的字段一个不少、一个不多 —— 前端不用改渲染逻辑。
"""
import html
import re

from . import flow, pdfout

_CITE_RE = re.compile(r"\[\s*id\s*=\s*([^\]]+?)\s*\]")


def _measure_mode():
    """度量口径(model / keyword)。取不到就返回 None —— 前端与 PDF 都当成"不知道"处理, 不瞎标。"""
    try:
        from .sources import live
        return live.measure_mode()
    except Exception:
        return None
_RANGE_RE = re.compile(r"(\d+)\s*[-–~]\s*(\d+)")
_SENTI_NAME = {0: "负", 1: "中", 2: "正"}


def _title_of(r):
    """一行证据显示什么名字: 标题 → 正文首句 → 「(无正文)」。**与网页 labelOf() 同一套**。

    没有标题的多是楼内回复/视频评论(本机 6618 行里 4131 行如此)。这层不给兜底的话, 同一个对话
    在网页上显示"这条评论的首句"、在导出的 PDF 上显示"(无标题)", 用户会以为导出的那份坏了。

    标题顺手把 HTML 实体还原: NGA 的新闻标题存进来时带 &#39; 这类转义(实测 10 行), 不还原就
    「&#39;启程！鸣潮巡游巴士&#39;」原样印到纸上 —— 网页那边也是同一个字符串, 一起修。"""
    t = html.unescape(r["title"] or "").strip()
    if t:
        return t
    s = re.sub(r"\s+", " ", r["text"] or "").strip()
    return s[:26] + ("…" if len(s) > 26 else "") if s else "(无正文)"


def _clean_answer(answer):
    """答案开头漏出的英文思考过程, 在这层再剃一次(与 engine._seal_answer 同一条规则)。

    引擎出关时已经剃过(engine._strip_leading_scratch), 但那是 09-16 才上的 —— 之前落的流水
    里那些答案照样带着「I've used 5 crawl rounds…」的头。展示层读的是历史流水, 不跟着剃的话,
    用户翻回旧题会看见引擎早就承诺不出的那句英文(实测 36 条)。

    规则本身取自引擎, 不在这里另写一套: 两处各认各的, 早晚会漂。
    导入放到函数里 —— engine 那棵树不小, 而这层只在真有答案要展示时才用到它。"""
    try:
        from .engine import _strip_leading_scratch
    except Exception:
        return answer
    return _strip_leading_scratch(answer)[0]


def parse_cited(answer):
    """从答案正文抠出被引 id。支持 [id=N] / [id=N, M] / [id=N-M] 三种写法(q4 实测有区间写法)。"""
    cited = set()
    for m in _CITE_RE.finditer(answer or ""):
        for tok in re.split(r"[\s,，、;；/]+", m.group(1).strip()):
            if not tok:
                continue
            if tok.isdigit():
                cited.add(int(tok))
                continue
            rng = _RANGE_RE.fullmatch(tok)
            if rng:
                a, b = int(rng.group(1)), int(rng.group(2))
                if 0 < a <= b <= a + 200:
                    cited.update(range(a, b + 1))
    return cited


# 量化热度列(view/like/reply/danmaku/video_tags)必须一起取出来: 它们是画热度类图表的唯一原料,
# 解析层辛苦留下的字段不能在这层被丢掉 —— 前端拿不到就只能画"条数", 画不了"哪条真火"。
# 老行这些列是 NULL(采集早于列上线), 前端据此自己决定画不画。
# kind(post/reply/official_post/official_reply)与 crawled_at 是画事件时间轴的分道依据:
# 靠 kind 才能把"官方发的"和"玩家说的"分成不同泳道。
_SEL = ("SELECT id, platform, kind, title, author, url, published_at, crawled_at, "
        "senti_arg, sarcasm, topic_tags, text, sub_text, "
        "view_count, like_count, reply_count, danmaku_count, video_tags "
        "FROM observation ")


def rows_of_runs(conn, rec):
    """本题这一问**自己爬到的**样本行(rec.ev = 'run#12,run#15'; 没真爬过则是 'cites')。"""
    runs = [int(x) for x in re.findall(r"run#(\d+)", rec.get("ev") or "")]
    if not runs:
        return []
    ph = ",".join("?" * len(runs))
    return [dict(r) for r in conn.execute(
        _SEL + "WHERE crawl_id IN (%s) ORDER BY id" % ph, runs).fetchall()]


def rows_of_ids(conn, ids):
    """按 id 捞行 —— **刻意不按 scope 过滤**: 引用要走本机证据池。

    模型引的可能是**早先会话**爬到的料(用户要的"之前查过的还能回去查到")。若这里按 scope 过滤,
    那条引用会被判成"核不回存档", 卡片上挂个红叉 —— 明明是找得回来的真出处, 却报成幻觉。"""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    return [dict(r) for r in conn.execute(_SEL + "WHERE id IN (%s)" % ph, ids).fetchall()]


def build_ask(conn, rec, ask_id=None, cid=None):
    """rec = flow 里的一条记录(自带 scope/ts)。返回前端 renderAsk 认的形状。
    cid = 本轮物化结论的卡 id(如 "M3"), 只在当轮活跃时有值 —— 纠错要用它;
    历史题从流水里重建, 拿不到(卡已翻篇), 前端据此如实提示。

    证据面 = **本题新爬到的** ∪ **本题引到的**(引用从全池解析)。两个来源分清楚, 卡片上的数字才对得上:
    本题新爬那批是"这一问搬回库的东西"; 引用那批可能有一大半是早先会话就在库里的老料, 不该冒充新爬量。"""
    own = rows_of_runs(conn, rec)
    answer = _clean_answer(rec.get("answer"))
    cited_ids = parse_cited(answer)
    pool_rows = rows_of_ids(conn, cited_ids)
    merged = {r["id"]: r for r in own}
    for r in pool_rows:
        merged.setdefault(r["id"], r)
    rows = [merged[k] for k in sorted(merged)]
    idx = {r["id"]: r for r in rows}
    reused = len([r for r in pool_rows if r["id"] not in {o["id"] for o in own}])

    by_plat, senti, trend = {}, {"负": 0, "中": 0, "正": 0, "无": 0}, {}
    for r in rows:
        by_plat[r["platform"]] = by_plat.get(r["platform"], 0) + 1
        senti[_SENTI_NAME.get(r["senti_arg"], "无")] += 1
        day = (r["published_at"] or "")[:10]
        if day:
            trend[day] = trend.get(day, 0) + 1

    cites = []
    for i in sorted(cited_ids):
        r = idx.get(i)
        if r is None:
            continue
        cites.append({
            "id": r["id"], "platform": r["platform"], "kind": r["kind"], "title": _title_of(r),
            "author": r["author"], "url": r["url"], "published_at": r["published_at"],
            "senti": r["senti_arg"], "sarcasm": r["sarcasm"], "tags": r["topic_tags"],
            "view_count": r["view_count"], "like_count": r["like_count"],
            "reply_count": r["reply_count"], "danmaku_count": r["danmaku_count"],
            "video_tags": r["video_tags"],
            "text": (r["text"] or "")[:600],
            # 字幕全文(只取过的那些行有): 引用卡片上单独一块, 不跟 600 字的简介抢一个格
            "sub": r["sub_text"] or "",
        })
    # 真核一次: 答案标的每个 id 是否真在本机证据池里(前端徽标用)
    missing = sorted(i for i in cited_ids if i not in idx)
    uncited = [{
        "id": r["id"], "platform": r["platform"], "kind": r["kind"], "title": _title_of(r),
        "author": r["author"], "published_at": r["published_at"],
        "senti": r["senti_arg"], "like_count": r["like_count"], "view_count": r["view_count"],
        # reply_count 也得给 —— 时间轴那个点的「回复 N」是把那天的行加起来的, 少了这个字段
        # 就只加得到被引用的那几条, 数会偏小(而且看不出来)。
        "reply_count": r["reply_count"],
        "video_tags": r["video_tags"],
        "text": (r["text"] or "")[:160],
    } for r in own if r["id"] not in cited_ids]

    trace = rec.get("trace") or []
    tool_ev = [e for e in trace if e.get("tool")]
    blocked = [e for e in tool_ev if e.get("blocked")]
    need_sentiment = any(r["senti_arg"] is not None for r in rows)
    # 时间轴右端那条"本次采集"竖线: 取这批证据里最晚的一次落库时刻(含早先会话捞回来的老料)。
    crawled = sorted(r["crawled_at"] for r in rows if r.get("crawled_at"))
    collected_at = crawled[-1] if crawled else None

    # 这一题的 token 消耗(三档: 缓存命中/未命中/输出)。命中那两档必须分开带出去: 只给一个
    # prompt_tokens 总数, 页面上就说不清这份输入里有多少是复用的、有多少是新喂的。
    usage = rec.get("usage") or {}

    aid = ask_id or "a%s" % re.sub(r"[^\w]", "", str(rec.get("ts") or ""))[:14]
    return {
        "id": aid,
        "cid": cid,
        "mode": rec.get("mode"), "question": rec.get("question"),
        "game": rec.get("game_hint"),
        "answer": answer, "concl": rec.get("concl"),
        "cites": cites, "uncited": uncited,
        "cite_check": {"cited": len(cites), "missing": missing},
        "stats": {
            "total": len(rows), "by_platform": by_plat,
            "new": len(own),            # 本题新爬归档的条数(卡上"新归档"那个数)
            "reused": reused,           # 引用的证据里, 早先会话就已在库里的条数
            "runs": len(tool_ev) - len(blocked),
            "rounds": rec.get("rounds"), "blocked": len(blocked),
            "usage": usage, "ceiling": rec.get("ceiling"),
        },
        "need_sentiment": need_sentiment,
        # 导出 PDF 时要把「情感是词表启发式」如实标出来, 所以度量口径跟着这一题一起带走
        "measure": _measure_mode(),
        "senti": senti if need_sentiment else None,
        "trend": [{"date": k, "n": v} for k, v in sorted(trend.items())] if need_sentiment else None,
        "trace": trace,
        # 点过「停止」提前收口的那一题(从流水里读, 所以刷新页面/历史题照样认得出来):
        # 前端据此标"只用了当时已取到的证据", 免得这份答案被当成跑完全程的答卷。
        "cancelled": bool(rec.get("cancelled")),
        "collected_at": collected_at,
        # PDF 是按卡 id 落在 out/pdf/ 下的: 文件在就给出下载地址 —— 刷新页面后这条链接也还在
        # (当场那次是 webapp 渲染完再补上同一个值)。
        "pdf": ("/api/pdf?id=%s" % aid) if pdfout.exists(aid) else None,
        # 会话时间轴要的两个: 开始时刻 + 实际用时长(老记录没有这两列 -> 前端只标结束时刻, 不瞎推)
        "started_at": rec.get("started_at"), "elapsed": rec.get("elapsed"),
        "ts": rec.get("ts"),
    }


def build_pending(rec, paused_rec=None):
    """问了、却没留下答案的那一题。只如实说"问到哪了" —— 不编答案, 不编数字。

    字段刻意少: 前端见到 unfinished 就走那张简卡, 其余统计/引用一概没有可给的东西。

    paused_rec = 这道题那条 event=paused 的流水(有就是**用户自己按的暂停**, 不是进程崩了)。
    两种情形给的东西不一样, 因为能做的事不一样:
      · 用户按的暂停 —— 已爬到的样本还在库里, 给出「继续」按钮 + 当时爬到哪了;
      · 进程崩了 —— 没有那批号的底账(不知道爬了几轮/几条), 只能照实说中断了、重问一遍。
    把崩溃那条也画上「继续」是骗人的: 没有 paused 记录就不知道上次停在哪, "接着爬"只是重问。"""
    out = {
        "id": "p%s" % re.sub(r"[^\w]", "", str(rec.get("qid") or rec.get("ts") or ""))[:14],
        "unfinished": True,
        "question": rec.get("question"), "mode": rec.get("mode"),
        "started_at": rec.get("started_at"), "ts": rec.get("ts"),
    }
    if paused_rec:
        out["paused"] = True
        out["qid"] = paused_rec.get("qid")
        out["rounds"] = int(paused_rec.get("rounds") or 0)    # 暂停时已经真爬过的轮数
        out["got"] = int(paused_rec.get("got") or 0)          # 那道题当时新归档了几条样本
        out["paused_at"] = paused_rec.get("ts")
    return out


def history(conn, scope, limit=20):
    """本会话问过的题(按写入序取最后 limit 条) + 问了没跑完的那几题。

    没跑完的也要给: 不然用户刷新回来只看见对话短了一截, 那一题像是从没问过。
    两边混在一起后按时刻重排(流水按写入序读, 而没跑完的那条可能是插在中间的)。"""
    out = [build_ask(conn, r) for r in flow.read(scope)[-limit:]]
    pz = flow.paused(scope)
    out += [build_pending(r, pz.get(r.get("qid"))) for r in flow.pending(scope)]
    out.sort(key=lambda a: str(a.get("ts") or ""))
    return out
