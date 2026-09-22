# -*- coding: utf-8 -*-
"""真爬虫 provider(替换 mock_nga): 平台路由到常驻 HTTP 爬虫服务(本机直连, 或经 ssh 隧道打到远端)。
NGA=8770 / bili=8771。两家 crawler 已拆成独立函数 crawl_nga/crawl_bili + CRAWLERS 注册表
(各自不依赖对方, 以后要独立包成 tool 让用户选装时, 一个注册项 = 一个可装卸 crawler 单元);
crawl() 只做平台分发, 保持 SEAM #2 provider 接口不变。服务响应转成 observation 形状的 items。
实测要点:
  - NGA 全站 key 搜, 空格分词的多词命中最好(search|孙膑 中路 -> 直接命中"为什么不让孙膑打中路"原帖);
    长句无空格 key 大概率 0, 由模型收敛成短关键词(引擎会多轮自适应)。
  - NGA board|<游戏> 自动动态找版, 找不出版面会内部回退全站搜热帖, 不会空手。
  - bili 搜索词 = 游戏+关键词 全去空格直拼(空格会漏匹配; query 里重复的游戏名先剥掉)。
  - 已删/过期帖(title=帖子发布或回复时间超过限制 / post_time=1970)过滤掉。
  - 官号探针 crawl_official(舆情用)走 bili 服务端 POST /official: 该时间段内的官号动态按评论数中位数
    算基准线, 超基准 3 倍且 >=100 条判"高峰"(疑似节奏) -> 高峰深采 25 条评论、平峰只 5 条;
    条目 kind=official_post/official_reply, 归档里与玩家帖(reply/post)区分开。
  - 错误语义: 服务/传输失败 RAISE CrawlError(bili 超时/429 打 throttle 标记供引擎退避锁),
    空列表 = 真没搜到(平台是好的), 引擎靠这个区分两种失败。"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from .. import supervisor
from . import CrawlError

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_HERE, "..", "data")

# 这是"装在用户电脑上"的分发版: 爬虫就带在包里的 crawlers/ 下, 本机 8770/8771。
# 所以本地模式是**默认**; 想换成别的部署形态(比如走远端隧道)再设 HARNESS_LOCAL=0 + NGA_HTTP/BILI_HTTP。
_LOCAL = os.environ.get("HARNESS_LOCAL", "1").strip().lower() not in ("", "0", "false", "no", "off")
_LOCAL_ROOT = os.environ.get("HARNESS_LOCAL_ROOT") or os.path.normpath(
    os.path.join(_HERE, "..", "..", "crawlers"))

_BASE = {
    "nga": os.environ.get("NGA_HTTP", "http://127.0.0.1:8770" if _LOCAL else "http://127.0.0.1:18770"),
    "bilibili": os.environ.get("BILI_HTTP", "http://127.0.0.1:8771" if _LOCAL else "http://127.0.0.1:18771"),
}
_TOKEN_FILE = {
    "nga": os.environ.get("NGA_TOKEN_FILE", os.path.join(_DATA, ".nga_token")),
    "bilibili": os.environ.get("BILI_TOKEN_FILE", os.path.join(_DATA, ".bili_token")),
}
# 服务端起动时自己在服务目录生成 .tool_token; 本地模式下直接读它, 免手工拷成 .nga_token/.bili_token。
_LOCAL_TOKEN_FILE = {
    "nga": os.path.join(_LOCAL_ROOT, "nga", ".tool_token"),
    "bilibili": os.path.join(_LOCAL_ROOT, "bili", ".tool_token"),
}


def is_local():
    """是不是"装在用户电脑上"那种部署(爬虫在包内、按需拉起)。"""
    return _LOCAL


def _token_candidates(plat):
    """找 token 的顺序: 本地模式下先看服务目录的 .tool_token, 再看 harness/data 的老位置。"""
    return [_LOCAL_TOKEN_FILE[plat], _TOKEN_FILE[plat]] if _LOCAL else [_TOKEN_FILE[plat]]


def _read_token(plat):
    for p in _token_candidates(plat):
        try:
            with open(p, encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                return tok
        except Exception:
            continue
    return None


def _err(msg):
    print("[live] %s" % msg, file=sys.stderr, flush=True)


def _post(plat, query, since=None, until=None, windows=None, timeout=300):
    """POST /crawl, 单飞 busy(429)短重试; 超时/其他错误立即返回 {'error': ...}。
    不自动重试同 query 的爬超时: bili 限流下重复爬同词会加剧风控(实测重试把单次卡到15min)。
    since/until(YYYY-MM-DD) 为可选时间窗: NGA 侧翻页到该时段再采(见 nga_search_demo 时间窗导航)。
    windows = 分段窗 [[since,until],...](对比题"今年 vs 去年"): 服务端每段各取一窗再合并,
    避免合成单窗后只取到最新一截把早那段丢掉。NGA(按末回翻页)与 bili(按发布时间过滤)均已支持。"""
    payload = {"query": query}
    if since:
        payload["since"] = since
    if until:
        payload["until"] = until
    if windows:
        payload["windows"] = windows
    tmo = 600 if windows else timeout        # 分段窗(对比题)要翻 2 段, 给足时间
    return _post_path(plat, "/crawl", payload, timeout=tmo)


def _post_path(plat, path, payload, timeout=300):
    """POST 到某平台服务端的任意端点(带鉴权)。429 单飞忙短重试, 超时/连不上不重试直接回 {'error':...}。"""
    tok = _read_token(plat)
    if not tok:
        return {"error": "token 读不到(找过: %s)" % ", ".join(_token_candidates(plat))}
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(3):
        req = urllib.request.Request(_BASE[plat] + path, data=body, method="POST",
            headers={"X-Token": tok, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(12)
                continue
            raw = e.read().decode("utf-8", "replace")[:400]
            # 服务端的错误体也是 JSON, 解析回来才留得住 risk 这类字段(见 bili 服务端 503 那两个);
            # 解析不出来(如网关回的 HTML)才退回纯文本。
            try:
                body = json.loads(raw)
            except Exception:
                body = None
            if not isinstance(body, dict):
                body = {"error": raw}
            body.setdefault("error", "HTTP %s" % e.code)
            body.setdefault("http", e.code)
            return body
        except Exception as e:
            return {"error": repr(e)}
    return {"error": "busy 重试 3 次仍 429"}


_DROPPED = ("ConnectionResetError", "ConnectionRefusedError", "ConnectionAbortedError",
            "RemoteDisconnected", "WinError 10054", "WinError 10061")


def _dropped(res):
    """这条错误是不是「连接被掐/被拒」——传输层断了, 不是超时、不是限流、不是鉴权失败。
    本地服务重启一下就能重连的那种; 超时(接了不回)和 429 都不算, 那两类重发只会更糟。"""
    return any(k in str(res.get("error") or "") for k in _DROPPED)


def _get_path(plat, path, timeout=10):
    """GET 某平台服务端端点(带鉴权)。失败回 {'error': ...}, 不抛。"""
    tok = _read_token(plat)
    if not tok:
        return {"error": "token 读不到(找过: %s)" % ", ".join(_token_candidates(plat))}
    req = urllib.request.Request(_BASE[plat] + path, method="GET", headers={"X-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])}
    except Exception as e:
        return {"error": repr(e)}


def health(plat):
    """读服务端 /health = 爬虫健康检查入口: 服务活没活 + 登录态在不在。
    返回服务端原样 {'ok','busy','queries_done','login',...}; 连不上则 {'ok':False,'login':'unknown','error':...}。
    给前端"没登录就让用户扫码"那条链提供后端那一半。"""
    res = _get_path(plat, "/health")
    if res.get("error"):
        return {"ok": False, "login": "unknown", "error": res["error"]}
    return res


def measure_mode():
    """度量走的是模型还是关键词降级 —— 前端要如实显示, 不能假装有情感分析。
    只看 nlp-tool 的 token 在不在(不真连, 免得每次刷状态都打一次服务)。"""
    tok = os.environ.get("NLP_TOOL_TOKEN")
    if not tok:
        path = os.environ.get("NLP_TOOL_TOKEN_FILE") or os.path.join(_DATA, ".nlp_token")
        try:
            with open(path, encoding="utf-8") as f:
                tok = f.read().strip()
        except Exception:
            tok = None
    return "model" if tok else "keyword"


def _raise_if_err(plat, res, throttle_hint=False):
    """transport/服务层错误 -> RAISE CrawlError(空返回留给"真没搜到", 引擎靠它区分)。
    throttle_hint 只给 bili: 超时/429/**风控拦截**(服务端回 risk=True, 或错误文本带"风控")
    都算风控信号, 触发引擎退避锁。"""
    err = res.get("error")
    if not err:
        return
    msg = str(err)
    low = msg.lower()
    # "tim" 这种三段字母的裸包含会把 estimate / optimal 之类也判成超时(实测教训: 判据要写全词)。
    throttle = throttle_hint and (bool(res.get("risk")) or "429" in msg or "风控" in msg
                                  or "timeout" in low or "timed out" in low)
    raise CrawlError("%s crawl fail: %s" % (plat, msg), platform=plat, throttle=throttle)


def _nga_key(game, query):
    """组 NGA 检索串(改版内路由 2026-09-08): 有话题词 -> searchin|游戏|词(先定主板 fid 再版内搜,
    泛用词不再全站搜, 否则跨游戏噪音/漂移); 只有游戏名 -> board|游戏 拉主板最新帖。
    主板定位失败由服务端内部回退全站搜热帖; 版内没这词 = 真没搜到(空, 引擎如实)。"""
    q = (query or "").strip()
    g = (game or "").strip()
    if not q:
        return "board|%s" % g if g else ""
    if g and g in q:
        q = q.replace(g, " ").strip()
    toks = [t for t in re.split(r"[,，。？！?、;；:：\s]+", q) if t and t != g]
    seen, uniq = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    if not uniq:
        return "board|%s" % g if g else ""
    if not g:
        return "search|" + " ".join(uniq)          # 没游戏名拿不到主板, 只能全站搜兜底
    return "searchin|%s|%s" % (g, " ".join(uniq))


def _num(v):
    """量化字段归一: 数字原样, 数字串转 int, 其余(空/非数) -> None。图表/论据用, 宁可空也不编。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = str(v).strip()
    return int(s) if s.lstrip("-").isdigit() else None


def _nga_items(res, query_meta):
    """NGA 响应 -> items: 主楼 kind=post, 热评 kind=reply。过滤已删/过期帖。
    量化字段(回复数/热评点赞)一并带回: 采集当时快照, 供作答当论据、供下游画图表。"""
    items = []
    posts = res.get("posts") or []
    for p in posts:
        title = (p.get("title") or "").strip()
        ptime = str(p.get("post_time") or "")
        if title.startswith("帖子发布或回复时间超过限制") or ptime.startswith("1970"):
            continue
        tid = p.get("tid")
        url = p.get("url") or ""
        text = (p.get("content") or "").strip()
        items.append({"kind": "post", "title": title[:500], "text": text[:2000],
                      "author": p.get("author") or "", "raw_id": str(tid),
                      "url": url, "published_at": ptime,
                      "reply_count": _num(p.get("reply_count")),
                      "dedup_key": "nga:%s" % tid})
        for i, h in enumerate(p.get("hot_replies") or []):
            hc = (h.get("content") or "").strip()
            if not hc:
                continue
            items.append({"kind": "reply", "title": "", "text": hc[:2000],
                          "author": h.get("author") or "",
                          "raw_id": "%s_h%d" % (tid, i), "url": url,
                          "published_at": str(h.get("time") or ptime),
                          "like_count": _num(h.get("like")),
                          "dedup_key": "nga:%s:h%d" % (tid, i)})
    return items


def _bili_items(res):
    """bili 响应 -> items: 视频 kind=post(标题+简介), 评论 kind=reply。
    量化字段(播放/弹幕/回复/点赞/自带标签)一并带回: 采集当时快照, 供作答当论据、供下游画图表。"""
    items = []
    for v in res.get("videos") or []:
        bvid = v.get("bvid")
        if not bvid:
            continue
        url = v.get("url") or ("https://www.bilibili.com/video/%s" % bvid)
        desc = (v.get("desc") or "").strip()
        tags = v.get("tags") or []
        if isinstance(tags, str):
            tags = [t for t in re.split(r"[,，\s]+", tags) if t]
        items.append({"kind": "post", "title": (v.get("title") or "")[:500],
                      "text": (desc or v.get("title") or "")[:2000],
                      "author": v.get("author") or "", "raw_id": bvid,
                      "url": url, "published_at": str(v.get("publish_time") or ""),
                      "view_count": _num(v.get("view_count")),
                      "danmaku_count": _num(v.get("danmaku_count")),
                      "reply_count": _num(v.get("reply_count")),
                      "like_count": _num(v.get("like_count")),
                      "video_tags": ",".join(str(t) for t in tags)[:300] or None,
                      "dedup_key": "bili:%s" % bvid})
        for cm in v.get("comments") or []:
            cc = (cm.get("content") or "").strip()
            if not cc:
                continue
            rpid = str(cm.get("rpid") or "")
            items.append({"kind": "reply", "title": "", "text": cc[:2000],
                          "author": cm.get("author") or "",
                          "raw_id": ("%s_c%s" % (bvid, rpid)) if rpid else bvid,
                          "url": url,
                          "published_at": str(cm.get("time") or ""),
                          "like_count": _num(cm.get("like")),
                          "dedup_key": "bili:%s_c%s" % (bvid, rpid)})
    return items


KW_GAP = 12          # 一轮内多个检索词之间的间隔(秒): 挨着连打同一平台就是风控的由来


def crawl_bili(query, game, limit=8, since=None, until=None, windows=None, keywords=None):
    """bilibili 真爬(独立 crawler 单元, 不依赖 NGA; 以后可独立包成 tool 让用户选装)。

    keywords(引擎注入的检索词表, 走这条): 逐词各搜一次、按条目去重合并 —— 这是「B站检索词 =
    游戏本体名, 不拼话题词」那条口径的落点(2026-09-17 定); 词与词之间留 KW_GAP,
    挨着连打同一平台就是风控的由来。
    不给 keywords 时退回老行为: 游戏名+query 全去空格直拼(query 里重复的游戏名先剥掉)。
    since/until/windows 原样透传给服务端; bili 与 NGA 一样支持分段窗(每段按发布时间各搜一页再合并),
    对比题「今年 vs 去年」两侧都能取到早那段。"""
    if keywords:
        kws = [re.sub(r"\s+", "", str(k)) for k in keywords if str(k).strip()]
    else:
        g, q = (game or "").strip(), (query or "").strip()
        if g and g in q:                      # query 别重复游戏名: "王者荣耀王者荣耀孙膑" 双拼
            q = q.replace(g, "")
        kw = re.sub(r"\s+", "", "%s%s" % (g, q)) if (g or q) else ""
        kws = [kw] if kw else []
    if not kws:
        return []
    supervisor.ensure("bilibili")          # 没起就先拉起来(冷启动要拉一次浏览器)
    items, seen, first_err = [], set(), None
    for i, kw in enumerate(kws):
        if i:
            time.sleep(KW_GAP)
        res = _post("bilibili", kw, since=since, until=until, windows=windows)
        try:
            _raise_if_err("bilibili", res, throttle_hint=True)
        except CrawlError as e:
            # 一个检索词翻车不该把这一轮**已经搜到的**样本一起丢掉(2026-09-17 实测: 第二个词
            # 被服务拖到 504, 第一个词那批好数据连带已取的字幕全没了)。记一笔接着搜下一个词;
            # 只有**一个词都没搜成**才把错抛出去 —— 那才是"平台不行", 该让引擎退避。
            first_err = first_err or e
            _err("bili 检索词 %r 没成: %s(其余词照收)" % (kw, e))
            continue
        for it in _bili_items(res):
            key = it.get("dedup_key")
            if key in seen:               # 两个词会搜到同一批热门视频, 只留先到的那份
                continue
            seen.add(key)
            items.append(it)
    if first_err and not items:
        raise first_err
    supervisor.touch("bilibili")
    _err("bili kw=%s -> %d items" % ("/".join(kws)[:60], len(items)))
    return items


def bili_subtitles(bvids, limit=2, max_minutes=30):
    """取 bili 视频字幕全文(高热度视频解析用): 服务端按给定顺序看, 时长超门槛的跳过, 取满 limit 条即停。

    失败**不抛**(返回 []): 字幕是给答案加料, 不是主料 —— 服务忙/没登录/网络断了都不该把这一题带走。
    但会经 _err 留一行, 免得"没取到"被读成"这些视频本来就没字幕"。
    """
    bv = [b for b in (bvids or []) if b]
    if not bv or limit <= 0:
        return []
    res = _post_path("bilibili", "/subtitle",
                     {"bvids": bv, "limit": limit, "max_minutes": max_minutes}, timeout=240)
    if res.get("error"):
        _err("bili 字幕没取到: %s" % res.get("error"))
        return []
    items = [it for it in (res.get("items") or []) if (it.get("text") or "").strip()]
    _err("bili 字幕 %d 条(试了 %d 个视频, 跳过 %d 个)"
         % (len(items), len(bv), len(res.get("skipped") or [])))
    return items


def _official_items(res, game):
    """官号探针响应 -> items: 每条动态 kind=official_post, 其下评论 kind=official_reply。
    动态 title 带探针判定(高峰/平峰 + 评论数/基准线), 评论 title 带所属动态的判定与正文摘要
    —— 评论落库后按时间倒序排列, 不写进 title 就分不清哪条评论属于哪个动态。
    kind 用 official_* 前缀: 归档里一眼区分"官方口径" vs "玩家口径"(NGA 帖/bili 视频)。"""
    items = []
    base = res.get("baseline_comments")
    base_txt = ("%d" % base) if isinstance(base, (int, float)) else "-"
    # 窗口口径: 探针按时间段取(不是"最近 N 条"), 每条 title 都带窗口, 模型才知道这批动态覆盖到哪
    win = "%s~%s" % (res.get("window_since") or "?", res.get("window_until") or "?")
    trunc = "" if res.get("window_reached", True) else "(窗口未翻到头, 更早的月份可能没取到)"
    for d in res.get("dynamics") or []:
        did = d.get("id")
        if not did:
            continue
        peak = bool(d.get("is_peak"))
        tag = "高峰" if peak else "平峰"
        url = "https://www.bilibili.com/opus/%s" % did
        body = (d.get("text") or "").strip()
        cmts = d.get("comments") or []
        # 日期用站上那套相对文案(8月11日/3天前, 人一眼认得出), 拿不到就退回 ISO 的月-日
        rel = d.get("published_at_rel") or (str(d.get("published_at") or "")[5:10])
        head = "[官号·%s·%s] 评论 %s / 基准线 %s(窗 %s 内中位数)%s" % (
            tag, rel, d.get("comment"), base_txt, win, trunc)
        if d.get("comments_error"):
            head += "(评论未取到)"
        # 投稿类动态带视频信息, 附上播放量供"热度"论据
        extra = ""
        if d.get("bvid"):
            extra = "\n视频: %s 播放 %s" % (d.get("bvid"), d.get("play"))
        items.append({"kind": "official_post", "title": head[:500],
                      "text": ((body or "[无正文]") + extra)[:2000],
                      "author": res.get("official_name") or game,
                      "raw_id": str(did), "url": url,
                      "published_at": str(d.get("published_at") or ""),
                      "like_count": _num(d.get("like")),
                      "reply_count": _num(d.get("comment")),
                      "video_tags": "官号动态,%s" % tag,
                      "dedup_key": "bili_off:%s" % did})
        # 评论落库后按时间倒序、跟别的动态的评论混在一起 -> title 必须带上"哪条动态"(日期+评论数,
        # 同一天两条同型动态靠评论数区分) + 该动态是不是高峰, 否则模型没法把评论挂回动态。
        parent = "[官号·%s·%s·评%s] %s" % (tag, rel, d.get("comment"), (body or "[无正文]")[:70])
        for cm in cmts:
            cc = (cm.get("text") or "").strip()
            if not cc:
                continue
            rpid = str(cm.get("id") or "")
            items.append({"kind": "official_reply", "title": parent[:500],
                          "text": cc[:2000], "author": cm.get("author") or "",
                          "raw_id": ("%s_c%s" % (did, rpid)) if rpid else str(did),
                          "url": url, "published_at": str(cm.get("published_at") or ""),
                          "like_count": _num(cm.get("like")),
                          "video_tags": "官号评论,%s" % tag,
                          "dedup_key": "bili_off:%s_c%s" % (did, rpid)})
    return items


def crawl_official(game, timeout=600, since=None, until=None):
    """官号动态探针(舆情用): 取**since~until 这段**的官号动态 -> 中位数基准线 -> 标高峰 ->
    高峰深采 25 条评论/平峰 5 条。走 bili 服务端 POST /official。
    窗口由题目的时间锚点定(since/until = 'YYYY-MM-DD'): since 缺省 = 服务端近 42 天(约一个版本周期)。
    **不是"最近 10 条"** —— 问几年前的版本, 窗口就该落在几年前。**账号在服务端按名解析, 解析不出来
    就报错(不猜号)** —— 所以"取不到"要么是这游戏没进账号表且按名搜有歧义, 要么是服务/登录态出问题,
    都不是"社区没讨论"。

    错误语义: 账号没解到 / 动态流失败 / 窗内一条动态都没翻到 -> CrawlError(throttle=False,
    不是风控事件, 别熔断 bili); 传输超时/429 -> CrawlError(throttle=True), 交给引擎的 bili 退避锁。"""
    g = (game or "").strip()
    if not g:
        return []
    payload = {"game": g}
    if since:
        payload["since"] = since
    if until:
        payload["until"] = until
    # 官号探针也走 bili 服务, 同样按需拉起。这一路不是按词搜索(/crawl), 重发一次不加剧风控;
    # 而本地服务被巡检错杀、或正巧在重启时, 连接会当场被掐 —— 那种情况重连一次就好, 不必让
    # 整题丢掉「官方口径」这一侧面。只对传输层断连重试一次: 超时(接了不回)与 429 照旧不重试。
    res = {}
    for attempt in (0, 1):
        supervisor.ensure("bilibili")
        res = _post_path("bilibili", "/official", payload, timeout=timeout)
        if not res.get("error") or attempt or not _dropped(res):
            break
        _err("official game=%s 连接被掐(%s), 重连一次" % (g, str(res.get("error"))[:60]))
    if res.get("error"):
        _raise_if_err("bilibili", res, throttle_hint=True)
    if res.get("resolved_by") == "ambiguous" or not res.get("mid"):
        cands = ", ".join("%s(mid=%s)" % (c.get("uname"), c.get("mid"))
                          for c in (res.get("candidates") or [])[:4])
        raise CrawlError("官号账号没认准(%s) —— 不猜号; 候选: %s"
                         % (res.get("error") or "有歧义", cands or "无"),
                         platform="bilibili", throttle=False)
    if not res.get("dynamics"):
        # 窗内一条都没有: 探针翻不到那么早 / 账号在窗口期内没发动态。别让模型读成"官方没发东西"。
        why = ("动态流取不到: %s" % res.get("dynamics_error")) if res.get("dynamics_error") \
            else "翻页扫了 %s 条就到底或撞上保险丝" % res.get("scanned")
        raise CrawlError("官号探针在 %s~%s 窗内没取到动态(%s)"
                         % (res.get("window_since"), res.get("window_until"), why),
                         platform="bilibili", throttle=False)
    # 窗口按时间算、翻页翻到一半可能撞风控(HTML 拦截页) —— 那时已经采到的动态照用, 不整批作废;
    # "没翻到窗口起点"这件事由 title 里的「窗口未翻到头」标注, 模型据此别断言"只有这些"。
    items = _official_items(res, g)
    _err("official game=%s 窗=%s~%s 扫=%s 动态=%s 基线=%s 高峰=%s -> %d items%s" % (
        g, res.get("window_since"), res.get("window_until"), res.get("scanned"),
        res.get("dynamics_count"), res.get("baseline_comments"), res.get("peak_count"), len(items),
        " [翻页中断: %s]" % str(res.get("dynamics_error"))[:60] if res.get("dynamics_error") else ""))
    return items


def crawl_nga(query, game, limit=8, since=None, until=None, windows=None):
    """NGA 真爬(独立 crawler 单元, 不依赖 bili; 以后可独立包成 tool 让用户选装)。
    key 由 _nga_key 组(有话题词 -> searchin|游戏|词 先定主板再版内搜; 只有游戏名 -> board|游戏)。
    since/until(YYYY-MM-DD) = 时间锚点: 服务端翻页到该时段再采, 而不是只取最新。
    windows = 分段窗(对比题"今年 vs 去年"): 每段各翻一窗再合并。"""
    key = _nga_key(game, query)
    if not key:
        return []
    supervisor.ensure("nga")               # 没起就先拉起来(冷启动要拉一次浏览器)
    res = _post("nga", key, since=since, until=until, windows=windows)
    _raise_if_err("nga", res)
    items = _nga_items(res, key)
    supervisor.touch("nga")
    # searchin 版内无命中 = 真没搜到(不兜底 dump 主板噪音); board/search 空同样如实空
    _err("nga key=%s win=%s..%s -> %d items" % (key[:40], since or "-", until or "-", len(items)))
    return items


# 平台 -> crawler 单元 注册表: 引擎/tool 只经此分发; "用户选装" = 装/卸注册项即增删爬虫能力面
CRAWLERS = {"nga": crawl_nga, "bilibili": crawl_bili}


def crawl(query, game, platform, limit=8, since=None, until=None, windows=None, keywords=None):
    """SEAM #2 provider 统一入口: 按 platform 分发到 crawl_nga/crawl_bili(向后兼容既有调用方,
    模型工具面仍是 crawl_live+platform 枚举, 不因源码拆分变)。
    keywords 只有 B站 收(NGA 是按 key 组检索词, 形状不同), 给 NGA 会被忽略。"""
    p = (platform or "").lower()
    if "bili" in p:
        return crawl_bili(query, game, limit=limit, since=since, until=until, windows=windows,
                          keywords=keywords)
    return crawl_nga(query, game, limit=limit, since=since, until=until, windows=windows)
