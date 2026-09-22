#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token 消耗自检: 一条回答到底吃掉了多少 token, 这个数是怎么攒出来的。纯离线, 不触网、不碰产品库。

为什么这支值得存在: 消耗数**错了不会报错**。它只是安静地显示一个看着挺像的数 —— 少记一炮、
把已经拆开的命中/未命中又加一遍、服务商没回用量时印一个 0 出来。用户拿账去对, 才对不上,
那时线索早没了。所以这几条必须钉死:

  ① 三档拆分的规矩: 有拆分就分开记; 只给 prompt_tokens 时整份算未命中(多算不多报);
    命中显式为 0 时**不重复计** prompt_tokens(同一份输入算两遍是最容易犯的错);
  ② 每一炮都记账: _llm 的两条路(非流式/流式)都要进账 —— 收口那次重写也在这儿;
  ③ 服务商不回 usage: 记 0 不猜、不抛, 也不把整题搞挂;
  ④ 累计: 边爬边给的那个"到这一刻花了多少"要跟最终数对得上;
  ⑤ 落盘: 流水里落三档**真数**与模型名, 且 prompt_tokens == 命中 + 未命中(不能自相矛盾);
  ⑥ 出得来: 网页卡片 / 实时画面 / PDF 上真带着这个数(源码级接线, 免得后端算了前端不显示);
  ⑦ 页面上**不许再出现钱**: 这一版明确要的是 token 消耗, 不是金额(也不许留价格表之类的遗迹)。

用法: python _syscheck/verify_tokens.py   退出码 0=全过, 1=有失败。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from harness import engine, pdfout                                            # noqa: E402
# webview 不进 import: 它只被 open() 当源码文本读(见下面 6.x 段的源码级接线断言), 导入没用上。

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


# ---------- 假响应体 / 假流 ----------
class _FakeResp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _usage(**kw):
    body = {"choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}]}
    if kw:
        body["usage"] = kw
    return body


def _engine():
    """不跑 __init__(那要 DB): _llm/_count/_tk_total 只碰下面这几个属性。"""
    e = engine.Engine.__new__(engine.Engine)
    e._tools, e._on_event, e._cancel = None, None, None
    e._tk_hit = e._tk_miss = e._tk_out = 0
    e._tk_model = ""
    e.scope = "syscheck_tokens"
    return e


_real_urlopen = engine.urllib.request.urlopen
_real_complete = engine.llmstream.complete


def _patch(body):
    engine.urllib.request.urlopen = lambda req, timeout=None: _FakeResp(
        json.dumps(body, ensure_ascii=False).encode("utf-8"))
    engine.llmstream.complete = lambda url, key, b, on_delta=None: body


# ---------- ① 三档拆分的规矩 ----------
e = _engine()
_patch(_usage(prompt_tokens=300000, prompt_cache_hit_tokens=100000,
              prompt_cache_miss_tokens=200000, completion_tokens=5000))
try:
    e._llm([], tools=False)
    chk("①-1 给了缓存拆分就分开记(命中/未命中/输出三档各归各位)",
        (e._tk_hit, e._tk_miss, e._tk_out) == (100000, 200000, 5000),
        (e._tk_hit, e._tk_miss, e._tk_out))

    e._tk_hit = e._tk_miss = e._tk_out = 0
    _patch(_usage(prompt_tokens=100000, completion_tokens=1000))
    e._llm([], tools=False)
    chk("①-2 只给 prompt_tokens(没拆分)时整份算未命中 —— 多算不多报",
        (e._tk_hit, e._tk_miss) == (0, 100000), (e._tk_hit, e._tk_miss))

    e._tk_hit = e._tk_miss = e._tk_out = 0
    _patch(_usage(prompt_tokens=50000, prompt_cache_hit_tokens=0,
                  prompt_cache_miss_tokens=50000, completion_tokens=0))
    e._llm([], tools=False)
    chk("①-3 命中显式为 0 时**不重复计** prompt_tokens(同一份输入算两遍是最容易犯的错)",
        (e._tk_hit, e._tk_miss) == (0, 50000), (e._tk_hit, e._tk_miss))

    e._tk_hit = e._tk_miss = e._tk_out = 0
    _patch(_usage(prompt_tokens=12345, prompt_cache_hit_tokens=12345,
                  prompt_cache_miss_tokens=0, completion_tokens=7))
    e._llm([], tools=False)
    chk("①-4 全命中也不重复加(命中 12345 就该是 12345)",
        (e._tk_hit, e._tk_miss, e._tk_out) == (12345, 0, 7), (e._tk_hit, e._tk_miss, e._tk_out))

    # ---------- ② 每一炮都记(含流式那条路) ----------
    e._tk_hit = e._tk_miss = e._tk_out = 0
    _patch(_usage(prompt_tokens=300000, prompt_cache_hit_tokens=100000,
                  prompt_cache_miss_tokens=200000, completion_tokens=5000))
    e._llm([], tools=False)
    mid = (e._tk_hit, e._tk_miss, e._tk_out)
    e._llm([], tools=False, stream=True)
    chk("②-1 流式那一炮也进账(两条路都得记, 只记一条等于漏一半)",
        (e._tk_hit, e._tk_miss, e._tk_out) == (mid[0] * 2, mid[1] * 2, mid[2] * 2),
        (e._tk_hit, e._tk_miss, e._tk_out))
    from harness.settings import llm_cfg
    chk("②-2 模型名记的是这次真发出去用的那个(设置页换过模型也认得出)",
        e._tk_model == llm_cfg()[1] and bool(e._tk_model), (e._tk_model, llm_cfg()[1]))

    # ---------- ③ 服务商不回 usage ----------
    snap = (e._tk_hit, e._tk_miss, e._tk_out)
    _patch(_usage())                       # 没有 usage 这个键
    e._llm([], tools=False)
    e._llm([], tools=False, stream=True)
    chk("③-1 服务商没回 usage: 那一炮记 0, 不猜也不报错",
        (e._tk_hit, e._tk_miss, e._tk_out) == snap, (e._tk_hit, e._tk_miss, e._tk_out))

    # ---------- ④ 累计 ----------
    e._tk_hit, e._tk_miss, e._tk_out = 100000, 200000, 5000
    t = e._tk_total()
    chk("④-1 累计给的是输入合计(命中+未命中)与输出, 与最终流水口径一致",
        t == {"prompt_tokens": 300000, "completion_tokens": 5000}, t)
finally:
    engine.urllib.request.urlopen = _real_urlopen
    engine.llmstream.complete = _real_complete

# ---------- ⑤ 落盘 ----------
got = []
_real_append = engine.flow.append
engine.flow.append = lambda scope, rec: got.append(rec)
try:
    e._tk_hit, e._tk_miss, e._tk_out, e._tk_model = 100000, 200000, 5000, "deepseek-chat"
    e._emit_flow("快速", "这题问啥", "崩铁", "结论: x", "cites", [], 100000, 200000, 5000, 2,
                 160000, est=123, t0=1758000000.0)
finally:
    engine.flow.append = _real_append
rec = got[0] if got else {}
u = rec.get("usage") or {}
chk("⑤-1 流水落三档真数与模型名",
    u.get("cache_hit_tokens") == 100000 and u.get("cache_miss_tokens") == 200000
    and u.get("completion_tokens") == 5000 and u.get("model") == "deepseek-chat", u)
chk("⑤-2 输入合计与两档之和对得上(流水不许自相矛盾)",
    u.get("prompt_tokens") == (u.get("cache_hit_tokens") or 0) + (u.get("cache_miss_tokens") or 0),
    u)
chk("⑤-3 本地估算单列(est_input_tokens), 不混进服务商口径",
    u.get("est_input_tokens") == 123, u.get("est_input_tokens"))

# ---------- ⑥ 出得来 ----------
_ask = {
    "id": "aX", "question": "崩铁最近怎么样", "mode": "快速", "game": "崩铁",
    "answer": "结论: 还行", "cites": [], "uncited": [], "cite_check": {},
    "stats": {"total": 3, "by_platform": {"NGA": 3},
              "usage": {"prompt_tokens": 300000, "completion_tokens": 5000,
                        "cache_hit_tokens": 100000, "cache_miss_tokens": 200000,
                        "model": "deepseek-chat"},
              "ceiling": 160000},
    "measure": "keyword", "ts": "2026-09-16T10:00:00", "cancelled": False,
}
_txt = pdfout.text_of(_ask)
chk("⑥-1 PDF 上印着这个数, 且带三档构成与模型(纸要能说清吃了多少)",
    "token 消耗 输入 300000" in _txt and "缓存命中 100000" in _txt
    and "输出 5000" in _txt and "deepseek-chat" in _txt, "")
_nousage = {"id": "aY", "question": "q", "answer": "a", "cites": [], "cite_check": {},
            "stats": {"by_platform": {}, "usage": {}}, "measure": "keyword", "ts": "t"}
chk("⑥-2 PDF 遇到没有用量记录: 如实说没记到, 不印 0",
    "未记录" in pdfout.text_of(_nousage), "")

_js = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
chk("⑥-3 网页源码里挂着 token 徽标, 且真被每条回答调用",
    "function tokenBadge(" in _js and "tokenBadge(ask)" in _js, "")
chk("⑥-4 网页读的是 stats.usage(后端算了前端不显示 = 白算)",
    "ask.stats||{}).usage" in _js, "")
chk("⑥-5 实时画面里有累计消耗(还没收口就知道吃了多少)",
    "live.usage.total" in _js, "")
chk("⑥-6 后端翻译层原样把 usage 带出去(含三档与模型名)",
    '"usage": usage' in open(os.path.join(ROOT, "harness", "webview.py"),
                             encoding="utf-8").read(), "")

# ---------- ⑦ 页面上不许再出现钱 ----------
# 这一版明确要的是 token 消耗、不是金额。留着半套计价最坏: 一会儿显示一会儿不显示,
# 或者价格表还在那儿悄悄生效 —— 所以连"遗迹"一起钉住。
_leak = []
for rel in (os.path.join("web", "index.html"), os.path.join("harness", "pdfout.py"),
            os.path.join("harness", "webview.py"), os.path.join("harness", "engine.py"),
            os.path.join("harness", "test.py"), "config.example.bat"):
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        continue
    blob = open(p, encoding="utf-8", errors="replace").read()
    for bad in ("计费", "¥", "价目", "元/百万", "HARNESS_PRICING", "pricing"):
        if bad in blob:
            _leak.append("%s: %s" % (rel, bad))
chk("⑦-1 全套源码里没有残留的金额/计价痕迹", not _leak, _leak)
chk("⑦-2 计价模块已删掉(不留一个没人用的价目表)", not os.path.exists(
    os.path.join(ROOT, "harness", "pricing.py")), "")

print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !!", f)
sys.exit(1 if fails else 0)
