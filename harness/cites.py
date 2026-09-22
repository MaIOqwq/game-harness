# -*- coding: utf-8 -*-
"""多形态引用解析: [id=N] / [id=a, b, c] / [id=a-b] / 混写 [id=a, c-e]。

背景: 原先三处(engine._CITE_RE / runner.evalgold.CITE_RE / runner.verify.CITE)各自只认
严格单引用 r"\\[id=(\\d+)\\]" —— 成组与区间写法整段 token 静默漏掉: 那些 id 既不进
「对回 scope」校验, 也不进「零引用下断言」纪律判定。更阴的是混写
「见 [id=12] 与 [id=13, 14]」: 只校验 12, 13/14 无声放行。
实测命中 2 题(09-08/09-09): [id=307, 313, 315, 316] / [id=128-131]。

口径(三处共用, 不许各自再造一份):
- 只认「整段 token 全由 id 构成」的 [id=...]; 空内容或含非数字(如 [id=abc]) -> 不是引用, 原样保留。
- 分隔符: 半角逗号 / 全角逗号 / 顿号; 分段: N 或 N-M(连接符 - – ~ 皆可)。
- 区间过宽(> _RANGE_MAX)视为不可解析, 不展开 —— 防 [id=1-99999999] 撑爆内存。
- 例外: 「[id=N，额外说明]」这种混写由 normalize_junk 在收口时抠成 [id=N](说明丢掉) ——
  不抠的话整条引用等于没标, 而且前头那句引文会被算到下一条真引用名下, 复核时误判成"挂错号"。
"""
import re

TOKEN_RE = re.compile(r"\[id\s*=\s*([^\]]*)\]")
_SEP = re.compile(r"[,，、]")
_PART = re.compile(r"^\s*([0-9]+)\s*(?:[-–~]\s*([0-9]+))?\s*$")
_RANGE_MAX = 500


def _parse_inner(inner):
    """token 内层文本 -> [(原样分段, [id...]), ...]; 非引用 -> None。"""
    inner = (inner or "").strip()
    if not inner:
        return None
    parts = []
    for raw in _SEP.split(inner):
        raw_s = raw.strip()
        m = _PART.match(raw_s)
        if not m:
            return None
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if hi < lo:
            lo, hi = hi, lo
        if hi - lo > _RANGE_MAX:
            return None
        parts.append((raw_s, list(range(lo, hi + 1))))
    return parts or None


def iter_cites(text):
    """产出所有「合法引用 token」的 (start, end, inner, ids)。"""
    for m in TOKEN_RE.finditer(text or ""):
        parts = _parse_inner(m.group(1))
        if parts is None:
            continue
        ids = sorted({i for _, xs in parts for i in xs})
        yield m.start(), m.end(), m.group(1), ids


def cite_ids(text):
    """全部被引 id, 去重升序。"""
    ids = set()
    for _, _, _, xs in iter_cites(text):
        ids.update(xs)
    return sorted(ids)


def has_cite(text):
    """有没有至少一个合法引用(token 内至少一个 id)。"""
    return next(iter_cites(text or ""), None) is not None


_JUNK_TAIL = re.compile(r"^\s*(\d+)\s*[,，、;；\s]")


def normalize_junk(text):
    """把「[id=N，额外说明]」这种混写抠成 [id=N](说明丢掉)。返回 (新正文, 抠了几处)。

    背景(§10.50 实测): 模型爱把热度一起塞进括号 —— [id=49，like=530]。这种 token **解析不出来**,
    于是整条引用等于没标: 既不进"对回 scope"校验、也不进纪律判定, 而它前头那句引文会被算到
    **下一条**真引用的名下 —— 复核时看着像"号挂错了", 其实号就在原地、只是被说明噎住了。
    实测 q1 报的 24 处「挂错号」**全部**是这一种: 抠出 id 后挂错归零(见 _syscheck/verify_quote_repair.py)。

    只动「解析不出来、且以数字打头再接分隔符」的: 合法多引用 [id=49, 52] 与 [id=abc] 原样不动。
    """
    if not text:
        return text, 0
    n = [0]

    def _sub(m):
        if _parse_inner(m.group(1)) is not None:      # 合法的(含成组/区间/混写)一律不碰
            return m.group(0)
        hit = _JUNK_TAIL.match(m.group(1))
        if not hit:
            return m.group(0)                        # [id=abc] / [id=] 这类不是"混说明", 原样留
        n[0] += 1
        return "[id=%s]" % hit.group(1)

    out = TOKEN_RE.sub(_sub, text)
    return (out, n[0]) if n[0] else (text, 0)


def strip_bad(text, bad):
    """剔除命中 bad 的 id: 分段内全好则整段保留, 某分段含坏 id 则该分段整体丢弃;
    整条 token 的分段全丢则连 token 一起删。返回 (新文本, 实际剔掉的 id 列表)。"""
    if not text:
        return text, []
    badset = {int(b) for b in bad}
    dropped = set()

    def _sub(m):
        parts = _parse_inner(m.group(1))
        if parts is None:
            return m.group(0)
        keep = []
        for raw, ids in parts:
            hit = [i for i in ids if i in badset]
            if hit:
                dropped.update(hit)
            else:
                keep.append(raw)
        if len(keep) == len(parts):
            return m.group(0)
        if not keep:
            return ""
        return "[id=" + ", ".join(keep) + "]"

    out = TOKEN_RE.sub(_sub, text)
    if not dropped:
        return text, []
    out = re.sub(r"[ \t]*\n[ \t]*\n[ \t]*\n+", "\n\n", out).strip() or "(空)"
    return out, sorted(dropped)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    total = ok = 0
    fails = []

    def chk(name, cond, got=""):
        global total, ok
        total += 1
        print(("PASS" if cond else "FAIL"), name)
        if cond:
            ok += 1
        else:
            fails.append("%s -> %r" % (name, got))

    chk("[id=1, 2, 3]", cite_ids("[id=1, 2, 3]") == [1, 2, 3], cite_ids("[id=1, 2, 3]"))
    chk("[id=1-3]", cite_ids("[id=1-3]") == [1, 2, 3], cite_ids("[id=1-3]"))
    chk("[id=7]", cite_ids("[id=7]") == [7], cite_ids("[id=7]"))
    chk("混写 单+组", cite_ids("见 [id=12] 与 [id=13, 14]") == [12, 13, 14], "")
    chk("混写 单+区间", cite_ids("[id=5, 9-11]") == [5, 9, 10, 11], cite_ids("[id=5, 9-11]"))
    chk("去重", cite_ids("[id=3, 3, 2]") == [2, 3], cite_ids("[id=3, 3, 2]"))
    chk("顿号/全角逗号", cite_ids("[id=1、2，3]") == [1, 2, 3], "")
    chk("非数字 [id=abc] 不算引用", cite_ids("与空[id=abc]忽略") == [], cite_ids("[id=abc]"))
    chk("空 [id=] 不算引用", cite_ids("[id=]") == [], "")
    chk("降序区间 [id=5-3] -> 3,4,5", cite_ids("[id=5-3]") == [3, 4, 5], cite_ids("[id=5-3]"))
    chk("超宽区间不展开", cite_ids("[id=1-99999999]") == [], cite_ids("[id=1-99999999]"))
    chk("无引用", cite_ids("纯文本 无引用") == [], "")
    chk("has_cite 真好", has_cite("[id=1, 2]") is True, "")
    chk("has_cite 假好", has_cite("[id=abc]") is False, "")
    chk("剔组内坏 id", strip_bad("[id=1, 999, 3]", [999])[0] == "[id=1, 3]", strip_bad("[id=1, 999, 3]", [999]))
    chk("剔组内坏 id 返回值", strip_bad("[id=1, 999, 3]", [999])[1] == [999], "")
    chk("全坏 -> 整 token 删", strip_bad("[id=1, 2]", [1, 2])[0] == "(空)", strip_bad("[id=1, 2]", [1, 2]))
    chk("区间内坏 -> 整段删", strip_bad("[id=128-131]", [129])[0] == "(空)", strip_bad("[id=128-131]", [129]))
    chk("混写只剔坏的", strip_bad("见 [id=12] 与 [id=13, 14]", [13])[0] == "见 [id=12] 与 [id=14]",
        strip_bad("见 [id=12] 与 [id=13, 14]", [13]))
    chk("真例 4 组剔 1", strip_bad("x[id=307, 313, 315, 316]y", [313])[0] == "x[id=307, 315, 316]y",
        strip_bad("x[id=307, 313, 315, 316]y", [313]))
    chk("真例 8 组", cite_ids("[id=260, 261, 262, 264, 266, 267, 268, 270]") ==
        [260, 261, 262, 264, 266, 267, 268, 270], "")
    chk("非引用 token 原样保留", strip_bad("空[id=abc]在", [999])[0] == "空[id=abc]在", "")
    chk("无坏 id 原样", strip_bad("[id=1-3]", [9])[0] == "[id=1-3]", "")

    chk("混写说明 -> 抠出 id", normalize_junk("[id=49，like=530]")[0] == "[id=49]",
        normalize_junk("[id=49，like=530]"))
    chk("混写说明 计数", normalize_junk("a[id=49，like=530]b[id=7 x]c")[1] == 2, "")
    chk("合法成组不动", normalize_junk("[id=49, 52]")[0] == "[id=49, 52]", "")
    chk("合法区间不动", normalize_junk("[id=1-3]")[0] == "[id=1-3]", "")
    chk("[id=abc] 不动", normalize_junk("[id=abc]")[0] == "[id=abc]", "")
    chk("无混写 -> 原样 + 0", normalize_junk("纯文本[id=1]") == ("纯文本[id=1]", 0),
        normalize_junk("纯文本[id=1]"))
    chk("真例 like=2016", normalize_junk("[id=36，like=2016]")[0] == "[id=36]", "")
    chk("真例 半角逗号", normalize_junk("[id=430, like=12]")[0] == "[id=430]", "")

    print("PASS=%d FAIL=%d" % (ok, len(fails)))
    for f in fails:
        print("  FAIL:", f)
    sys.exit(1 if fails else 0)
