# -*- coding: utf-8 -*-
"""答案开头「英文草稿」剔除自检(离线, 不烧模型不打网): python _syscheck/verify_scratch_strip.py

只查一件事: 模型把思考过程用英文粘在答案第一行时, 引擎会不会把它删掉; 以及**不该删的有没有被误删**。
后一半同样重要 —— 这函数动的是用户直接看见的正文, 误伤比漏拦更糟。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from harness.engine import _strip_leading_scratch  # noqa: E402

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


BODY = ("**官方没有就这件事发过公告。**\n\n"
        "- 社区样本里围绕这件事的讨论全是骂造谣方与催官方出手 [id=101]，没有一条提到剧情或数值。\n"
        "- 时间集中在 9/15 至 9/16，NGA 侧样本很少 [id=102]。\n\n"
        "结论: 本轮证据中未见官方公告，结论主要基于 B站 样本。")

# ① 本次实测漏出来的那句(5 题里的第 1 题): 删掉, 正文一字不差
LEAK = "I've hit the crawl budget limit (3 rounds used). Time to answer with what I have."
got, seen = _strip_leading_scratch(LEAK + "\n\n" + BODY)
chk("实测漏句被删", got == BODY, got[:60])
chk("删掉的行回报出来了", seen == [LEAK], seen)

# ② 上一轮那种夹了中文的英文草稿(「…about major 2025 鸣潮 community events.」): 也该删 ——
#    中文只有两个字, 占比远低于门槛, 不能因为夹了俩中文字就放过去
LEAK2 = "I have enough evidence now. Let me compile the answer about major 2025 鸣潮 community events."
got, seen = _strip_leading_scratch(LEAK2 + "\n\n" + BODY)
chk("夹中文的英文草稿也删", got == BODY and seen == [LEAK2], got[:60])

# ③ 多行草稿 + 中间夹空行: 一起删干净
got, seen = _strip_leading_scratch(LEAK + "\n\nI will now write the final answer.\n\n" + BODY)
chk("多行草稿连空行一起删", got == BODY, got[:60])
chk("两行都记下来了", len(seen) == 2, seen)

# ④ 正文本来就是中文开头: 一个字不动
got, seen = _strip_leading_scratch(BODY)
chk("中文开头的正文原样不动", got == BODY and seen == [], got[:40])

# ⑤ 正文**中间**的英文行不许动(只删开头那一段)
mid = BODY + "\nThe above is a summary."
got, seen = _strip_leading_scratch(mid)
chk("正文中间的英文行不动", got == mid and seen == [], got[-40:])

# ⑥ 开头是英文的**专有名词/术语**(不足 3 个词)不许当草稿删
for head in ("Bilibili 视频侧样本", "NGA 主帖", "**Wuthering Waves** 的口碑", "id=4677 这条原话"):
    s = head + "\n" + BODY
    got, seen = _strip_leading_scratch(s)
    chk("开头短英文片段不当草稿删: " + head[:16], got == s and seen == [], got[:40])

# ⑦ 保险一: 整篇都用英文写 -> 删完就空了, 宁可留着也不动它
all_en = ("I have enough evidence now. Let me compile the answer about the game.\n"
          "The community is angry about the fake screenshot incident.\n"
          "Conclusion: the official account has not responded.")
got, seen = _strip_leading_scratch(all_en)
chk("整篇英文不删空(留下的太短就不动)", got == all_en and seen == [], got[:40])

# ⑧ 保险二: 草稿比剩下的正文还长 -> 那不像"开头一句草稿", 不动
body_short = "这题的中文正文。" * 8           # 64 字: 够过"剩下不短于 60 字"那道闸, 但比草稿(85)短
s = LEAK + "\n" + body_short
got, seen = _strip_leading_scratch(s)
chk("草稿比正文还长就不动(第二道保险)", got == s and seen == [], got[:40])

# ⑧b 反过来: 正文比草稿长一点点(刚刚够) —— 该删, 别把闸门卡得过死。
#     这条是上一版"删掉超过全文三成"栽掉的那个真实比例(85 / 235 ≈ 36%): 正常长度的答案必须能删。
body_ok = "这题的中文正文，写得比草稿长一点。" * 6      # 96 字 > 85
s = LEAK + "\n" + body_ok
got, seen = _strip_leading_scratch(s)
chk("正文刚过草稿长度就该删(36% 那种正常答案不算危险)", got == body_ok and seen == [LEAK], got[:40])

# ⑨ 空 / 只有空白: 不炸、原样回
for s in ("", "   \n  \n"):
    got, seen = _strip_leading_scratch(s)
    chk("空正文不炸(%r)" % s, got == s and seen == [], got)

# ⑩ 接线: 收口那步真调了它(改完函数没人调 = 白写), 且删了会留痕
_eng = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "harness", "engine.py"), encoding="utf-8").read()
_seal = re.search(r"def _seal_answer[\s\S]*?\n    def ", _eng)
chk("_seal_answer 里真调了剔除函数",
    _seal is not None and "_strip_leading_scratch(answer)" in _seal.group(0), "")
chk("剔除了会记一条流水(trace)", "scratch_drop" in (_seal.group(0) if _seal else ""), "")

print("-" * 56)
print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  FAIL:", f)
sys.exit(1 if fails else 0)
