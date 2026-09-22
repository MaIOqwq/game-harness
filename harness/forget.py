# -*- coding: utf-8 -*-
"""会话记忆的清除 —— 前台「删除这个对话」按钮落到后端的那一层。

删**不是立刻抹掉**, 而是分两步(2026-09-16 用户定):
  ① 删的当下 —— `trash_move`: 把这一份记忆**整段搬进垃圾桶**, 活的那一侧(checkpoint / session_claim /
     流水文件)当场清空。所以删完再进这个对话是从零开始, 不是接着旧卡答 —— 这正是要的效果;
  ② 过了 7 天 —— `sweep`: 垃圾桶里过期的那几行真删掉, 那才是物理清除。中间用户反悔了, `trash_restore` 整段搬回去。
     (过期时间落在 expire_at 上, 恢复时按原样写回, 一行不多不少。)

清的**只有这一个会话自己的那一份**: checkpoint(L2 检查点) + session_claim(结论日志) + flow 流水。
三样一律不碰:
  - 证据池(crawl_run / observation): 别的会话也在引它, 删了历史答案里的 [id=N] 就核不回原帖了;
  - L4 注册表(alias_resolution): 全机器共享的黑话账本, 不归任何单个会话所有;
  - 别的会话: 全按 scope 精确匹配, 一行不多删。

guard: 本模块是 harness/ 下**唯一**被允许出现删除原语的地方(guard._PURGE_ALLOWED 逐名放行)。
只增契约管的是**证据**(爬到的原帖永不改写、永不遗忘); 会话记忆本来就该让用户能清掉 ——
schema.sql 那句「物理清除是前台删除按钮的独立任务」说的就是这个模块。
"""
import json
import os
import time

from . import flow

# 垃圾桶保留期(用户定 7 天)。改这里 = 改前台的承诺, 故只此一处, 由 webapp 读出去给页面显示。
TRASH_DAYS = 7
TRASH_SECS = TRASH_DAYS * 24 * 3600


def _del(conn, sql, args):
    """删某张表里本 scope 的行, 返回删了几行; 表还没建(未 init)就按 0 算, 不炸主链。"""
    try:
        return conn.execute(sql, args).rowcount
    except Exception:
        return 0


def _rows(conn, sql, args):
    """查一批行; 表还没建就按"什么都没存"算, 不炸主链。"""
    try:
        return conn.execute(sql, args).fetchall()
    except Exception:
        return []


def _claim_row(r):
    """session_claim 一行 -> 可以 JSON 化的 dict(列名写死, 免得库里加列把存档格式带跑偏)。"""
    return {"cid": r["cid"], "q": r["q"], "concl": r["concl"], "ev": r["ev"],
            "moved_at": r["moved_at"], "topic": r["topic"]}


def _safe(name):
    return (name or "").strip()


def purge(conn, scope, flow_dir=None):
    """把 scope 这个会话的记忆从**活的那一侧**清掉, 返回各层清掉多少(前台照实显示, 不编数)。

    调用方须先确认**这个 scope 没有题在跑**: 一轮答到一半被清掉, 收口时会把流水又写回来。
    conn 用调用方的(webapp 那条链上就是那个会话自己的连接)。

    注意这是"从活的那侧挪走", 不等于物理清除 —— 前台删对话走 trash_move(它会先存进垃圾桶再调这里);
    只有 sweep 过期清扫才是真抹掉。
    """
    sc = _safe(scope)
    if not sc:
        raise ValueError("forget.purge: scope 不能为空")
    n_card = _del(conn, "DELETE FROM checkpoint WHERE scope=?", (sc,))
    n_claim = _del(conn, "DELETE FROM session_claim WHERE scope=?", (sc,))
    conn.commit()
    p = flow.fpath(sc, flow_dir)
    n_flow = 0
    if os.path.isfile(p):
        os.remove(p)
        n_flow = 1
    return {"checkpoint": n_card, "session_claim": n_claim, "flow_file": n_flow}


def trash_move(conn, scope, title=None, flow_dir=None, now=None):
    """删一个对话: 先整段存进垃圾桶, 再从活的那侧清掉。返回 {cleared, expire_at}。

    「本来就没东西」的会话不往垃圾桶里塞一条空记录 —— 那会让用户在垃圾桶里看见一堆自己没印象的条目。
    """
    sc = _safe(scope)
    if not sc:
        raise ValueError("forget.trash_move: scope 不能为空")
    ts = int(now if now is not None else time.time())
    cards = _rows(conn, "SELECT card FROM checkpoint WHERE scope=?", (sc,))
    claims = _rows(conn, "SELECT cid,q,concl,ev,moved_at,topic FROM session_claim WHERE scope=?", (sc,))
    p = flow.fpath(sc, flow_dir)
    text = None
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            text = f.read()
    if not cards and not claims and text is None:
        return {"cleared": purge(conn, sc, flow_dir), "expire_at": None}
    # **先落垃圾桶、再清活的那侧** —— 顺序反过来的话, 清完却存不进垃圾桶(库被锁/表没建),
    # 这一份记忆就凭空没了。反过来的风险只是"垃圾桶里有条空的", 那无害。
    conn.execute("INSERT OR REPLACE INTO trash(scope,title,deleted_at,expire_at,card,claims,flow) "
                 "VALUES(?,?,?,?,?,?,?)",
                 (sc, (title or "").strip()[:120], ts, ts + TRASH_SECS,
                  cards[0]["card"] if cards else None,
                  json.dumps([_claim_row(r) for r in claims], ensure_ascii=False) if claims else None,
                  text))
    conn.commit()
    return {"cleared": purge(conn, sc, flow_dir), "expire_at": ts + TRASH_SECS}


def trash_list(conn, now=None):
    """垃圾桶里还剩什么(不含正文大块, 列表页用不着): 标题 / 什么时候删的 / 还剩几天。"""
    ts = int(now if now is not None else time.time())
    out = []
    for r in _rows(conn, "SELECT scope,title,deleted_at,expire_at FROM trash ORDER BY deleted_at DESC", ()):
        left = int(r["expire_at"]) - ts
        out.append({"scope": r["scope"], "title": r["title"] or "",
                    "deleted_at": int(r["deleted_at"]), "expire_at": int(r["expire_at"]),
                    "days_left": max(0, (left + 86399) // 86400)})   # 向上取整: 还剩 2 小时也该显示「1 天」
    return out


def trash_restore(conn, scope, flow_dir=None):
    """把垃圾桶里这一份整段搬回活的会话。返回各层放回多少; 不在垃圾桶里返回 None(调用方据此回 404)。

    搬回去以后**必须把内存里缓存的那个 Session 丢掉**(webapp 那侧做): 那上面挂着的是一张空状态卡,
    不丢的话下次进这个会话照样接着空卡答, 等于白恢复。
    """
    sc = _safe(scope)
    if not sc:
        raise ValueError("forget.trash_restore: scope 不能为空")
    rows = _rows(conn, "SELECT scope,title,card,claims,flow FROM trash WHERE scope=?", (sc,))
    if not rows:
        return None
    row = rows[0]
    n_card = n_claim = n_flow = 0
    if row["card"] is not None:
        conn.execute("INSERT OR REPLACE INTO checkpoint(scope,card,saved_at) VALUES(?,?,?)",
                     (sc, row["card"], int(time.time())))
        n_card = 1
    for c in json.loads(row["claims"] or "[]"):
        conn.execute("INSERT OR REPLACE INTO session_claim(cid,q,concl,ev,moved_at,topic,scope) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (c.get("cid"), c.get("q"), c.get("concl"), c.get("ev"),
                      c.get("moved_at"), c.get("topic"), sc))
        n_claim += 1
    conn.commit()
    if row["flow"] is not None:
        p = flow.fpath(sc, flow_dir)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(row["flow"])
        n_flow = 1
    _del(conn, "DELETE FROM trash WHERE scope=?", (sc,))
    conn.commit()
    # 标题跟着回给前端: 侧栏那一行的标题只存在浏览器里, 不还回去的话恢复出来的对话就叫「新对话」了
    return {"checkpoint": n_card, "session_claim": n_claim, "flow_file": n_flow,
            "title": row["title"] or ""}


def sweep(conn, now=None):
    """把垃圾桶里过了保留期的真删掉, 返回清掉几条(过期=物理清除, 到这一步才找不回来)。"""
    ts = int(now if now is not None else time.time())
    rows = _rows(conn, "SELECT scope FROM trash WHERE expire_at<=?", (ts,))
    n = 0
    for r in rows:
        n += _del(conn, "DELETE FROM trash WHERE scope=?", (r["scope"],))
    if n:
        conn.commit()
    return n
