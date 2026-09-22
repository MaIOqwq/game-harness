# -*- coding: utf-8 -*-
"""护栏硬顶自检(离线, 不烧模型不打网): python _syscheck/verify_guardrail.py

只查一件事: **真要发出去的那一份**超上限时, 引擎会不会把它压回线下, 且不把 tool 消息弄丢
(弄丢就把整轮请求变成非法 400)。顺带核上限本身是按所配窗口折算的。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from harness.engine import _est_input_tokens, _in_ceiling, _shrink_to_ceiling

total = ok = 0
fails = []


def chk(name, cond, got=""):
    global total, ok
    total += 1
    print(("PASS " if cond else "FAIL ") + name)
    if cond:
        ok += 1
    else:
        fails.append("%s -> %r" % (name, got))


def fake_msgs(n_tool, chars_each):
    """一条 assistant(带 n 个 tool_calls) + n 条对应的 tool 结果。"""
    msgs = [{"role": "system", "content": "系统词" * 50},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c%d" % i} for i in range(n_tool)]}]
    for i in range(n_tool):
        msgs.append({"role": "tool", "tool_call_id": "c%d" % i, "content": "证" * chars_each})
    return msgs


CEIL = 20000

# ① 没超 -> 一个字节都不动
m = fake_msgs(2, 100)
before = [x.get("content") for x in m]
chk("没超上限时不动它", _shrink_to_ceiling(m, CEIL) == [])
chk("没超上限时内容原样", [x.get("content") for x in m] == before)

# ② 超了 -> 压到线下, 且 tool 消息条数不变(配对不许断)
m = fake_msgs(6, 20000)
chk("构造的样本确实超了", _est_input_tokens(m) > CEIL * 0.9, _est_input_tokens(m))
cuts = _shrink_to_ceiling(m, CEIL)
after_est = _est_input_tokens(m)
chk("超了会动手", len(cuts) > 0, cuts)
chk("压完落到线下", after_est <= CEIL * 0.9, after_est)
chk("tool 消息一条不丢", sum(1 for x in m if x.get("role") == "tool") == 6)
chk("tool_call_id 一一对应没断",
    [x["tool_call_id"] for x in m if x.get("role") == "tool"] == ["c%d" % i for i in range(6)])
chk("从最早的压起(最后那条仍是长文)",
    len(m[-1]["content"]) > 3000, len(m[-1]["content"]))

# ③ 只留一条超长 tool, 压不动也得给个占位(不许整条删)
m = fake_msgs(1, 200000)
cuts = _shrink_to_ceiling(m, 3000)
# 原先写成 `... <= 3000 * 0.9 if False else len(cuts) > 0`: 那个 `if False` 把前半句永远丢掉,
# 于是这条只验了"动过手", 而**压完到底落没落到线下**从来没验过 —— 恰恰是这条用例的名字要说的。
# 修成真断言: 确实压过(cuts 非空) **且**压完估算用量真落到线下, 两件一起判。
chk("单条超长也压得下来",
    len(cuts) > 0 and _est_input_tokens(m) <= 3000 * 0.9,
    (_est_input_tokens(m), cuts))
chk("压完仍是一条 tool 消息", sum(1 for x in m if x.get("role") == "tool") == 1)

# ④ 上限按所配窗口折算(1M 窗口 -> 快速 16 万 / 精准 32 万)
os.environ.pop("LLM_WINDOW", None)
chk("1M 窗口 -> 快速 160000", _in_ceiling("快速") == 160000, _in_ceiling("快速"))
chk("1M 窗口 -> 精准 320000", _in_ceiling("精准") == 320000, _in_ceiling("精准"))
os.environ["LLM_WINDOW"] = "32000"
# 32k*16% = 5120, 但下限是 8000 —— 小窗口走"等比例收紧"时不许紧到没法作答
chk("32k 窗口 -> 快速收紧但守下限", _in_ceiling("快速") == 8000, _in_ceiling("快速"))
chk("32k 窗口 -> 精准等比例收紧", _in_ceiling("精准") == 10240, _in_ceiling("精准"))
os.environ["LLM_WINDOW"] = "1000"      # 太小 -> 兜底 8000 而非 160/320
chk("窗口填得过小有兜底", _in_ceiling("快速") == 8000, _in_ceiling("快速"))
os.environ.pop("LLM_WINDOW", None)

print("-" * 56)
print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  FAIL:", f)
sys.exit(1 if fails else 0)
