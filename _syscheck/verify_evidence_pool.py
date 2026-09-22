#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据池 + 追问上下文 自检(纯离线, 不起浏览器, 不连网)。

盯的是这一轮改动的五条断言 —— 每一条都对应一个"改错了会静默出错"的坏法:
  ① 会话隔离没破: 新会话的状态卡/结论日志/检查点是空的, 上一轮的问答不串过来;
  ② 但**证据**跨会话可见: 甲会话爬到的样本, 乙会话 search_archive 搜得到、引得到;
  ③ 标定池(exp:*)仍然与真实池互不可见, 两个方向都查一遍;
  ④ 同一池内重复爬到同一条 = dup, 不存两份(否则检索重复、计数虚高);
  ⑤ 追问: 上一轮的问答真进了这一轮的 messages, 且游戏上下文能从上轮锚定值兜底。

用法: python _syscheck/verify_evidence_pool.py   退出码 0=全过, 1=有失败。
"""
import os
import shutil
import sys
import tempfile
import types

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


TMP = tempfile.mkdtemp(prefix="evpool_")
DB = os.path.join(TMP, "t.db")

from harness import archive, db, flow, quotecheck, registry, archive as _a  # noqa: E402
from harness.evidence import pool_of, pool_clause   # noqa: E402
from harness.runner.session import Session          # noqa: E402
from harness import webview                          # noqa: E402
from harness.engine import _sanitize_cites           # noqa: E402

flow._FLOW_DIR = os.path.join(TMP, "flow")           # 别把自检流水写进产品数据目录
os.makedirs(flow._FLOW_DIR, exist_ok=True)

conn = db.connect(DB)
db.init(conn)
registry.seed_builtin(conn)


def mkitems(tag, n=2):
    return [{"kind": "reply", "title": "%s-%d" % (tag, i), "text": "%s 的正文 %d" % (tag, i),
             "raw_id": "%s%d" % (tag, i), "url": "http://x/%s%d" % (tag, i),
             "published_at": "2026-09-0%d" % (i + 1)} for i in range(n)]


# ---------- ① 池折算本身 ----------
chk("①-1 prod/web 同池", pool_of("prod") == pool_of("web:abc") == "local")
chk("①-2 exp 各自成池", pool_of("exp:c:t:q1") == "exp:c:t:q1"
    and pool_of("exp:c:t:q1") != pool_of("exp:c:t:q2"))
chk("①-3 真实池的条件不匹配 exp 前缀", "NOT LIKE" in pool_clause("web:abc")[0])

# ---------- 写两批证据: 甲会话 + 标定 ----------
r1 = archive.record_crawl(conn, {"query": "甲", "game": "三角洲行动", "platform": "NGA"},
                          mkitems("甲"), scope="web:A")
r2 = archive.record_crawl(conn, {"query": "标定", "game": "三角洲行动", "platform": "NGA"},
                          mkitems("标定"), scope="exp:calib:t:q1")

# ---------- ② 跨会话可检索 ----------
hit_b = archive.search(conn, scope="web:B", game="三角洲行动")
chk("②-1 乙会话搜得到甲会话爬的样本", len(hit_b) == 2, [r["title"] for r in hit_b])
chk("②-2 甲会话自己搜得到", len(archive.search(conn, scope="web:A", game="三角洲行动")) == 2)
cnt_b = archive.count(conn, scope="web:B", game="三角洲行动")
chk("②-3 count() 与 search() 口径一致", cnt_b == 2, cnt_b)

# ---------- ③ 标定池两个方向都不串 ----------
chk("③-1 真实池看不到标定样本",
    not any("标定" in (r["title"] or "") for r in archive.search(conn, scope="web:B", game="三角洲行动")),
    [r["title"] for r in archive.search(conn, scope="web:B", game="三角洲行动")])
chk("③-2 标定池看不到真实样本",
    len(archive.search(conn, scope="exp:calib:t:q1", game="三角洲行动")) == 2,
    [r["title"] for r in archive.search(conn, scope="exp:calib:t:q1", game="三角洲行动")])
chk("③-3 两个标定 scope 之间也不串",
    len(archive.search(conn, scope="exp:calib:t:q2", game="三角洲行动")) == 0)

# ---------- ④ 同池去重 ----------
r3 = archive.record_crawl(conn, {"query": "乙", "game": "三角洲行动", "platform": "NGA"},
                          mkitems("甲"), scope="web:B")      # 同一条帖子, 换个会话再爬一次
chk("④-1 重复爬到同一条 -> dup, 不新增", r3["new"] == 0 and r3["dup"] == 2, r3)
chk("④-2 池里仍只有 2 条(没存两份)",
    len(archive.search(conn, scope="web:B", game="三角洲行动")) == 2)

# ---------- ⑤ 引用按池解析: 跨会话的引得住, 编造的照剔 ----------
ids = [r["id"] for r in archive.search(conn, scope="web:A", game="三角洲行动")]
ans = "甲的证据在这 [id=%d]。编造的在这 [id=999999]。" % ids[0]
clean, bad = _sanitize_cites(ans, conn, "web:B")      # 乙会话来校验
chk("⑤-1 乙会话引甲会话的证据不被剔(池内可见)", bad == [999999], bad)
chk("⑤-2 编造 id 仍被剔", "[id=999999]" not in clean, clean)
clean_x, bad_x = _sanitize_cites(ans, conn, "exp:calib:t:q1")
chk("⑤-3 标定池里引真实池的号 -> 全部被剔", set(bad_x) == {ids[0], 999999}, bad_x)

qc_mis = quotecheck.check_answer("正文不动。", conn, "web:B")
chk("⑤-4 quotecheck 在池内可读(不抛错)", qc_mis == [], qc_mis)

# ---------- ⑥ 记忆那一半仍按 scope 严格隔离 ----------
def _empty_card(s):
    """空卡 = 既没有活跃结论, 也没有锚定的游戏上下文(render() 本身总会印一行 anchor, 不作数)。"""
    return not s.engine.card.active and not s.engine.card.corrections \
        and not s.engine.card.anchor.get("game")


s_a = Session(db_path=DB, scope="web:A")
s_b = Session(db_path=DB, scope="web:B")
chk("⑥-1 新会话状态卡是空的", _empty_card(s_b), s_b.card_text())
chk("⑥-2 新会话结论日志为空", s_b.log_count() == 0, s_b.log_count())
s_a.card_text()
s_a.engine.card.materialize("M1", "甲的问题", "甲的结论", ev="cites")
s_a._save_checkpoint()
s_a.engine._flush_active()
chk("⑥-3 甲会话落库后, 乙会话仍读不到", s_b.log_count() == 0 and _empty_card(s_b),
    (s_b.log_count(), s_b.card_text()))
s_b2 = Session(db_path=DB, scope="web:B")
chk("⑥-4 乙会话检查点没被甲的串进来(重启也不串)", _empty_card(s_b2), s_b2.card_text())
s_a2 = Session(db_path=DB, scope="web:A")
chk("⑥-5 甲会话自己续得上", "甲的结论" in s_a2.card_text(), s_a2.card_text())

# ---------- ⑦ 追问: 上一轮进 messages + 游戏上下文兜底 ----------
calls = []


def fake_llm(self, messages, tools=True, stream=False):
    calls.append(messages)
    return {"choices": [{"message": {"content": "社区口径见 [id=%d]。\n结论: 测试结论一句" % ids[0]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


s_q = Session(db_path=DB, scope="web:q")
s_q.engine._llm = types.MethodType(fake_llm, s_q.engine)
s_q.ask_turn("三角洲行动 最近社区氛围怎么样")
chk("⑦-1 第一轮没有历史(新会话一张白纸)",
    all(m.get("content", "").find("三角洲行动 最近") < 0 or m["role"] != "assistant" for m in calls[0]),
    calls[0])

calls.clear()
s_q.engine.card.anchor["game"] = "三角洲行动"          # 上一轮真爬过 -> 锚定
m, g = s_q.resolve("那它最近呢")
chk("⑦-2 追问题面不含游戏名 -> 用上轮锚定值兜底", g == "三角洲行动", g)
s_q.ask_turn("那它最近呢")
msgs = calls[0]
chk("⑦-3 追问这轮的 messages 里带上了上一轮问答",
    len(msgs) >= 4 and msgs[1]["role"] == "user" and "三角洲行动 最近社区氛围怎么样" in msgs[1]["content"]
    and msgs[2]["role"] == "assistant" and "测试结论一句" in msgs[2]["content"],
    [m.get("role") for m in msgs])
chk("⑦-4 历史排在本轮问句之前",
    [i for i, mm in enumerate(msgs) if mm["role"] == "user"][-1] == len(msgs) - 1
    or msgs[-1]["role"] != "user" or len([mm for mm in msgs if mm["role"] == "user"]) == 2,
    [m.get("role") for m in msgs])
chk("⑦-5 历史 assistant 那侧不带 [id=](不教模型照抄号)",
    "[id=" not in msgs[2]["content"], msgs[2]["content"])

# ---------- ⑧ 出题卡: 跨会话引用解析得出来 ----------
rec = {"scope": "web:B", "ev": "", "question": "乙问", "answer": "见 [id=%d]" % ids[0],
       "concl": "c", "trace": [], "ts": "2026-09-15T10:00:00", "mode": "快速"}
ask = webview.build_ask(conn, rec, ask_id="t1")
chk("⑧-1 跨会话引用被解析成 cites(不是 missing)",
    len(ask["cites"]) == 1 and ask["cite_check"]["missing"] == [],
    (len(ask["cites"]), ask["cite_check"]))
chk("⑧-2 reused 记下了'这条来自早先归档'", ask["stats"]["reused"] == 1, ask["stats"])
chk("⑧-3 这题没新爬 -> new=0", ask["stats"]["new"] == 0, ask["stats"])

shutil.rmtree(TMP, ignore_errors=True)
print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !!", f)
sys.exit(1 if fails else 0)
