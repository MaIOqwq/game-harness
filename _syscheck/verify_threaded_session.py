#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨线程会话自检 —— 「同一会话连问两题」在**真线程模型**下还成不成立。

病根(2026-09-15 真跑捉到, 不是推演出来的):
    网页后端把 Session 按会话缓存起来(_SESSIONS), 而**每一题都开一条新线程**跑(webapp._run_ask)。
    而 sqlite3.connect() 默认 check_same_thread=True —— 连接认死"建它的那条线程"。
    于是:**第一题好好答完, 第二题直接 ProgrammingError**(SQLite objects created in a thread
    can only be used in that same thread), 报错在收口前, 用户看到的就是"本题失败"。

为什么以前的自检照不出来: 它们全在**一条线程**里跑完整段 —— 建连接、提问、断言都在同一线程,
连接的主人是自己, 永远不越界。这个坏法只在"缓存对象 + 多线程"这个真实形态里现形,
所以它躲过了 50 项的会话压缩自检、35 项的位置扫描自检、17 项的会话回收自检。

本文件钉三件事:
  ① 一条连接, 在**另一条线程**上用, 不抛 ProgrammingError(schema 播种/检查点读写都走一遍);
  ② **模拟网页后端的真实用法** —— Session 在第 1 题的线程里建、第 2 题在另一条线程里接着用,
     两轮都要收得了口;
  ③ 跨线程跑完, 落下的流水与检查点是**完整的**, 不是半截。

纯离线: 假模型(零网络、不调真 LLM、不拉爬虫)。
用法: python _syscheck/verify_threaded_session.py   退出码 0=全过, 1=有失败。
"""
import os
import sys
import tempfile
import threading

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

fails = []
total = ok = 0


def chk(name, cond, got=""):
    global total, ok
    total += 1
    print(("PASS" if cond else "FAIL"), name)
    if cond:
        ok += 1
    else:
        fails.append("%s -> %r" % (name, got))


TMP = tempfile.mkdtemp(prefix="thr_")

from harness import checkpoint, db, flow                        # noqa: E402
from harness import registry as hregistry                       # noqa: E402
from harness.runner.session import Session                      # noqa: E402

flow._FLOW_DIR = os.path.join(TMP, "flow")                      # 别把自检流水写进产品数据目录
os.makedirs(flow._FLOW_DIR, exist_ok=True)

DBP = os.path.join(TMP, "t.db")
conn = db.connect(DBP)
db.init(conn)
hregistry.seed_builtin(conn)

MAIN = threading.get_ident()

# ---------- ① 建连接的线程 != 用它的线程 ----------
errs = []


def _foreign_use():
    try:
        conn.execute("SELECT 1").fetchone()
        hregistry.seed_builtin(conn)                            # 写
        checkpoint.save(conn, "web:probe", {"card": {}, "engine_n": 1})
        checkpoint.load(conn, "web:probe")                      # 读
    except Exception as e:
        errs.append(repr(e))


_t = threading.Thread(target=_foreign_use)
_t.start()
_t.join()
chk("①-0 上面那段确实是在**另一条**线程里跑的", threading.get_ident() == MAIN)
chk("①-1 换一条线程用同一条连接, 不抛 ProgrammingError", not errs, errs)

# ---------- ② 照网页后端的真实用法: 缓存 Session, 每轮一条新线程 ----------
# 关键在**第 1 题的线程里建 Session**, 第 2 题在另一条线程里接着用 —— 这才是线上那条路径。
holder = {}
results = []


def _fake_llm(messages, tools=True, stream=False):
    return {"choices": [{"message": {"role": "assistant",
            "content": "线程轮答完。\n\n结论: 跨线程这轮收口了"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2}}


def _turn(q):
    try:
        if "sess" not in holder:
            s = Session(conn=conn, scope="web:thr")             # 模拟 _session() 懒建
            s.engine._llm = _fake_llm
            s.engine._ext_specs = lambda: []                    # 别读真设置、别连真外部工具服务
            holder["sess"] = s
        r = holder["sess"].ask_turn(q, mode="快速")
        results.append(("ok", (r or {}).get("answer") or ""))
    except Exception as e:
        results.append(("err", repr(e)))


for i in (1, 2):
    th = threading.Thread(target=_turn, args=("跨线程第%d题：" % i + "这游戏现在怎么样",))
    th.start()
    th.join()

chk("②-1 两轮都收得了口(没有 ProgrammingError)", all(s == "ok" for s, _ in results), results)
chk("②-2 两轮的答案都拿得到、非空",
    len(results) == 2 and all(a for _, a in results), results)
chk("②-3 Session 确实只建了一次(第 2 轮是**复用**缓存对象, 不是重开)",
    "sess" in holder and len(results) == 2, len(results))

# ---------- ③ 跨线程跑完, 落地的东西是完整的 ----------
recs = flow.records("web:thr")
chk("③-1 跨线程那两轮都落了流水", len(recs) == 2, len(recs))
chk("③-2 两条流水的问句是各自的(没串)",
    len(recs) == 2 and "第1题" in (recs[0].get("question") or "")
    and "第2题" in (recs[1].get("question") or ""),
    [r.get("question") for r in recs])
snap = checkpoint.load(conn, "web:thr")
chk("③-3 检查点落在库里(不是半截)", bool(snap) and "card" in snap, snap)
chk("③-4 第 2 轮看得见第 1 轮(缓存对象跨轮带着同一个引擎)",
    len(recs) == 2 and holder["sess"].engine.scope == "web:thr", "")

print()
print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !!", f)
sys.exit(1 if fails else 0)
