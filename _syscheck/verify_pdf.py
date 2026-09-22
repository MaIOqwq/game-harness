#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 导出的离线自检 —— 看 `build_blocks()` 排出来的内容, 不开浏览器、不装 PDF 库。

为什么不去验真 PDF 的字节: 真 PDF 是压缩流, 断言不了字。而**版式会出问题的地方全在"排了哪些块"里**
—— 上次实测就栽在这儿: 答案正文没走渲染器, `##` `**` `- ` 原样印进了 PDF, 而同一个答案在网页上
是排好的版。所以这里盯的就是「答案在 PDF 里跟网页同款」。

分五头:
  A. 正文渲染 —— 标题/短横列表/粗体都成了真块, 原文那几个标记字符一个都不许漏;
  B. 引用 —— 正文 [id=N] 成徽标, 引用清单带号带链接, 核不上的出黄条;
  C. 度量 —— 情感是词表启发式时如实标「关键词降级」, 是模型时不许瞎标;
  D. 台账与页脚 —— 样本数/平台数/token 三档/页脚口径;
  E. 字体 —— 包里那两份字必须真在(缺了导出会当场炸, 而且是"到用户机器上才炸")。

用法: python _syscheck/verify_pdf.py   退出码 0=全过, 1=有失败。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness import pdfout  # noqa: E402

ok = 0
fails = []


def chk(c, msg):
    global ok
    if c:
        ok += 1
    else:
        fails.append(msg)


def ask(**kw):
    a = {"id": "t1", "question": "这游戏这期水温怎么样？", "mode": "精准", "game": "鸣潮",
         "answer": "", "cites": [], "stats": {"total": 0, "by_platform": {}},
         "cite_check": {"cited": 0, "missing": []}, "ts": "2026-09-16T06:42:40"}
    a.update(kw)
    return a


def cite(i, **kw):
    c = {"id": i, "title": "标题 %s" % i, "platform": "NGA", "senti": 1,
         "text": "正文", "url": "https://ngabbs.com/read.php?tid=%s" % i}
    c.update(kw)
    return c


def blocks_of(a, t=None):
    b = pdfout.build_blocks(a)
    return [x for x in b if t is None or x["t"] == t]


def answer_text(a):
    """只取答案那一段印出来的字 —— 漏标记字符只许漏在正文里, 清单/黄条不算。"""
    out, on = [], False
    for b in pdfout.build_blocks(a):
        if b["t"] in ("h", "p", "li", "table", "hr"):
            on = True
        elif b["t"] in ("sect", "cite") or (b["t"] == "warn" and on):
            break
        if on:
            out.append(pdfout._block_text(b))
    return "\n".join(out)


# ① 正文渲染 —— 与网页那个 md() 同款
A = ask(answer="# 大标题\n\n正文一句。\n\n## 小标题\n\n- 第一条 **重点**\n- 第二条\n\n收口。")
BS = pdfout.build_blocks(A)
B = answer_text(A)
h1 = [b for b in BS if b["t"] == "h" and b.get("lv") == 1]
h2 = [b for b in BS if b["t"] == "h" and b.get("lv") == 2]
chk(h1 and pdfout._block_text(h1[0]).strip() == "大标题", "①1 一级标题成了 h1 块")
chk(h2 and pdfout._block_text(h2[0]).strip() == "小标题", "①2 二级标题成了 h2 块")
lis = [b for b in BS if b["t"] == "li"]
chk(len(lis) == 2, "①3 短横列表成了两条 li")
chk(any(r.get("b") and r["text"] == "重点" for r in lis[0]["runs"]), "①4 粗体成了加粗片段")
chk("##" not in B, "①5 正文里没漏出 ## 原字符")
chk("**" not in B, "①6 正文里没漏出 ** 原字符")
chk("- 第一条" not in B, "①7 正文里没漏出 - 开头原字符")
chk("收口。" in [pdfout._block_text(b).strip() for b in BS if b["t"] == "p"], "①8 普通行成了 p")

# ② 引用徽标 与 引用清单
A = ask(answer="主流判为缓慢膨胀 [id=5834]，也有人反驳 [id=5846,5870]。")
badges = [r["text"] for b in pdfout.build_blocks(A) if b["t"] == "p"
          for r in b["runs"] if r.get("cite")]
chk(badges == ["［5834］", "［5846］", "［5870］"], "②1 三个 [id=N] 都成了徽标: %s" % badges)
# 模型偶尔在 id 括号里夹一条自注（实测有 `[id=49,like=530]`）—— 那是个数不是引用, 印成徽标
# 会让人去清单里找第 530 条。只认纯数字与区间, 其余丢掉；区间写法保留原样(实测 q4 用过)。
A = ask(answer="一条 [id=49,like=530] 一条 [id=12-14] 一条 [id=x]")
badges = [r["text"] for b in pdfout.build_blocks(A) if b["t"] == "p"
          for r in b["runs"] if r.get("cite")]
chk(badges == ["［49］", "［12-14］"], "②1b 括号里的非引用 token 不当徽标: %s" % badges)
A = ask(cites=[cite(5834), cite(5846, title=None, senti=0, platform="bilibili")])
sect = blocks_of(A, "sect")
cs = blocks_of(A, "cite")
chk(sect and "引用清单（2 条）" in sect[0]["text"], "②2 清单标题带条数")
chk([c["id"] for c in cs] == [5834, 5846], "②3 每条清单项带号")
chk(cs[1]["title"] == "(无正文)", "②4 没标题的按 (无正文) 显示")
chk(cs[1]["senti"] == "负" and cs[0]["senti"] == "中", "②5 情感按 0/1/2 -> 负/中/正 翻译")
chk("https://ngabbs.com" in cs[0]["url"], "②6 原帖链接排进清单")
chk(not blocks_of(ask(), "sect"), "②7 没有引用就不排清单")

# ③ 度量如实标注(情感是词表启发式时, 不能让它看着像有模型)
w = blocks_of(ask(measure="keyword"), "warn")
chk(w and "关键词降级" in w[0]["text"], "③1 keyword 时标出「关键词降级」")
for m in ("model", None):
    chk(not blocks_of(ask(measure=m), "warn"), "③2 measure=%r 时不标" % m)

# ④ 引用核不上 与 中途停止 —— 两件事都得在纸上说
w = blocks_of(ask(cite_check={"cited": 1, "missing": ["5999"]}), "warn")
chk(w and "5999" in w[0]["text"], "④1 有引用核不上就出黄条并列出号")
chk(not blocks_of(ask(), "warn"), "④2 全核得上就不出黄条")

# ⑤ 台账 + 页脚
mt = pdfout.text_of(ask(stats={"total": 151, "by_platform": {"NGA": 46, "bilibili": 105}}))
chk("样本 151 条" in mt, "⑤1 样本总数")
chk("NGA 46 条" in mt and "bilibili 105 条" in mt, "⑤2 分平台条数")
chk("非官方权威" in pdfout.FOOT_NOTE and "对回原帖" in pdfout.FOOT_NOTE, "⑤3 页脚那句口径声明")
chk("导出 %s" in open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "harness", "pdfout.py"), encoding="utf-8").read(),
    "⑤4 页脚印着导出时间")

# ⑥ 正文里的尖括号 —— 现在是纯排版, 没有 HTML 这一层, 所以要求是**原样印出来**
raw = "<script>alert(1)</script> 一句"
chk(raw in pdfout.text_of(ask(answer=raw)), "⑥1 尖括号原样印出来, 不会被当成标记吃掉")
chk("<script>" not in answer_text(ask(answer=raw)).replace(raw, ""),
    "⑥2 正文里没有凭空多出来的标记")

# ⑦ 出文件的路径规矩
chk(pdfout.path_for("a/b:c").endswith(".pdf"), "⑦1 文件名收成 .pdf")
chk("/" not in os.path.basename(pdfout.path_for("a/b:c")), "⑦2 id 里的斜杠不许穿到文件名")
chk(pdfout.path_for(None).endswith(".pdf"), "⑦3 id 为空也不炸")

# ⑧ 字体真在包里 —— 少了这两份, 导出会在**没有中文字体的机器上**印出满纸方框
chk(os.path.isfile(pdfout.REGULAR) and os.path.getsize(pdfout.REGULAR) > 100000,
    "⑧1 正文中文字体在包里")
chk(os.path.isfile(pdfout.BOLD) and os.path.getsize(pdfout.BOLD) > 100000,
    "⑧2 加粗中文字体在包里")
chk(os.path.isfile(os.path.join(pdfout.ASSETS, "OFL.txt")), "⑧3 字体许可证随包带")

print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !!", f)
sys.exit(1 if fails else 0)
