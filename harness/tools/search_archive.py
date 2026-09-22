# -*- coding: utf-8 -*-
"""模型面工具 #2: search_archive —— 只读历史归档, 不触发新爬。
as_of = 只答过去不答现在的刻度(观测日期上界); since = 时效题的时间窗下界(引擎按问句自动注入)。
和 crawl_nga/crawl_bili 对称: 模型自判"这题先翻历史还是现爬"。"""
from .. import archive

SUB_RECALL = 2000   # 回捞时字幕只给开头这么多字。crawl 那条路有"每轮 2 条"的额度兜着上下文,
                    # 回捞这条路没有(limit 由模型定) —— 一次捞几十行、每行再挂一份几千字的字幕就爆了。


def _row(r):
    """归档行 -> 给模型看的证据行。有字幕的行多一格 sub(截断处明说, 不静默砍)。"""
    row = {"id": r["id"], "platform": r["platform"], "kind": r["kind"],
           "title": (r["title"] or "")[:120], "text": (r["text"] or "").replace("\n", " ")[:600],
           "author": r["author"], "published_at": r["published_at"],
           "senti": {0: "负", 1: "中", 2: "正"}.get(r["senti_arg"]),
           "sarcasm": r["sarcasm"], "tags": r["topic_tags"],
           "view": r["view_count"], "like": r["like_count"], "reply": r["reply_count"],
           "danmaku": r["danmaku_count"], "video_tags": r["video_tags"]}
    sub = (r.get("sub_text") or "").strip()
    if sub:
        row["sub"] = sub if len(sub) <= SUB_RECALL else (
            "%s…(字幕共 %d 字, 这里只给开头; 全文见该条的引用卡片)" % (sub[:SUB_RECALL], len(sub)))
    return row


def schema():
    return {
        "description": "按游戏/平台/时间/关键词检索本机历史归档(不带新爬; 含早先会话爬到的样本), 返回样本供作答",
        "params": {"query": str, "game": str, "platform": str, "as_of": str, "limit": int},
    }


def call(ctx, params):
    conn = ctx["conn"]
    scope = ctx.get("scope") or "prod"
    words = tuple(w for w in [params.get("query")] if w)
    # 时效题的下界由引擎传入(跟 crawl 的入档闸门同一把尺), 模型不传时取空 = 不设下界
    since = params.get("since") or ""
    rows = archive.search(conn, scope=scope, game=params.get("game"), platform=params.get("platform"),
                          as_of=params.get("as_of"), since=since, words=words,
                          limit=params.get("limit"))
    total = archive.count(conn, scope=scope, game=params.get("game"), platform=params.get("platform"),
                          as_of=params.get("as_of"), since=since, words=words)
    return {"total": total, "rows": [_row(r) for r in rows]}
