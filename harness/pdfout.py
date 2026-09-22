# -*- coding: utf-8 -*-
"""把一题的答案导出成 PDF —— 逐块排版, 不起浏览器、不借系统字体。

**为什么不再走浏览器**（原先这条路是把 HTML 交给 Chrome 打印）:
  1. 一台干净的 Linux 服务器上没有任何中文字体（`fc-list :lang=zh` 是空的, 只有 DejaVu/Liberation）。
     Chrome 于是拿 Helvetica 排中文, 导出的那份 PDF 满纸方框 —— 用户下载到的是一份看不懂的纸。
     这不是 Chrome 的错, 谁排都一样: **包里不带字体, 到了没装中文的机器上就是方框**;
  2. 每导一次起一个浏览器进程, 又慢又重, 还多一个"启不起来"的失败面（09-19 那次就是）。

所以改成自己排: 字体随包带（`harness/assets/`, Noto Sans SC 子集, OFL-1.1 可分发）,
用 fpdf2 直接画字。没有字体依赖、没有浏览器依赖, 一份 PDF 几百毫秒出来。

版式仍与网页那边对齐 —— 标题/列表/表格/粗体/引用徽标认同一套标记（改规则时两边一起改）。

内容与画法是分开的: `build_blocks()` 只说"要印哪些块", `_draw()` 只管画。
自检脚本断言内容时不必装 PDF 库, 也不必真出一份 PDF。
"""
import os
import re
import threading

from fpdf import FPDF
from fpdf.enums import XPos, YPos

_HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(_HERE, "assets")
OUT_DIR = os.path.join(os.path.dirname(_HERE), "out", "pdf")
REGULAR = os.path.join(ASSETS, "NotoSansSC-Regular.ttf")
BOLD = os.path.join(ASSETS, "NotoSansSC-Bold.ttf")

_CITE_RE = re.compile(r"\[\s*id\s*=\s*([^\]]+?)\s*\]")
# 括号里按这组分隔符切成一个个 token, 只认「纯数字」和「数字-数字」两种。
# 认不出的（模型偶尔会在里头夹一条 `like=530` 之类的自注）就丢掉 —— 那是个数, 不是一条引用,
# 印成徽标会让人以为清单里该有它。网页那侧 parseIds 也是这么剔的, 两边同一个口径。
_CITE_TOK = re.compile(r"[\s,，、;；/]+")
_CITE_RANGE = re.compile(r"^(\d+)\s*[-–~]\s*(\d+)$")
_MD_HEAD = re.compile(r"^(#{1,3})\s+(.*)$")
_MD_LI = re.compile(r"^[-*]\s+(.*)$")
# 表格分隔行 |---|---|(对齐可写 :---: / ---:): 与网页 md() 认同一套, 两处别各认各的
_MD_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# 分节线: 模型常在段间写一行 ---, 规则同网页
_MD_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
# 中文表意文字/全角/中文标点: 这些逐字断行, 拉丁单词按空格断
_CJK = re.compile(r"[⺀-鿿豈-﫿＀-￯　-〿]")
_LOCK = threading.Lock()

_SENTI = {0: "负", 1: "中", 2: "正"}

# 页脚那句口径声明。纸是要被转发的, 这句必须跟着走 —— 自检也钉着它。
FOOT_NOTE = ("本页由本地作答工具生成。答案为社区当下口径、非官方权威；"
             "［id=N］ 都能在上面引用清单里对回原帖。")

# 颜色与字号: 跟着网页那份 CSS 的口径走(网页 14px 正文 = 纸上 10.5pt)
INK = (28, 36, 52)
MUTED = (102, 112, 133)
FAINT = (152, 162, 179)
LINE = (228, 231, 236)
SOFT = (248, 250, 252)
WARN_BG = (255, 246, 229)
WARN_BD = (245, 201, 138)
WARN_FG = (138, 90, 0)
BADGE = (29, 78, 216)

_SIZE_BODY = 10.5
_SIZE_TITLE = 15.0
_SIZE_META = 8.0
_SIZE_H = {1: 13.0, 2: 11.5, 3: 10.5}
_SIZE_SECT = 11.0
_SIZE_CITE = 9.5
_SIZE_SUB = 9.0
_SIZE_URL = 7.5
_SIZE_WARN = 9.5


def _safe(sid):
    return re.sub(r"[^\w.-]", "_", str(sid or "ask"))[:60]


def path_for(ask_id):
    return os.path.join(OUT_DIR, _safe(ask_id) + ".pdf")


def exists(ask_id):
    return os.path.isfile(path_for(ask_id))


def conv_key(session):
    """整段对话那份 PDF 的文件名 —— 按会话号, 不按题目 id(一份装一整个对话)。"""
    return "conv_%s" % _safe(session)


# ---------- 行内标记 -> 可排的片段 ----------
# 一段文字先切成"原子": 一个汉字/一个空格/一个拉丁词 各算一个。
# 折行、量宽、换字体都按原子来 —— 中文逐字可用空间更满、拉丁词不会被拦腰截断。
def _inline_runs(s):
    """`一段话` -> [{text, b, cite}]。`**粗**` 与 `[id=N]` 是唯一两种行内标记。"""
    runs, pos = [], 0
    for m in re.finditer(r"\*\*(.+?)\*\*|\[\s*id\s*=\s*([^\]]+?)\s*\]", s):
        if m.start() > pos:
            runs.append({"text": s[pos:m.start()]})
        if m.group(2) is not None:
            for t in _CITE_TOK.split(m.group(2).strip()):
                if not t:
                    continue
                if t.isdigit() or _CITE_RANGE.match(t):
                    runs.append({"text": "［%s］" % t, "cite": True})
        else:
            runs.append({"text": m.group(1), "b": True})
        pos = m.end()
    if pos < len(s):
        runs.append({"text": s[pos:]})
    return runs or [{"text": ""}]


_COVER = None


def _covered():
    """包里这两份字画得出哪些码位。社区原文里有大量表情符号（🐴🐮🕊 之类）, 那不在子集里 ——
    不拦的话 fpdf 会在纸上留个空、还逐字刷日志。剔掉它, 正文照排, 只是那几个表情不出现在纸上。
    （字体覆盖范围直接问字体本身, 不另维护一张表 —— 表会跟字体脱节。）"""
    global _COVER
    if _COVER is None:
        from fontTools.ttLib import TTFont
        s = set()
        for p in (REGULAR, BOLD):
            try:
                for t in TTFont(p)["cmap"].tables:
                    s.update(t.cmap.keys())
            except Exception:
                pass
        _COVER = s or {0x20}      # 真读不出来时别把整篇剔空
    return _COVER


def _atoms(runs):
    """片段 -> 原子。样式跟着原子走, 折行后不用回头找。"""
    cover = _covered()
    out = []
    for r in runs:
        b, cite = bool(r.get("b")), bool(r.get("cite"))
        buf = ""
        for ch in r.get("text") or "":
            if ord(ch) not in cover:
                continue
            if ch == "\n":
                # 换行是个"断行"指令, 不是字。当成字排会在纸上印出一个空框。
                if buf:
                    out.append({"text": buf, "b": b, "cite": cite})
                    buf = ""
                out.append({"text": "", "nl": True})
            elif _CJK.match(ch) or ch == " ":
                if buf:
                    out.append({"text": buf, "b": b, "cite": cite})
                    buf = ""
                out.append({"text": ch, "b": b, "cite": cite})
            else:
                buf += ch
        if buf:
            out.append({"text": buf, "b": b, "cite": cite})
    return out


def _cells(ln):
    """`| 维度 | A | B |` -> ['维度','A','B']。首尾那两根竖线不算格。"""
    return [c.strip() for c in ln.strip().strip("|").split("|")]


def _md_blocks(src):
    """答案正文的小渲染器 —— 规则与网页那个 md() 同一套(标题/短横列表/表格/粗体/引用徽标)。

    不渲染的话, 答案里的 `##` `**` `- ` 会原样印进 PDF, 而同一个答案在网页上是排好的版 ——
    同一份内容两处长相不一样, 用户会以为导出的坏了。
    """
    out, ul = [], False
    lines = (src or "").splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].rstrip()
        if not s.strip():
            if ul:
                out.append({"t": "endlist"})
                ul = False
            i += 1
            continue
        if s.lstrip().startswith("|") and i + 1 < len(lines) and _MD_SEP.match(lines[i + 1]):
            if ul:
                out.append({"t": "endlist"})
                ul = False
            head, rows = _cells(s), []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            out.append({"t": "table", "head": head, "rows": rows})
            continue
        m = _MD_HEAD.match(s)
        if m:
            if ul:
                out.append({"t": "endlist"})
                ul = False
            out.append({"t": "h", "lv": len(m.group(1)), "runs": _inline_runs(m.group(2))})
            i += 1
            continue
        if _MD_HR.match(s):
            if ul:
                out.append({"t": "endlist"})
                ul = False
            out.append({"t": "hr"})
            i += 1
            continue
        m = _MD_LI.match(s)
        if m:
            out.append({"t": "li", "runs": _inline_runs(m.group(1))})
            ul = True
            i += 1
            continue
        if ul:
            out.append({"t": "endlist"})
            ul = False
        out.append({"t": "p", "runs": _inline_runs(s)})
        i += 1
    if ul:
        out.append({"t": "endlist"})
    return out


# ---------- 内容模型(不装 PDF 库也能断言) ----------
def _token_line(stats):
    """这条回答的 token 消耗 —— 纸上也要有(数来自服务商逐炮回的 usage, 引擎累加, 见 engine._count)。

    三档一并印出来(输入里的缓存命中/未命中 + 输出): 命中的那部分往往是输入的大头,
    只印一个"输入 N"看不出这份上下文里有多少是复用的。"""
    u = (stats or {}).get("usage") or {}
    if u.get("prompt_tokens") is None and u.get("completion_tokens") is None:
        return "token 消耗 未记录（服务商这一题没有回用量）"
    split = ("服务商没给缓存拆分，整份按未命中计"
             if (u.get("cache_hit_tokens") is None and u.get("cache_miss_tokens") is None)
             else "其中缓存命中 %s / 未命中 %s" % (u.get("cache_hit_tokens") or 0,
                                                 u.get("cache_miss_tokens") or 0))
    return ("token 消耗 输入 %s（%s）· 输出 %s —— 本题发给模型的所有炮合计（含收口那次重写）；模型 %s"
            % (u.get("prompt_tokens") or 0, split, u.get("completion_tokens") or 0,
               u.get("model") or "未知"))


def _warns(ask):
    """黄条: 度量降级 / 中途停止 / 引用核不上 —— 三件事都得在纸上如实说。"""
    out = []
    if ask.get("measure") == "keyword":
        out.append({"t": "warn", "text":
                    "度量：关键词降级。情感/反讽是词表启发式、不是模型 —— 本机没装情感分析服务"
                    "（那要另下模型权重，很重；不装照样出答案）。"})
    if ask.get("cancelled"):
        # 点过「停止」的题: PDF 是能下载、能转发的纸, 不标出来就会被当成跑完全程的答卷。
        out.append({"t": "warn", "text":
                    "这一题提问者中途点了「停止」：答案只用当时已经取到的证据，原定的取数范围没跑完。"})
    missing = (ask.get("cite_check") or {}).get("missing") or []
    if missing:
        out.append({"t": "warn", "text":
                    "本轮有 %d 处引用没能在存档里找到（%s）—— 看答案时留意。"
                    % (len(missing), "、".join(str(x) for x in missing[:20]))})
    return out


def build_blocks(ask):
    """一条 ask -> 要印的块序列。单题 PDF 与整段对话 PDF 共用这一份。"""
    esc = lambda v: str(v) if v is not None else ""
    q = ask.get("question") or ""
    stats = ask.get("stats") or {}
    by_plat = stats.get("by_platform") or {}
    blocks = [{"t": "title", "text": q}]
    meta = [ask.get("mode") or "", ask.get("game") or "未指定游戏",
            "样本 %s 条" % esc(stats.get("total", 0)),
            " · ".join("%s %s 条" % (k, v) for k, v in sorted(by_plat.items()))]
    blocks.append({"t": "meta", "text": "　".join(x for x in meta if x)})
    blocks.append({"t": "meta", "text": _token_line(stats)})

    blocks += _warns(ask)
    blocks += _md_blocks(ask.get("answer") or "")
    blocks += [{"t": "gap"}]

    cites = ask.get("cites") or []
    # 正文里的 ［N］ 得在这份 PDF 里对得回去, 所以清单必须真排进去 ——
    # 页脚那句「对回上面的引用清单」指着它。
    if cites:
        blocks.append({"t": "sect", "text": "引用清单（%d 条）—— 正文里的 ［id=N］ 按这个号对" % len(cites)})
        for c in cites:
            blocks.append({
                "t": "cite", "id": c.get("id"), "title": c.get("title") or "(无正文)",
                "platform": c.get("platform") or "",
                "senti": _SENTI.get(c.get("senti"), "—") if c.get("senti") is not None else "—",
                "text": (c.get("text") or "").strip(),
                # 字幕全文: 只有取过字幕的那几条视频有。它是这一题最长的单块内容, 但正文里引它的
                # 那些话就靠它才对得回出处 —— 纸上少这一块, 用户就没法核那几个判断是从哪一段来的。
                "sub": (c.get("sub") or "").strip(),
                "url": c.get("url") or "",
            })
    return blocks


def _block_text(b):
    """一个块会印出来的字(拼接) —— 只给自检看, 排版时不用。"""
    t = b["t"]
    if t == "table":
        return "\n".join(list(b["head"]) + [c for r in b["rows"] for c in r])
    if t == "cite":
        return "\n".join(str(b.get(k) or "") for k in
                         ("id", "title", "platform", "senti", "text", "sub", "url"))
    if "runs" in b:
        return _plain(b["runs"])
    return str(b.get("text") or "")


def text_of(ask):
    """一条 ask 在纸上会出现的全部文字。自检据此断言内容 —— 不必装 PDF 库、不必真出一份纸。"""
    return "\n".join(_block_text(b) for b in build_blocks(ask))


def build_conv_blocks(title, asks):
    """整段对话: 标题 + 逐题排下去, 每题仍是**同一份**块序列(两处长相必须一致,
    不然用户会以为导出的那份坏了)。"""
    blocks = [{"t": "title", "text": title or "对话"},
              {"t": "meta", "text": "共 %d 题　最近一题 %s"
               % (len(asks), str(asks[-1].get("ts") or "")[:16].replace("T", " "))}]
    for i, a in enumerate(asks, 1):
        blocks.append({"t": "qhead", "n": i, "text": a.get("question") or "",
                       "first": i == 1})
        blocks += build_blocks(a)
    return blocks


# ---------- 画 ----------
class _Doc(FPDF):
    def __init__(self, foot_ts):
        super().__init__(format="A4", unit="mm")
        self.foot_ts = str(foot_ts or "")[:16].replace("T", " ")
        # 字体必须在开页之前登记好
        self.add_font("sc", "", REGULAR)
        self.add_font("sc", "B", BOLD)
        self.set_margins(14, 15, 14)
        self.set_auto_page_break(True, 18)
        self.alias_nb_pages()

    def footer(self):
        """页脚: 口径声明 + 导出时间 + 页码。纸是要被转发的, 这句必须跟着走。"""
        self.set_auto_page_break(False)
        top = self.h - 14
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        self.line(self.l_margin, top, self.w - self.r_margin, top)
        self.set_y(top + 1.6)
        self.set_font("sc", "", 7.5)
        self.set_text_color(*FAINT)
        self.set_x(self.l_margin)
        self.cell(150, 3.6, FOOT_NOTE, align="L")
        self.set_x(self.w - self.r_margin - 32)
        self.cell(32, 3.6, "导出 %s　第 %s / {nb} 页" % (self.foot_ts, self.page_no()), align="R")
        self.set_auto_page_break(True, 18)


def _font(pdf, b, size):
    pdf.set_font("sc", "B" if b else "", size)


def _wrap(pdf, atoms, size, w):
    """原子折行。中文逐字可用空间更满、拉丁词不被拦腰截断; 比整行还宽的词(长 URL)才硬断。"""
    flat = []
    for a in atoms:
        pdf.set_font("sc", "B" if a.get("b") else "", size)
        if a.get("nl") or pdf.get_string_width(a["text"]) <= w:
            flat.append(a)
        else:
            flat.extend({"text": ch, "b": a.get("b"), "cite": a.get("cite")} for ch in a["text"])
    lines, cur, cw = [], [], 0.0
    for a in flat:
        if a.get("nl"):
            lines.append(cur)
            cur, cw = [], 0.0
            continue
        pdf.set_font("sc", "B" if a.get("b") else "", size)
        tw = pdf.get_string_width(a["text"])
        if cw + tw > w and cur:
            lines.append(cur)
            cur, cw = [], 0.0
            if not a["text"].strip():       # 行首不吃空格
                continue
        cur.append(a)
        cw += tw
    if cur:
        lines.append(cur)
    return lines or [[]]


def _flow(pdf, runs, size=_SIZE_BODY, lh=5.6, color=INK, x=None, w=None, hang=0.0, keep=0.0):
    """把一段(可带样式的)文字排出来, 自动换行、自动翻页。返回用掉的高度。"""
    x = pdf.l_margin if x is None else x
    w = (pdf.w - pdf.r_margin - x) if w is None else w
    atoms = _atoms(runs) if isinstance(runs, list) else _atoms(_inline_runs(runs))
    lines = _wrap(pdf, atoms, size, w - hang)
    if keep and pdf.get_y() + lh * len(lines) > pdf.page_break_trigger:
        pdf.add_page()
    y = pdf.get_y()
    for i, ln in enumerate(lines):
        if y + lh > pdf.page_break_trigger:
            pdf.add_page()
            y = pdf.get_y()
        pdf.set_xy(x + (hang if i else 0.0), y)
        for a in ln:
            if not a["text"]:       # 空原子不许进 cell: fpdf 把 w=0 当成"顶到右边距"
                continue
            # 逐原子 cell: 折行已经自己算过了, 这里要的是"放到哪就是哪", 不许 fpdf 再折一次
            _font(pdf, a.get("b"), size)
            pdf.set_text_color(*(BADGE if a.get("cite") else color))
            pdf.cell(pdf.get_string_width(a["text"]), lh, a["text"],
                     new_x=XPos.RIGHT, new_y=YPos.TOP)
        y += lh
    pdf.set_y(y)
    return y


def _panel(pdf, text, size=_SIZE_WARN, lh=4.8, fg=WARN_FG, bg=WARN_BG, bd=WARN_BD, pad=2.4):
    """一块带底色的提示条(黄条 / 字幕全文那种灰块都用它)。"""
    w = pdf.w - pdf.l_margin - pdf.r_margin
    lines = _wrap(pdf, _atoms(_inline_runs(text)), size, w - 2 * pad) if text else [[]]
    h = lh * len(lines) + 2 * pad
    if pdf.get_y() + h > pdf.page_break_trigger:
        pdf.add_page()
    x, y = pdf.l_margin, pdf.get_y()
    pdf.set_fill_color(*bg)
    pdf.set_draw_color(*bd)
    pdf.set_line_width(0.2)
    pdf.rect(x, y, w, h, style="DF", round_corners=False)
    pdf.set_xy(x + pad, y + pad)
    for ln in lines:
        for a in ln:
            if not a["text"]:
                continue
            _font(pdf, a.get("b"), size)
            pdf.set_text_color(*(BADGE if a.get("cite") else fg))
            pdf.cell(pdf.get_string_width(a["text"]), lh, a["text"],
                     new_x=XPos.RIGHT, new_y=YPos.TOP)
        pdf.set_xy(x + pad, pdf.get_y() + lh)
    pdf.set_y(y + h + 2.0)
    return h


def _table(pdf, head, rows):
    """对照表。模型答"两边权衡"这类题爱用 | 维度 | A | B | —— 不排成表格就是一堆竖线。"""
    w = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.set_font("sc", "B", _SIZE_CITE)
    widths = [pdf.get_string_width(c) for c in head]
    pdf.set_font("sc", "", _SIZE_CITE)
    for r in rows:
        for i, c in enumerate(r[:len(head)]):
            widths[i] = max(widths[i], pdf.get_string_width(re.sub(r"\*\*", "", c)) + 2)
    tot = sum(widths) or 1
    # 列宽按内容占比分, 但每列都留个下限, 免得某一列被压成一个字一行
    rel = [max(1, int(round(x / tot * 100))) for x in widths]
    if pdf.get_y() + 8 > pdf.page_break_trigger:
        pdf.add_page()
    pdf.set_font("sc", "", _SIZE_CITE)
    from fpdf.fonts import FontFace
    with pdf.table(col_widths=tuple(rel), text_align="LEFT", borders_layout="ALL",
                   line_height=4.6, first_row_as_headings=True,
                   headings_style=FontFace(emphasis="BOLD", fill_color=SOFT, color=(52, 64, 84))) as t:
        row = t.row()
        for c in head:
            row.cell(re.sub(r"\*\*", "", c))
        for r in rows:
            row = t.row()
            for c in r:
                row.cell(_plain([{"text": re.sub(r"\*\*", "", c)}]))
    pdf.set_y(pdf.get_y() + 1.5)


def _plain(runs):
    return "".join(a["text"] for a in _atoms(runs))


def _draw(pdf, blocks):
    for k, b in enumerate(blocks):
        t = b["t"]
        if t == "title":
            pdf.set_font("sc", "B", _SIZE_TITLE)
            pdf.set_text_color(*INK)
            _flow(pdf, b["text"], _SIZE_TITLE, 7.4, INK)
            pdf.set_y(pdf.get_y() + 1.0)
        elif t == "meta":
            _flow(pdf, b["text"], _SIZE_META, 4.4, MUTED)
        elif t == "qhead":
            # 一题一节, 从第二题起另起一页(第一题不另起, 免得头一页是半张空白)
            if not b.get("first"):
                pdf.add_page()
            pdf.set_font("sc", "B", _SIZE_SECT)
            _flow(pdf, "第 %d 问　%s" % (b["n"], b["text"]), _SIZE_SECT, 6.2, INK)
            pdf.set_draw_color(*LINE)
            pdf.line(pdf.l_margin, pdf.get_y() + 0.8, pdf.w - pdf.r_margin, pdf.get_y() + 0.8)
            pdf.set_y(pdf.get_y() + 3.0)
        elif t == "warn":
            _panel(pdf, b["text"])
        elif t == "h":
            size = _SIZE_H.get(b.get("lv"), _SIZE_BODY)
            pdf.set_y(pdf.get_y() + 2.2)
            _flow(pdf, b["runs"], size, size * 0.62, INK, keep=size * 0.62)
            pdf.set_y(pdf.get_y() + 1.2)
        elif t == "p":
            _flow(pdf, b["runs"], _SIZE_BODY, 5.6, INK)
            pdf.set_y(pdf.get_y() + 1.4)
        elif t == "li":
            _flow(pdf, [{"text": "· "}] + _atoms(b["runs"]), _SIZE_BODY, 5.6, INK,
                  x=pdf.l_margin + 4, hang=3.5)
            pdf.set_y(pdf.get_y() + 0.6)
        elif t == "endlist":
            pdf.set_y(pdf.get_y() + 1.2)
        elif t == "hr":
            pdf.set_y(pdf.get_y() + 2.0)
            pdf.set_draw_color(*LINE)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.set_y(pdf.get_y() + 3.0)
        elif t == "table":
            _table(pdf, b["head"], b["rows"])
        elif t == "sect":
            pdf.set_y(pdf.get_y() + 3.0)
            _flow(pdf, b["text"], _SIZE_SECT, 6.0, INK, keep=6.0)
            pdf.set_draw_color(*LINE)
            pdf.line(pdf.l_margin, pdf.get_y() + 0.6, pdf.w - pdf.r_margin, pdf.get_y() + 0.6)
            pdf.set_y(pdf.get_y() + 3.0)
        elif t == "cite":
            _cite(pdf, b)
        elif t == "gap":
            pdf.set_y(pdf.get_y() + 1.0)


def _cite(pdf, c):
    """一条引用: 左竖线 + 号/标题/平台/情感 + 正文 + 字幕全文 + 原帖链接。"""
    left = pdf.l_margin
    x = left + 3.5
    w = pdf.w - pdf.r_margin - x
    # 整块尽量不跨页: 高度粗估一下, 放不下就翻页(粗估即可, 宁可早翻一页也不要把块劈开)
    est = 5.0 + 4.6 * (len(_wrap(pdf, _atoms(_inline_runs(c["text"])), _SIZE_CITE, w)) if c["text"] else 0)
    if c.get("sub"):
        est += 6.0 + 4.4 * (len(c["sub"]) // 46 + 1)
    if pdf.get_y() + est > pdf.page_break_trigger:
        pdf.add_page()
    top = pdf.get_y()
    _flow(pdf, [{"text": "［%s］ " % c["id"], "cite": True}, {"text": c["title"], "b": True},
                {"text": "　%s" % c["platform"], "b": False}],
          _SIZE_CITE, 4.6, INK, x=x, w=w)
    pdf.set_y(pdf.get_y() + 0.4)
    _flow(pdf, [{"text": "情感 %s" % c["senti"]}], 8.0, 4.0, MUTED, x=x, w=w)
    if c["text"]:
        _flow(pdf, c["text"], _SIZE_CITE, 4.6, (53, 64, 82), x=x, w=w)
    if c.get("sub"):
        _panel(pdf, "字幕全文（%d 字）\n%s" % (len(c["sub"]), c["sub"]), _SIZE_SUB, 4.3, (53, 64, 82), SOFT, LINE)
    if c.get("url"):
        _flow(pdf, [{"text": c["url"]}], _SIZE_URL, 3.8, FAINT, x=x, w=w)
    # 左边那道竖线: 块有多高就画多长
    bottom = pdf.get_y()
    pdf.set_draw_color(208, 213, 221)
    pdf.set_line_width(0.6)
    pdf.line(left, top, left, max(bottom, top + 4))
    pdf.set_y(bottom + 3.0)


def _render(blocks, out, ts):
    os.makedirs(OUT_DIR, exist_ok=True)
    pdf = _Doc(ts)
    pdf.add_page()
    _draw(pdf, blocks)
    with _LOCK:
        pdf.output(out)
    if not os.path.isfile(out):
        raise RuntimeError("PDF 没生成出来")
    return out


def render(ask):
    """把一条 ask 渲染成 PDF, 返回可下载的 URL。"""
    out = path_for(ask.get("id"))
    _render(build_blocks(ask), out, ask.get("ts"))
    return "/api/pdf?id=%s" % ask.get("id")


def render_conv(session, title, asks):
    """把整个对话渲染成一份 PDF。**不缓存**: 对话会往下长, 每次重导覆盖同一份文件
    (前端拿地址时带一个时间戳, 破掉浏览器那层缓存)。"""
    key = conv_key(session)
    _render(build_conv_blocks(title, asks), path_for(key),
            asks[-1].get("ts") if asks else None)
    return {"key": key, "url": "/api/pdf?id=%s" % key}
