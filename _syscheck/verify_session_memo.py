#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会話压缩 + 答案回捞 自检(纯离线: 不连网、不调 LLM、不起浏览器)。

盯的是这一轮补的那件事:「一个会话里要能一直答下去, 用户问起之前某一轮的具体内容得答得上来」。
坏法都是静默的 —— 压缩丢了题、回捞串了会话、截断把原话截没了, 界面上看不出来, 只有当真追问时
才发现答非所问。所以逐条钉死:

  ① 会话压缩   —— 早前翻篇的题压成纪要(题号+问句+结论), 最近几轮仍给原话;
                  逼到字数上限先丢最早的、**永远保住开篇那题**; 别的会话的题一个字都不许进来。
  ② 答案回捞   —— 按词或按题号取回那一轮的答案**全文**; 取不到就报错并把这会话问过哪些题列出来;
                  只读本会话(跨会话捞不到); 超长截断要注明。
  ③ 接线       —— 回捞是注册表里的正式一件工具(引擎/MCP/设置页三处同名), 引擎工具面里有它。
  ④ 流水读口   —— records() 只读本 scope 那一个文件、按写入序; recent() 就是它取尾。

用法: python _syscheck/verify_session_memo.py   退出码 0=全过, 1=有失败。
"""
import copy
import json
import os
import sys
import tempfile

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


TMP = tempfile.mkdtemp(prefix="memo_")

from harness import db, flow, tools                                  # noqa: E402
from harness.tools import registry                                   # noqa: E402
from harness.engine import Engine                                    # noqa: E402

flow._FLOW_DIR = os.path.join(TMP, "flow")                           # 别把自检流水写进产品数据目录
os.makedirs(flow._FLOW_DIR, exist_ok=True)

conn = db.connect(os.path.join(TMP, "t.db"))
db.init(conn)


def put(scope, q, concl, ans=None):
    flow.append(scope, {"mode": "快速", "question": q, "concl": concl,
                        "answer": ans if ans is not None else ("答案正文：" + q + "。" + concl),
                        "ev": [], "trace": [], "rounds": 1})


def q(i):
    return "甲会话第%d题" % i


# ---------- ④ 流水读口 ----------
put("web:A", q(1), "结论: 甲1")
put("web:A", q(2), "结论: 甲2")
put("web:B", "乙会话唯一一题", "结论: 乙1")
put("web:B", "ZZUNIQ乙专有词", "结论: 乙2")
recs_a = flow.records("web:A")
chk("④-1 records() 只读本 scope 的流水", [r["question"] for r in recs_a] == [q(1), q(2)],
    [r.get("question") for r in recs_a])
chk("④-2 records() 按写入序(不是字典序/时间倒序)", recs_a[0]["question"] == q(1))
chk("④-3 recent(n) 就是 records() 取尾", flow.recent("web:A", 1) == recs_a[-1:])
chk("④-4 没有流水的 scope 回空表, 不报错", flow.records("web:从没问过") == [])

engine_a = Engine(conn, provider=None, scope="web:A")
engine_b = Engine(conn, provider=None, scope="web:B")

# ---------- ① 会话压缩 ----------
chk("①-0 web:A 目前只 2 题", len(flow.records("web:A")) == 2)
chk("①-1 会话还短(≤keep_recent) -> 不插纪要, 原话都在下文",
    engine_a._session_memo(keep_recent=3) == "", engine_a._session_memo(keep_recent=3))

for i in range(3, 7):                                                # 补到 6 题
    put("web:A", q(i), "结论: 甲%d" % i)
memo = engine_a._session_memo(keep_recent=3)
chk("①-2 6 题 keep_recent=3 -> 只有最早那 3 题进纪要", memo.count("→") == 3, memo.count("→"))
chk("①-3 纪要覆盖的是最早 3 题(第 4-6 题仍以原话在下文给)",
    all(q(i) in memo for i in (1, 2, 3)) and not any(q(i) in memo for i in (4, 5, 6)), memo)
chk("①-4 纪要带题号, 且从 1 起连续", "1. " + q(1) in memo and "3. " + q(3) in memo, memo)
chk("①-5 纪要里带结论行(不是只有问句)", "结论: 甲1" in memo and "结论: 甲3" in memo)
chk("①-6 纪要明说它是摘要、要原话得调 recall_answer", "recall_answer" in memo and "别拿这行摘要" in memo)
chk("①-7 纪要报的是**会话总数**(6)与已翻篇数(3), 别让它把题号口径搞混",
    "会话至今共 6 题" in memo and "下面这 3 题" in memo, memo[:160])
chk("①-7b 纪要明说题号与 recall_answer 的 index 同一把尺",
    "与 recall_answer 的 index 同一把尺" in memo, memo[:200])

memo_b = engine_b._session_memo(keep_recent=0)
chk("①-8 会话隔离: 甲会话的题一个字都不进乙的纪要",
    "甲会话" not in memo_b and q(1) not in memo_b, memo_b[:200])

long_q, long_c = "长" * 40, "结论: " + "长" * 86
for i in range(40):                                                  # 撑爆字数上限
    put("web:BIG", "%s%02d" % (long_q, i), long_c)
engine_big = Engine(conn, provider=None, scope="web:BIG")
big = engine_big._session_memo(keep_recent=3)
chk("①-9 再长的会话, 纪要长度也有界(不随题数涨)", len(big) < 2600, len(big))
chk("①-10 逼到上限 -> 如实说明中间有多少题没摆进来", "压在字数上限外" in big)
chk("①-11 丢的是中间那些, **开篇那题永远保住**", (long_q + "00") in big, big[:300])
# 40 题里最近 3 题由原话那侧给, 纪要只覆盖最早 37 题 —— 上限内保住的是其中**较近**的那些(第 36 题在)
chk("①-12 上限内保留的是较近的题(不是从最早开始一路砍)", (long_q + "36") in big,
    big[big.rfind("36. "):][:80] if "36. " in big else big[:200])

put("web:C", "没有收口的题", "", ans="")
engine_c = Engine(conn, provider=None, scope="web:C")
for i in range(3):
    put("web:C", "补题%d" % i, "结论: 补%d" % i)
memo_c = engine_c._session_memo(keep_recent=3)
chk("①-13 没收口的题在纪要里标「(未收口)」, 不冒充有结论", "(未收口)" in memo_c, memo_c[:200])

# ---------- ② 答案回捞 ----------
ctx_a = {"conn": conn, "scope": "web:A"}
r = registry.call(ctx_a, "recall_answer", {"index": 2})
chk("②-1 index 精确取会话内第 2 题", r.get("items") and r["items"][0]["question"] == q(2), r)
chk("②-2 回的是**答案全文**(不是结论行)", "答案正文" in (r["items"][0].get("answer") or ""), r)
chk("②-3 顺带报出会话内一共几题", r.get("total") == 6, r.get("total"))

r = registry.call(ctx_a, "recall_answer", {"query": "第3题"})
chk("②-4 按词命中对应那一题", r.get("items") and r["items"][0]["question"] == q(3),
    [i["question"] for i in r.get("items") or []])
chk("②-5 命中时也不串别的会话(乙会话不在结果里)",
    "乙会话" not in str(r), str(r)[:200])

r = registry.call(ctx_a, "recall_answer", {"index": 99})
chk("②-6 index 越界 -> 报错并说清一共几题", bool(r.get("error")) and "6 题" in r["error"], r)
r = registry.call(ctx_a, "recall_answer", {"index": "甲"})
chk("②-7 index 不是整数 -> 报错不瞎猜", bool(r.get("error")), r)
r = registry.call(ctx_a, "recall_answer", {"query": "宇宙飞船登月计划"})
chk("②-8 全不命中 -> 报错并列出这会话问过哪些题(好改词)",
    bool(r.get("error")) and r.get("questions") and q(1) in r["questions"], r)
r = registry.call(ctx_a, "recall_answer", {})
chk("②-9 query/index 都不给 -> 报错, 不默认拿最近一条糊弄", bool(r.get("error")), r)

r = registry.call({"scope": "web:从没问过"}, "recall_answer", {"query": "随便"})
chk("②-10 空会话 -> 如实说没得捞", bool(r.get("error")) and "空的" in r["error"], r)
# 会话隔离的正向验法: 拿只在甲会话出现过的词去乙会话捞 —— 捞不到才算数。
# (拿"甲会话"这种词不行: "会话"两字会撞上乙自己答案里的字, 命中的是乙自己的题。)
r = registry.call({"scope": "web:B"}, "recall_answer", {"query": "甲会话第一题"})
chk("②-11 跨会话捞不到(只在甲出现过的词, 在乙的召回里查无此物)",
    bool(r.get("error")) or all(i["question"] not in [q(1), q(2)] for i in r.get("items") or []), r)

put("web:LONG", "超长答案题", "结论: 长长",
    ans="正文" * 4000)                                               # 8000 字
r = registry.call({"scope": "web:LONG"}, "recall_answer", {"index": 1})
got = r["items"][0]["answer"]
chk("②-12 超长答案截断到 6000 字并注明截断", len(got) < 6100 and "截断" in got, len(got))
chk("②-13 回捞结果明说「旧引用号本轮未必成立, 别照抄」",
    "别照抄" in (r.get("note") or ""), r.get("note"))

# ---------- ③ 接线 ----------
names = [t["name"] for t in registry.BUILTIN]
chk("③-1 recall_answer 进了唯一注册表", "recall_answer" in names, names)
chk("③-2 两种形状同名(MCP 与 function-calling 同一份清单)",
    "recall_answer" in [x["function"]["name"] for x in registry.defs()]
    and "recall_answer" in [x["name"] for x in registry.mcp_tools()])
chk("③-3 默认就开(不是可选工具)", registry.enabled("recall_answer") is True)
sch = registry.BY_NAME["recall_answer"]["schema"]["properties"]
chk("③-4 参数表给了 query / index / limit", set(sch) == {"query", "index", "limit"}, sorted(sch))
chk("③-5 中文名与说明齐(设置页要显示)", bool(registry.BY_NAME["recall_answer"]["label"])
    and bool(registry.BY_NAME["recall_answer"]["desc"]))

eng = Engine(conn, provider=None, scope="web:A")
names_face = [d["function"]["name"] for d in eng._tool_defs()]
chk("③-6 引擎给模型的工具面里有 recall_answer", "recall_answer" in names_face, names_face)

# ---------- ⑤ 真跑一轮 ask(假模型: 零网络、不碰 LLM、不拉爬虫) ----------
# 前四段验的是"零件对不对", 这段验的是"装到机上真转起来时, 该带的东西到底带没带" ——
# 假模型把每次收到的 messages 原样记下来, 于是"模型看不看得见早前的题""调 recall_answer 拿不拿得到原话"
# 这两件事就从"我读过代码"变成"跑出来的证据"。爬虫不会被拉起来(假模型从不调 crawl), 也不出网。
from harness import registry as hregistry                            # noqa: E402
hregistry.seed_builtin(conn)

calls = []


def _fake_llm(messages, tools=True, stream=False):
    calls.append({"messages": copy.deepcopy(messages), "tools_on": bool(tools),
                  "tool_names": [d["function"]["name"] for d in (eng._tools or [])]})
    if len(calls) == 1:                                              # 第一轮: 模型要回捞第 1 题
        return {"choices": [{"message": {"role": "assistant", "content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "recall_answer", "arguments": '{"index": 1}'}}]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    return {"choices": [{"message": {"role": "assistant",
            "content": "照第 1 题的原话答给你。\n\n结论: 回捞到了, 细节与原话一致。"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5}}


eng._llm = _fake_llm
eng._ext_specs = lambda: []                                       # 别去读真设置、连真的外部工具服务
res = eng.ask("你刚才第一题说的具体是什么？", mode="快速")

blob0 = json.dumps(calls[0]["messages"], ensure_ascii=False)
chk("⑤-1 ask 真跑起来了(假模型被调用)", len(calls) >= 2, len(calls))
chk("⑤-2 第一轮上下文里摆了**早前那些题**的纪要", "已翻篇的早前轮次" in blob0 and q(1) in blob0,
    blob0[:200])
# 纪要那段与"原话"那段是分开的两块: 第 1 题在纪要里, 第 6 题(最近 3 轮)以原话给。
# 按结构取那一条 system 消息来看, 别拿字符串切开数 —— 切歪了会把下文原话也算进来。
_memo_msg = [m for m in calls[0]["messages"]
             if m.get("role") == "system" and "已翻篇的早前轮次" in (m.get("content") or "")]
chk("⑤-3 纪要只有一条, 且只摆翻篇的(第 1-3 题)、最近几轮不在其中重复",
    len(_memo_msg) == 1 and q(1) in _memo_msg[0]["content"] and q(6) not in _memo_msg[0]["content"],
    [_m.get("content", "")[:160] for _m in _memo_msg])
chk("⑤-4 最近 3 轮以**原话**摆在下文(第 6 题在, 不是只剩摘要)",
    q(6) in blob0 and "甲会话第6题" in blob0, "")
chk("⑤-5 本轮问句排在历史之后(顺序反了模型会当成「答完又补一句」)",
    blob0.rfind("你刚才第一题说的具体是什么") > blob0.rfind(q(6)), "")
chk("⑤-6 工具面里有 recall_answer(模型才可能想起来用它)", "recall_answer" in calls[0]["tool_names"],
    calls[0]["tool_names"])

blob1 = json.dumps(calls[1]["messages"], ensure_ascii=False)
chk("⑤-7 第二轮里出现了**回捞出来的原话**(那一轮的答案全文)",
    "答案正文：甲会话第1题" in blob1, blob1[-400:])
chk("⑤-8 回捞结果带「旧引用号本轮未必成立」的提醒, 不会教模型照抄",
    "别照抄" in blob1, "")
chk("⑤-9 收口是干净结论行(能进历史供下一轮用)", "结论: 回捞到了" in (res.get("answer") or ""), res.get("answer"))
_qs_a = [r["question"] for r in flow.records("web:A")]
chk("⑤-10 这一轮自己也落了流水(下一轮才捞得回它)",
    len(_qs_a) == 7 and any("你刚才第一题说的具体是什么" in x for x in _qs_a), _qs_a)

memo2 = eng._session_memo()
chk("⑤-11 会话长一轮, 纪要跟着长一轮(第 4 题这轮进来)", "4. " + q(4) in memo2, memo2[:400])

calls[:] = []
eng2 = Engine(conn, provider=None, scope="web:另一个会话")
eng2._llm = _fake_llm
eng2._ext_specs = lambda: []
eng2.ask("头一题", mode="快速")
blob_new = json.dumps(calls[0]["messages"], ensure_ascii=False)
chk("⑤-12 换个会话真跑: 上下文里一个字都没有上一个会话的题(会话隔离落到 messages 上)",
    q(1) not in blob_new and "已翻篇的早前轮次" not in blob_new, blob_new[:300])

print()
print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !!", f)
sys.exit(1 if fails else 0)
