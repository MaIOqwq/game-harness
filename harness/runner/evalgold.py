# -*- coding: utf-8 -*-
"""黄金回放 v0: 对录得的真实 flow 条目做确定性打分(不烧 LLM, 不触网)。

数据底: harness/data/flow/*.jsonl(每 scope 一条真实 ask 流水, 由 engine 收口自动落) +
        harness/data/<case.db>(标定库: observation/crawl_run 为落库真源)。
case 声明见 gold_cases.json; 分数只在"意外不符"时 exit 1 —— 已知弱项(kind=known_issue,
expect_fail 列出)按期望触发即不算阻塞, 单列计数可见。

判定原子:
  C1 收口存在   answer 含「结论:」, 且 concl 非空、非 "(未收口)"
  C2 引用对回   answer 每个 [id=N] 都能在 DB observation 找到 N, 且其 scope == case scope
               (跨 scope 引用 = 隔离泄漏, 就是抓它)
  C3 无引即避   cites==0 的答案必须是 abstain 措辞(证据不足以/无法确认…); 0-cite 的实指断言
               -> 违反引用纪律
  C4 有真爬     case scope 在 DB crawl_run 有落库记录(非凭空作答)
exp_flow 缺失(case 的 scope 在 flow 里找不到条目)记 ERR。

用法:
  python -m harness.runner.evalgold          # 常规打分
  python -m harness.runner.evalgold --json   # 逐 case 一行机器可读
退出: 0 = 全部如期望; 1 = 意外失败 / 条目缺失。"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "harness", "data")
HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "gold_cases.json")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from harness import flow  # noqa: E402
from harness.cites import cite_ids  # noqa: E402

# abstain 措辞(命中即视为"避答/不足", C3 对 0-cite 实指才开火)
ABSTAIN_MARK = ("证据不足以", "无法坐实", "无法确认", "未能", "未检索到",
                "未找到", "样本不足", "不足以", "(未收口)", "无直接")


def _abstain(text):
    return any(m in (text or "") for m in ABSTAIN_MARK)


def _cites(answer):
    # 走 cites.py: 成组/区间写法原先被严格单引用正则整段漏掉 -> 那些 id 不进 C2 校验(实测 2 题)
    return cite_ids(answer or "")


def _run_checks(entry, conn, scope):
    """返回 {check_id: (ok, detail)}。"""
    answer = entry.get("answer") or ""
    concl = entry.get("concl") or ""
    out = {}

    c1 = ("结论:" in answer) and bool(concl) and concl != "(未收口)"
    out["c1"] = (c1, "结论行: %s" % (concl[:40] if concl else "(空/未收口)"))

    ids = _cites(answer)
    bad = []
    for n in ids:
        row = conn.execute("SELECT id, scope FROM observation WHERE id=?", (n,)).fetchone()
        if row is None:
            bad.append("[id=%d] 无此 observation" % n)
        elif row["scope"] != scope:
            bad.append("[id=%d] scope=%s != %s(跨 scope 引用)" % (n, row["scope"], scope))
    out["c2"] = (not bad, "引用 %d 条, 全部对回本 scope" % len(ids) if not bad
                 else "; ".join(bad))

    if not ids:
        ok_c3 = _abstain(answer) or _abstain(concl)
        out["c3"] = (ok_c3, "0 引用 %s" % ("abstain 措辞(放行)" if ok_c3
                                           else "但为实指断言 -> 违反引用纪律"))
    else:
        out["c3"] = (True, "%d 引用(不需触发 abstain 要求)" % len(ids))

    n_run = conn.execute("SELECT COUNT(*) AS n FROM crawl_run WHERE scope=?",
                         (scope,)).fetchone()["n"]
    out["c4"] = (n_run > 0, "crawl_run 落库 %d 条" % n_run)
    return out


def _find_entry(scope):
    for rec in flow.read(scope=scope):
        return rec
    return None


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="黄金回放确定性打分 v0")
    ap.add_argument("--json", action="store_true", help="逐 case 一行机器可读")
    args = ap.parse_args(argv)

    import json
    cases = json.load(open(CASES, encoding="utf-8"))["cases"]

    stats = {"pass": 0, "known": 0, "unexpected": 0, "err": 0}
    for c in cases:
        name, scope, db = c["name"], c["scope"], c["db"]
        entry = _find_entry(scope)
        if entry is None:
            stats["err"] += 1
            print("ERR  %-30s 无对应 flow 条目" % name)
            continue
        dbp = os.path.join(DATA, db)
        if not os.path.exists(dbp):
            stats["err"] += 1
            print("ERR  %-30s 缺 DB: %s" % (name, db))
            continue
        conn = sqlite3.connect(dbp)
        conn.row_factory = sqlite3.Row
        checks = _run_checks(entry, conn, scope)
        conn.close()

        expect_fail = set(c.get("expect_fail") or [])
        fails = {k for k, (ok, _d) in checks.items() if not ok}
        # 期望失败集里恰好失败 = 已知弱项如期望; 其余失败 = 意外; 期望失败却没失败 = 意外
        unexpected = (fails - expect_fail) | (expect_fail - fails)
        status = "PASS"
        if c["kind"] == "known_issue":
            if not fails and not unexpected:
                status = "KNOWN-MISS"
            elif unexpected:
                status = "UNEXPECTED"
            else:
                status = "KNOWN"   # 预期弱项恰好失败
        else:
            if unexpected:
                status = "UNEXPECTED"

        if status == "PASS":
            stats["pass"] += 1
        elif status in ("KNOWN", "KNOWN-MISS"):
            stats["known"] += 1
        elif status == "UNEXPECTED":
            stats["unexpected"] += 1

        if args.json:
            print("RESULT\t%s\t%s\tc1=%s c2=%s c3=%s c4=%s" % (
                status, name,
                checks["c1"][0], checks["c2"][0], checks["c3"][0], checks["c4"][0]))
        else:
            print("%-8s %s" % (status, name))
            for k in ("c1", "c2", "c3", "c4"):
                ok, det = checks[k]
                print("     C%s %s  %s" % (k[1], "ok  " if ok else "FAIL", det))

    print("-" * 60)
    print("evalgold: %d PASS  %d KNOWN(弱项如期望)  %d UNEXPECTED  %d ERR"
          % (stats["pass"], stats["known"], stats["unexpected"], stats["err"]))
    return 1 if (stats["unexpected"] or stats["err"]) else 0


if __name__ == "__main__":
    sys.exit(main())
