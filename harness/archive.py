# -*- coding: utf-8 -*-
"""L3 archive store: crawl_run + observation 的写路径与检索。
写 = 自动节点(crawl_nga/crawl_bili 真爬工具的副作用), 模型不决定存不存(模型挑存=证据丢失+选择偏差);
检索 = search_archive 的数据底。度量字段由 measure 填充: 本地 LoRA(nlp-tool)为主, 可疑反讽才导流 DeepSeek judge。
落盘的不是逐字原文: crawl_run.raw_payload 存的是**过滤后**的样本(命中兄弟作/早于时间窗的已剔除, 重复样本仍在),
observation 的 title/text 另按 500/2000 字符截断 —— 详见 record_crawl 的说明。"""
import datetime
import json

from . import measure
from .evidence import pool_clause, pool_of


def _where(scope="prod", game=None, platform=None, as_of=None, since=None, words=()):
    cond, args = pool_clause(scope)
    clause = ["WHERE " + cond]
    if game:
        clause.append("AND game_key=?")
        args.append(game)
    if platform:
        clause.append("AND platform=?")
        args.append(platform)
    if as_of:
        clause.append("AND published_at <= ?")
        args.append(as_of)
    if since:
        clause.append("AND published_at >= ?")
        args.append(since)
    if words:
        cond = " OR ".join("(title LIKE ? OR text LIKE ?)" for _ in words)
        clause.append("AND (" + cond + ")")
        for w in words:
            args += ["%" + w + "%", "%" + w + "%"]
    return " ".join(clause), args


def _leaked(item, terms):
    """样本是否明显属于兄弟作: 标题/正文命中任一兄弟作外表名即算。
    只判"跑到兄弟作去了", 不做通用游戏归属 (跨 IP 对比帖必须留存)。"""
    if not terms:
        return None
    blob = (item.get("title") or "") + " " + (item.get("text") or "")
    for t in terms:
        if t in blob:
            return t
    return None


def _stale(item, since):
    """样本是否早于时间窗下界。published_at 缺失/格式异常时不判(宁留不误杀, 交由模型自行标注)。"""
    if not since:
        return False
    p = (item.get("published_at") or "")[:19]
    return bool(p) and len(p) >= 10 and p < since


def record_crawl(conn, meta, items, scope="prod"):
    """一次爬取 = 一条 crawl_run(**过滤后的** payload, 非逐字全量) + 逐条 observation。
    dedup_key 冲突则跳过(append-only, 不覆盖)。dedup_key 值前缀**证据池名** => 同一池内跨会话去重
    (另一个会话爬到同一条帖子只算 dup, 不重复入库 —— 否则同一批料在同一台机器上会存好几份,
    检索时重复、计数虚高); 实验池(exp:*)各成一体, 与真实池永不撞键。
    两道入档闸门(都只看样本自身, 爬取照跑不误伤平台状态; 被弃样本**不进** run 的 raw_payload ——
    落库的是过滤后的样本, 不是爬虫拿回来的逐字全量; 另 observation.title/text 截断到 500/2000 字符):
      meta["exclude_terms"]  兄弟作外表名 -> 命中即弃, 计 summary["leak"]
      meta["exclude_before"] 时间窗下界(YYYY-MM-DD...) -> 更早的弃, 计 summary["stale"]
    问句带"近期/最近/当下"这类时效词时才给 exclude_before; 历史题不给, 老样本照留。"""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    terms = tuple(meta.get("exclude_terms") or ())
    since = (meta.get("exclude_before") or "")[:19]
    leak_hits = [(i, _leaked(it, terms)) for i, it in enumerate(items)]
    leak_hits = [(i, t) for i, t in leak_hits if t]
    stale_hits = [i for i, it in enumerate(items) if i not in {j for j, _t in leak_hits} and _stale(it, since)]
    dropped = set(i for i, _t in leak_hits) | set(stale_hits)
    kept = [it for i, it in enumerate(items) if i not in dropped]
    items = kept
    dup = 0
    pending = []                                   # (dedup_key, item) 只对真新增度量(批量一次)
    for it in items:
        raw = it.get("dedup_key") or ("%s:%s" % (meta.get("platform"), it.get("raw_id") or it.get("url")))
        key = "%s|%s" % (pool_of(scope), raw)
        if conn.execute("SELECT 1 FROM observation WHERE dedup_key=?", (key,)).fetchone():
            dup += 1
            continue
        pending.append((key, it))
    # **度量要在开写事务之前**: measure 走 nlp-tool / 大模型复核, 慢起来几十秒到几分钟, 而 SQLite
    # 的写锁是整库一把 —— 压着写锁跑, 另一个对话同时落库就会撞 "database is locked"(默认只等 5 秒),
    # 那一题直接报错。这段只读不写(SELECT 在默认隔离级别下不开事务), 挪到前面结果分毫不差。
    scores = measure.score_records(
        [{"title": it.get("title") or "", "text": it.get("text") or ""} for _key, it in pending])
    cur = conn.execute(
        "INSERT INTO crawl_run(query,game_key,platform,started_at,finished_at,row_count,raw_payload,scope) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (meta.get("query"), meta.get("game"), meta.get("platform"), now, now,
         len(items), json.dumps(items, ensure_ascii=False), scope))
    run_id = cur.lastrowid
    for (key, it), sc in zip(pending, scores):
        conn.execute(
            "INSERT INTO observation(crawl_id,game_key,platform,kind,title,text,author,raw_id,url,"
            "published_at,crawled_at,source_query,dedup_key,senti_arg,sarcasm,topic_tags,"
            "view_count,like_count,reply_count,danmaku_count,video_tags,sub_text,scope) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, meta.get("game"), meta.get("platform"), it.get("kind", "reply"),
             (it.get("title") or "")[:500], (it.get("text") or "")[:2000], it.get("author"),
             it.get("raw_id"), it.get("url"), it.get("published_at"), now, meta.get("query"),
             key, sc["senti_arg"], sc["sarcasm"], sc["topic_tags"],
             it.get("view_count"), it.get("like_count"), it.get("reply_count"),
             it.get("danmaku_count"), it.get("video_tags"), it.get("sub_text"), scope))
    conn.commit()
    return {"run_id": run_id, "new": len(pending), "dup": dup,
            "leak": len(leak_hits), "leak_terms": sorted(set(t for _i, t in leak_hits)),
            "stale": len(stale_hits), "since": since or ""}


def rows_of_run(conn, run_id, limit=None):
    """一次真爬落库的**全部** observation, 按发布时间倒序(新的在前)。
    09-12 改: 原为 ORDER BY id DESC LIMIT n —— 只把该批尾部几条给模型看, 同批其余样本等于白爬,
    也违反 ARCHITECTURE P5「护栏优先于裁剪」。现在全量返回, 上下文由引擎 IN_CEILING 单点收口。"""
    sql = ("SELECT id,game_key,platform,kind,title,text,author,raw_id,published_at,senti_arg,sarcasm,topic_tags,"
           "view_count,like_count,reply_count,danmaku_count,video_tags,sub_text "
           "FROM observation WHERE crawl_id=? ORDER BY published_at DESC, id DESC")
    args = [run_id]
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def search(conn, scope="prod", game=None, platform=None, as_of=None, since=None, words=(), limit=None):
    """命中样本按发布时间倒序返回; limit=None = 全给(09-12 起 search_archive 也不截条数)。"""
    clause, args = _where(scope, game, platform, as_of, since, words)
    sql = ("SELECT id,game_key,platform,kind,title,text,author,raw_id,published_at,senti_arg,sarcasm,topic_tags,"
           "view_count,like_count,reply_count,danmaku_count,video_tags,sub_text "
           "FROM observation " + clause + " ORDER BY published_at DESC, id DESC")
    if limit:
        sql += " LIMIT ?"
        args = args + [limit]
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def count(conn, scope="prod", game=None, platform=None, as_of=None, since=None, words=()):
    clause, args = _where(scope, game, platform, as_of, since, words)
    return conn.execute("SELECT COUNT(*) FROM observation " + clause, args).fetchone()[0]
