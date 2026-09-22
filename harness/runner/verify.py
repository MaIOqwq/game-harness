# -*- coding: utf-8 -*-
"""确定性 verifier 雏形(§10.8 评估分层第一层, 免费不烧 LLM):
吃 jsonl 流水(每题一条, engine 收口自动落) + 归档 DB, 逐条判:
  ① concl_ok    答案带干净『结论:』(未收口 视为缺);
  ② cite 校验   answer 里每个 [id=N] 必须能在归档 observation 表对回, 且 scope 与本题一致(scope 隔离);
  ③ ev 校验     ev 里 run#N 必须能在 crawl_run 表对回(可选, 默认关, --check-run 开)。

用法:
  python -m harness.runner.verify --flow-dir harness/data/flow --db <sqlite> [--scope exp:calib:...]
只读。退出码: 0=全过, 2=参数错, 1=有 FLAG。"""
import argparse
import os
import re
import sys

from ..cites import cite_ids


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


UNFIN = ("(未收口)", "")


def verify_record(rec, conn, check_run=False):
    """返回 (flags:list[str], stats:dict)。flags 空 = 通过。只读 conn。"""
    flags = []
    answer = rec.get("answer") or ""
    scope = rec.get("scope")
    # ① 结论
    concl = (rec.get("concl") or "").strip()
    if concl in UNFIN:
        flags.append("concl_missing(未收口)")
    elif not re.search(r"(?m)^\s*结论\s*[:：]\s*\S", answer):
        flags.append("concl_missing(正文无结论行)")
    # ② 引用能对回 + scope 隔离(走 cites.py: 成组/区间/混写一并校验, 别只看严格单引用)
    ids = cite_ids(answer)
    resolved = cross = unresolved = 0
    for oid in ids:
        row = conn.execute("SELECT scope FROM observation WHERE id=?", (oid,)).fetchone()
        if row is None:
            unresolved += 1
            continue
        elif row[0] != scope:
            cross += 1
        else:
            resolved += 1
    if unresolved:
        flags.append("cite_unresolved x%d" % unresolved)
    if cross:
        flags.append("cite_cross_scope x%d" % cross)
    # ③ ev 的 run 可对回(显式才查)
    run_missing = 0
    if check_run and rec.get("ev"):
        for rid in re.findall(r"run#(\d+)", rec["ev"]):
            row = conn.execute("SELECT scope FROM crawl_run WHERE id=?", (int(rid),)).fetchone()
            if row is None:
                run_missing += 1
    if run_missing:
        flags.append("ev_run_unresolved x%d" % run_missing)
    stats = {"ids": ids, "resolved": resolved, "cross": cross,
             "unresolved": unresolved, "run_missing": run_missing}
    return flags, stats


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(description="流水 verifier(引用对归档/结论格式, 只读)")
    ap.add_argument("--flow-dir", default=os.path.join("harness", "data", "flow"),
                    help="jsonl 流水目录(默认 harness/data/flow)")
    ap.add_argument("--db", required=True, help="归档 SQLite(observation/crawl_run)")
    ap.add_argument("--scope", default=None, help="只验某 scope")
    ap.add_argument("--check-run", action="store_true", help="顺带把 ev run# 对回 crawl_run")
    ap.add_argument("--detail", action="store_true", help="逐条打印 stats")
    args = ap.parse_args(argv)

    from harness import db, flow
    conn = db.connect(args.db)
    try:
        recs = flow.read(scope=args.scope, flow_dir=args.flow_dir)
        if not recs:
            print("无流水记录: flow-dir=%s scope=%s" % (args.flow_dir, args.scope or "*"))
            return 2
        hard = flag_n = cite_n = ok_n = 0
        for rec in recs:
            flags, stats = verify_record(rec, conn, check_run=args.check_run)
            if args.detail:
                print("detail %s | cites=%s resolve/cross/unres=%s/%s/%s" % (
                    rec.get("scope"), stats["ids"], stats["resolved"], stats["cross"], stats["unresolved"]))
            if flags:
                flag_n += 1
                hard += 1 if any("unresolved" in f or "cross" in f or "concl_missing" in f for f in flags) else 0
                print("FLAG %s | %s" % (rec.get("scope"), "; ".join(flags)))
            else:
                ok_n += 1
                cite_n += bool(stats["ids"])
        print("records=%d OK=%d FLAG=%d (其中 hard=%d, 带引用=%d)" % (
            len(recs), ok_n, flag_n, hard, cite_n))
        return 1 if flag_n else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
