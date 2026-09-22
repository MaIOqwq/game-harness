# -*- coding: utf-8 -*-
"""引文挂靠检查: [id=N] 前面那句引号里的话, 到底出在不在 N 这条帖子里?

管的是「引文逐字对得上、但号挂错了」这类错 —— 既不进引用能对回校验(号是真实存在的 id),
也不进纪律判定(确实有引用), verifier 全绿而读者点进去对不上。实测命中 2 题:
  Q5 把 [id=342] 的正文记在 [id=343] 名下; Q6 把 [id=476] 的引文挂到 [id=473][474]。
两处都是**相邻 id 抄岔**, 人眼扫过去看不出来, 只有拿原文逐字比对才现形。

口径:
- 只在「引用前面那一截文字」(上一次引用之后 ~ 本条引用之前)里找引号内容;
- 引号四种: 「」『』“” 和成对的直引号;
- 归一化后做子串匹配(去空白与 ** 加粗标记);
- 本 id 里找不到 -> 在同 scope 其余帖子里找: **唯一**命中 = 挂错号(报出真正归属);
  查无出处或命中多处一律不报 —— 那多半是模型自己的转述被引号括起来, 报出来只会淹掉真信号。

除「查」以外还给一个「改」: repair_answer() 在收口时把挂错号的引用就地改正(引擎调, 见
engine._seal_answer)。查(本文件 main / check_answer)仍是只读的复盘工具, 不改任何东西。

用法(只读):
  python -m harness.quotecheck --db <sqlite> [--flow-dir harness/data/flow]
                              [--tag cert14-20260912 --n 14] [--scope exp:calib:...] [--detail]
退出码: 0=无挂错, 1=有挂错。
"""
import argparse
import os
import re
import sqlite3
import sys

from .cites import iter_cites, strip_bad
from .evidence import pool_clause

# 引号四式: 中文直角/双直角/弯引号/成对直引号(直引号那式最脏, 要求成对且内侧无引号)
_QUOTES = (re.compile(r"「([^」]{4,})」"),
           re.compile(r"『([^』]{4,})』"),
           re.compile(r"“([^”]{4,})”"),
           re.compile(r"\"([^\"\n]{4,})\""))
_WS = re.compile(r"\s+")
_GAP_MIN = 4          # 引号内容归一化后短于此直接不看(噪音太大)
_MIN_LEN = 10         # 判定用的最短引文: 短引文多是公共短语/简称, 撞车概率高
# 只在「引文在本 scope 里唯一出现」时才认挂错 —— 转述、简称、标签(如「阵营/反讽」)等
# 在库里查无出处, 若一并报出来会把真信号淹掉(实测放开时 202 条查无 vs 171 条对上)。
# 宁可漏报也不误报: 这是要挂到答案上的标注, 误报会稀释可信度。


def _norm(s):
    """归一化: 去掉空白与 markdown 加粗标记, 便于子串比对。"""
    return _WS.sub("", (s or "").replace("**", ""))


def _quote_spans(text):
    """取出一段话里所有引号内容 -> [(归一化文本, 闭引号在本段里的偏移)]。"""
    out = []
    for rx in _QUOTES:
        for m in rx.finditer(text or ""):
            n = _norm(m.group(1))
            if len(n) >= _GAP_MIN:
                out.append((n, m.end()))
    return out


def _quotes(text):
    """取出一段话里所有引号内容(归一化后)。"""
    return [n for n, _ in _quote_spans(text)]


def check_answer(answer, conn, scope, min_len=_MIN_LEN):
    """逐条引用核对 -> [{id, quote, verdict, actual}]。
    verdict: ok(引文就在被引帖里) / misattributed(引文不在被引帖, 但在同池另一帖里唯一出现)。
    引文查无出处的一律不报 —— 那多半是模型自己的转述被引号括起来, 不是挂错号。"""
    text = answer or ""
    cites = list(iter_cites(text))
    if not cites:
        return []           # 无引用 -> 不碰库(轻量 fixture 可能没归档表)
    try:
        cond, sargs = pool_clause(scope)   # 证据按池读(跨会话可回捞); 只有记忆那一半按 scope 隔离
        rows = conn.execute("SELECT id, title, text FROM observation WHERE " + cond, sargs).fetchall()
    except sqlite3.Error:
        return []           # 归档表不在(裸 fixture/只读库): 静默放行 —— 本检查是顾问角色, 绝不打断作答主链
    corpus = [(rid, _norm((ti or "") + (tx or ""))) for rid, ti, tx in rows]
    by_id = dict(corpus)
    out = []
    for k, (start, _end, _inner, ids) in enumerate(cites):
        prev_end = cites[k - 1][1] if k else 0
        claim = text[prev_end:start]
        for q in _quotes(claim):
            if len(q) < min_len:
                continue
            if any(q in by_id.get(oid, "") for oid in ids):
                out.append({"id": ids[0], "quote": q[:40], "verdict": "ok", "actual": ids[0]})
                continue
            hits = [rid for rid, blob in corpus if q in blob]
            if len(hits) == 1:                      # 唯一出处 -> 挂错号可信
                out.append({"id": ids[0], "quote": q[:40],
                            "verdict": "misattributed", "actual": hits[0]})
    return out


def repair_answer(answer, conn, scope, min_len=_MIN_LEN):
    """把挂错号的引用**就地改正**(只读校验之外的唯一一处写动作, 但仍不动库)。

    两种病一起治(§10.49):
      - **引用粒度**: 一段话并排引用好几条、只在段尾挂了一个号 —— 每条引文各补一个自己的号;
      - **真·挂错号**: 号下的引文其实出自别处 —— 把号补到正确的帖子上, 原来那个号若再没有
        引文撑着就剔掉。
    动手的前提与 check_answer 同一口径: 引文在本池里**唯一**出现在另一条帖子里才认,
    查无出处或命中多处的**一律不碰**(那多半是模型自己的转述被引号括起来, 乱改只会更糟)。
    返回 (新正文, 改动清单); 没改动则原样返回。"""
    text = answer or ""
    if not list(iter_cites(text)):
        return answer, []
    try:
        cond, sargs = pool_clause(scope)   # 同 check_answer: 证据按池读, 记忆才按 scope 隔离
        rows = conn.execute("SELECT id, title, text FROM observation WHERE " + cond, sargs).fetchall()
    except sqlite3.Error:
        return answer, []
    corpus = [(rid, _norm((ti or "") + (tx or ""))) for rid, ti, tx in rows]
    if not corpus:
        return answer, []
    by_id = dict(corpus)

    # 补号会**改变后面每条 token 的归属区间**(补进去的号自己也是一条 token, 把它后面那截切走了),
    # 所以一趟不够: 第一趟补的号可能把某句原本"撑得住"的引文推到别的号名下, 那它就还得再补一次。
    # 每趟至少补一个号、且补过的引文从此落在自己号下不会再被补 —— 单调收敛, 跑满 passes 也对。
    fixes = []
    for _ in range(4):
        text, fx = _repair_once(text, corpus, by_id, min_len)
        if not fx:
            break
        fixes.extend(fx)
    return text, fixes


def _repair_once(text, corpus, by_id, min_len):
    """一趟: 给"有唯一真出处却没挂在自己号下"的引文各补一个号, 再剔掉被搬空的号。"""
    cites = list(iter_cites(text))
    inserts, fixes, token_state = [], [], {}
    for k, (start, _end, _inner, ids) in enumerate(cites):
        prev_end = cites[k - 1][1] if k else 0
        claim = text[prev_end:start]
        supported, moved, unknown = False, 0, 0
        for q, off in _quote_spans(claim):
            if len(q) < min_len:
                continue
            if any(q in by_id.get(oid, "") for oid in ids):
                supported = True                      # 这条 token 确实撑得住其中一句引文
                continue
            hits = [rid for rid, blob in corpus if q in blob]
            if len(hits) == 1:
                moved += 1
                inserts.append((prev_end + off, "[id=%d]" % hits[0]))
                fixes.append({"quote": q[:40], "was": ids[0], "now": hits[0]})
            else:
                unknown += 1
        token_state[k] = (supported, moved, unknown)

    if not fixes:
        return text, []
    new = text
    for pos, ins in sorted(inserts, key=lambda x: -x[0]):   # 从后往前插, 免得前面的插入把后面偏移顶歪
        new = new[:pos] + ins + new[pos:]
    # 只在这一条 token 的引文**全都**改挂到别处(既没有撑得住的、也没有查无出处的)时才剔原号:
    # 只要还有一句查无出处, 那个号就可能是它真正的出处, 剔了就是丢证据。
    drop = []
    for k, (_s, _e, _i, ids) in enumerate(cites):
        supported, moved, unknown = token_state[k]
        if moved and not supported and not unknown:
            drop.extend(ids)
    if drop:
        new, dropped = strip_bad(new, drop)
        if dropped:
            fixes.append({"dropped": dropped})
    return new, fixes


def _scopes(conn, tag, n):
    for i in range(1, n + 1):
        yield "exp:calib:%s:q%d" % (tag, i)


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="引文挂靠检查(只读)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--flow-dir", default=os.path.join("harness", "data", "flow"))
    ap.add_argument("--tag", default=None, help="如 cert14-20260912 -> scope=exp:calib:<tag>:qN")
    ap.add_argument("--n", type=int, default=0)
    ap.add_argument("--scope", default=None, help="只查某 scope(与 --tag 互斥)")
    ap.add_argument("--detail", action="store_true", help="逐条打印 ok 项")
    args = ap.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from harness import flow

    conn = sqlite3.connect(args.db)
    if args.scope:
        scopes = [args.scope]
    else:
        scopes = list(_scopes(conn, args.tag, args.n)) if (args.tag and args.n) else []

    bad = okn = 0
    for sc in scopes:
        recs = flow.read(scope=sc, flow_dir=args.flow_dir)
        if not recs:
            print("(无流水) %s" % sc)
            continue
        rec = recs[-1]
        rows = check_answer(rec.get("answer") or "", conn, sc)
        prob = [r for r in rows if r["verdict"] != "ok"]
        okn += len(rows) - len(prob)
        bad += len(prob)
        head = "%s  引文核对 %d 条" % (sc.split(":")[-1], len(rows))
        if prob:
            print("!! %s  <- %d 条挂错号" % (head, len(prob)))
            for r in prob:
                print("     引文「%s」不在 [id=%d], 实际出自 [id=%s]"
                      % (r["quote"], r["id"], r["actual"]))
        else:
            print("   %s  <- 无挂错" % head)
        if args.detail:
            for r in rows:
                if r["verdict"] == "ok":
                    print("     ok  [id=%d] %s" % (r["id"], r["quote"]))
    print("-" * 66)
    print("引文核对合计: 对上 %d, 挂错 %d" % (okn, bad))
    conn.close()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
