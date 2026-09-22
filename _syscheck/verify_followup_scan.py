#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""追问的位置扫描自检 —— 「会话拉得很长以后, 回头问早前某一轮, 还答不答得上来」。

`verify_session_memo.py` 验的是「压缩 / 回捞这两件零件对不对」, 而且**只在一个固定位置**
(会话第 1 题)试过回捞。真实坏法不在固定位置: 会话一长, 用户回头问的是**哪一轮**完全是随机的,
而上下文对第 k 题只有三种待遇 ——

    · 最近 3 轮          -> 原话整段摆在下文            (够得着)
    · 更早但在字数预算内  -> 只剩纪要里一行「问句 → 结论行」 (要细节必须回捞)
    · 被字数上限挤出去的  -> 纪要里**连行都没有**          (只能靠 recall_answer)

所以这支自检把 k **扫一遍全场**(最远的 / 被挤出去的 / 预算内的 / 最近的), 逐个位置问同一句话
「你第 k 题当时具体怎么说的?」, 盯四件事:

  ① 该在上下文里的在不在; 不该全量在的别假装有 —— 纪要只该有结论行, **不该有答案正文**。
  ② 无论在哪个位置, recall_answer 按题号都能把**那一题**取回来(取成邻题 = 错位)。
  ③ 纪要里的题号与 recall_answer 的 index 是**同一把尺** —— 错一格, 全场错位。
  ④ **被挤出上下文的那些题, 不能因为没进上下文就丢了**。这是压缩最危险的静默坏法:
     界面上一切正常, 只有当真追问到"中间那几题"时才发现答非所问。

纯离线: 不连网、不调真 LLM、不起浏览器。
用法: python _syscheck/verify_followup_scan.py   退出码 0=全过, 1=有失败。
"""
import copy
import json
import os
import re
import sys
import tempfile

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


TMP = tempfile.mkdtemp(prefix="fscan_")

from harness import db, flow                                    # noqa: E402
from harness import registry as hregistry                       # noqa: E402
from harness.tools import registry                              # noqa: E402
from harness.engine import Engine                               # noqa: E402

flow._FLOW_DIR = os.path.join(TMP, "flow")                      # 别把自检流水写进产品数据目录
os.makedirs(flow._FLOW_DIR, exist_ok=True)

conn = db.connect(os.path.join(TMP, "t.db"))
db.init(conn)
hregistry.seed_builtin(conn)

N = 40                     # 会话题数: 要大到把纪要的字数上限撑爆(下面有前提断言兜底)

# 题号一律补零到三位 —— 否则 "第1题" 是 "第11题" 的子串, 逐题比对会假过。
def q(i):
    return "扫描会话第%03d题：这游戏最近的口碑走向到底怎么样" % i


def concl(i):
    return "结论: 扫描第%03d题——最近口碑偏冷, 主要在骂数值和卡池, 少数夸美术, 样本以哔哩哔哩为主" % i


ANS_MARK = "这是那一轮的全文，含具体措辞与引用的帖子细节"


def ans(i):
    return "答案正文(第%03d题)：%s" % (i, ANS_MARK)


def build(scope, n=N):
    for i in range(1, n + 1):
        flow.append(scope, {"mode": "快速", "question": q(i), "concl": concl(i),
                            "answer": ans(i), "ev": [], "trace": [], "rounds": 1})


def tiers(scope, keep_recent=3):
    """把这个会话里的每一题归到哪一档 —— 从**纪要真文**里读, 不重算 MEMO_BUDGET 的算法。"""
    recs = flow.records(scope)
    memo = Engine(conn, provider=None, scope=scope)._session_memo(keep_recent=keep_recent)
    in_memo = set(int(m.group(1)) for m in re.finditer(r"(?m)^(\d+)\. ", memo))
    recent = set(range(len(recs) - keep_recent + 1, len(recs) + 1)) if keep_recent else set()
    evicted = set(range(1, len(recs) + 1)) - in_memo - recent
    return recs, memo, in_memo, recent, evicted


# ---------- 0. 前提: 这个会话真的长到有题被挤出上下文 ----------
# (自检自己的地基: 没挤出的话第 ④ 条就没被测到, 会是假绿灯。)
build("scan:A")
recs_a, memo_a, in_memo_a, recent_a, evicted_a = tiers("scan:A")
chk("0-1 会话有 %d 题" % N, len(recs_a) == N, len(recs_a))
chk("0-2 纪要非空", bool(memo_a), memo_a[:80])
chk("0-3 有题被挤出字数上限(否则这支自检没测到最危险的那档)", len(evicted_a) > 0,
    sorted(evicted_a))
chk("0-4 三档都非空: 最近轮 / 纪要内 / 被挤出",
    len(recent_a) == 3 and len(in_memo_a) > 0 and len(evicted_a) > 0,
    (sorted(recent_a), len(in_memo_a), len(evicted_a)))
print("     位置分布: 最近3轮=%s | 纪要内 %d 题(含开篇) | 被挤出 %d 题(%s…%s)"
      % (sorted(recent_a), len(in_memo_a), len(evicted_a),
         min(evicted_a), max(evicted_a)))

# ---------- ① 三档各自的上下文待遇 ----------
chk("1-1 被挤出的题: 纪要里一个字都没有(上下文里确实找不到它)",
    all(q(i) not in memo_a for i in evicted_a),
    [i for i in evicted_a if q(i) in memo_a][:5])
chk("1-2 纪要内的题: 问句在纪要里(模型知道自己答过这题)",
    all(q(i) in memo_a for i in in_memo_a),
    [i for i in in_memo_a if q(i) not in memo_a][:5])
chk("1-3 纪要内的题: 结论行也在(不是只有问句)",
    all(concl(i)[:24] in memo_a for i in list(in_memo_a)[:3]), "")
chk("1-4 纪要里**只有**结论行、没有答案正文(要细节必须回捞, 不能靠纪要冒充原话)",
    ANS_MARK not in memo_a, memo_a[-200:])
chk("1-5 最近 3 轮不进纪要(避免和下文原话重复)",
    all(q(i) not in memo_a for i in recent_a), sorted(recent_a))
chk("1-6 纪要如实交代有题没摆进来", "压在字数上限外" in memo_a)
chk("1-7 开篇那题永远在(会话的锚, 无论怎么丢都留着)", 1 in in_memo_a, sorted(in_memo_a)[:5])
chk("1-8 纪要明说题号与 recall_answer 的 index 同尺", "同一把尺" in memo_a)
chk("1-9 纪要明说「别拿这行摘要当原话复述」", "别拿这行摘要当原话复述" in memo_a)

# ---------- ② 位置扫描: 逐个位置问「第 k 题」, 都必须取回**那一题** ----------
ctx_a = {"conn": conn, "scope": "scan:A"}
bad_q, bad_a, err = [], [], []
for k in range(1, N + 1):
    r = registry.call(ctx_a, "recall_answer", {"index": k})
    if r.get("error") or not r.get("items"):
        err.append((k, r.get("error")))
        continue
    got = r["items"][0]
    if got["question"] != q(k):
        bad_q.append((k, got["question"]))
    # 取回的必须是**这一题**的答案全文 —— 光对问句不够: 串了答案一样是错答。
    if ans(k) not in (got.get("answer") or ""):
        bad_a.append(k)
chk("2-1 全场 %d 个位置, 每个位置按题号都取得到(无一报错)" % N, not err, err[:5])
chk("2-2 每个位置取回的都是**那一题**的问句(没有错位一题)", not bad_q, bad_q[:5])
chk("2-3 每个位置取回的答案全文都是**那一题**的(不是邻题的答案)", not bad_a, bad_a[:5])
chk("2-4 取回的是答案全文(不是纪要那行结论)", ANS_MARK in
    registry.call(ctx_a, "recall_answer", {"index": min(evicted_a)})["items"][0]["answer"], "")

# ---------- ③ 同一把尺: 纪要里的题号 = recall_answer 的 index ----------
# 错一格的后果是"全场错位" —— 模型照纪要的题号去调, 拿回来的是别的题, 而且它自己看不出来。
mislabel = []
for k in sorted(in_memo_a):
    if ("%d. %s" % (k, q(k))) not in memo_a:                     # 纪要里第 k 行写的确实是第 k 题
        mislabel.append(k)
chk("3-1 纪要里「k. 」后面跟的确实是第 k 题的问句", not mislabel, mislabel[:5])
chk("3-2 被挤出的题也报得准: 越界题号报错并说清一共几题",
    "40 题" in (registry.call(ctx_a, "recall_answer", {"index": 41}).get("error") or ""),
    registry.call(ctx_a, "recall_answer", {"index": 41}))
chk("3-3 题号 0 / 负数都不接受(不悄悄当成第 1 题或倒数第 1 题)",
    bool(registry.call(ctx_a, "recall_answer", {"index": 0}).get("error"))
    and bool(registry.call(ctx_a, "recall_answer", {"index": -1}).get("error")), "")

# ---------- ④ 被挤出去的那档, 单独再钉一遍(最危险的静默坏法) ----------
mid = sorted(evicted_a)[len(evicted_a) // 2]
r = registry.call(ctx_a, "recall_answer", {"index": mid})
chk("4-1 被挤出上下文的题仍然捞得回来(第 %d 题, 纪要里没有它)" % mid,
    bool(r.get("items")) and r["items"][0]["question"] == q(mid) and ans(mid) in r["items"][0]["answer"],
    r)
chk("4-2 最远那题(第 1 题)与最近那题(第 %d 题)同样取得到" % N,
    registry.call(ctx_a, "recall_answer", {"index": 1})["items"][0]["question"] == q(1)
    and registry.call(ctx_a, "recall_answer", {"index": N})["items"][0]["question"] == q(N), "")

# ---------- ⑤ 隔离: 换个会话, 这些题一个字都捞不到 ----------
r = registry.call({"scope": "scan:别处"}, "recall_answer", {"index": 1})
chk("5-1 换个会话按同一题号捞 -> 如实说没得捞(不串到 scan:A)",
    bool(r.get("error")) and "空的" in r["error"], r)

# ---------- ⑥ 装到机上真跑一轮: 长会话里问「第 k 题具体怎么说的」 ----------
# 前半段验的是零件; 这段验的是**长会话真的转起来时, 模型手里到底有什么**。假模型把每轮收到的
# messages 原样记下来 —— 于是"被挤出的题它还看得见吗""它调 recall_answer 拿得到原话吗"
# 从"我读过代码"变成跑出来的证据。爬虫不会被拉起来(假模型从不调 crawl), 也不出网。
build("scan:B")
_, memo_b, in_memo_b, recent_b, evicted_b = tiers("scan:B")
deep = sorted(evicted_b)[len(evicted_b) // 3]                   # 挑一道**挤出区**的题来问
print("     真跑用题: 挤出区第 %d 题(纪要里没有它); 最近轮=%s" % (deep, sorted(recent_b)))

calls = []


def _fake_llm(messages, tools=True, stream=False):
    calls.append({"messages": copy.deepcopy(messages), "tools_on": bool(tools),
                  "tool_names": [d["function"]["name"] for d in (eng._tools or [])]})
    if len(calls) == 1:                                          # 第一轮: 模型要回捞那道题
        return {"choices": [{"message": {"role": "assistant", "content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "recall_answer",
                                             "arguments": json.dumps({"index": deep})}}]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    return {"choices": [{"message": {"role": "assistant",
            "content": "照第 %d 题的原话答给你。\n\n结论: 回捞到了, 细节与原话一致。" % deep}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5}}


eng = Engine(conn, provider=None, scope="scan:B")
eng._llm = _fake_llm
eng._ext_specs = lambda: []                                       # 别读真设置、别连真外部工具服务
res = eng.ask("你第 %d 题当时具体是怎么说的？" % deep, mode="快速")

chk("6-1 长会话里 ask 真跑起来了", len(calls) >= 2, len(calls))
blob0 = json.dumps(calls[0]["messages"], ensure_ascii=False)
chk("6-2 上下文里摆了早前那些题的纪要(模型知道自己答过很多题)",
    "已翻篇的早前轮次" in blob0 and q(1) in blob0, blob0[:160])
chk("6-3 最近 3 轮以**原话**在下文(不是只剩纪要)",
    q(N) in blob0 and q(N - 1) in blob0, "")
_memo_msg = [m for m in calls[0]["messages"]
             if m.get("role") == "system" and "已翻篇的早前轮次" in (m.get("content") or "")]
chk("6-4 纪要只有一条", len(_memo_msg) == 1, len(_memo_msg))
chk("6-5 要问的那题**确实不在**上下文里(挤出区) —— 所以它非得回捞不可",
    q(deep) not in _memo_msg[0]["content"], deep)
chk("6-6 本轮问句排在历史之后", blob0.rfind("当时具体是怎么说的") > blob0.rfind(q(N)), "")
chk("6-7 工具面里有 recall_answer(模型才可能想起来用它)",
    "recall_answer" in calls[0]["tool_names"], calls[0]["tool_names"])
blob1 = json.dumps(calls[1]["messages"], ensure_ascii=False)
chk("6-8 第二轮里出现了**那一题的原话全文**(挤出区的题也没丢)",
    ans(deep) in blob1, blob1[-300:])
chk("6-9 回捞结果带「旧引用号本轮未必成立」的提醒", "别照抄" in blob1, "")
chk("6-10 收口是干净结论行", "结论: 回捞到了" in (res.get("answer") or ""), res.get("answer"))

# 会话长一轮以后, 早前那题仍在(不因新记录被挤掉), 且纪要跟着长一轮
after = flow.records("scan:B")
chk("6-11 这一轮自己也落了流水", len(after) == N + 1, len(after))
chk("6-12 长了一轮之后, 被挤出区那题按题号仍取得到",
    registry.call({"scope": "scan:B"}, "recall_answer", {"index": deep})["items"][0]["question"] == q(deep), "")

print()
print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !!", f)
sys.exit(1 if fails else 0)
