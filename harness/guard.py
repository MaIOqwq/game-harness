#!/usr/bin/env python3
"""harness 数据层 append-only 守卫：静态扫描禁止破坏性文件/SQL 原语。

契约（ARCHITECTURE §5.1）：数据层只增不改——文件只读或追加("a")，sqlite 仅
CREATE IF NOT EXISTS + INSERT + ALTER 加列。本脚本把契约固化成可执行检查：
未来任何改动若往 harness/*.py 塞入删除/覆盖/截断原语，扫描即列出违规并 exit 1。

运行（从仓库根）：
    python -m harness.guard            # 扫默认根 = 本文件所在 harness/
    python harness/guard.py --root <dir>
import 用法：from harness import guard; guard.check_harness() -> [违规串]

设计取舍：整词正则命中即报（宁严勿漏）；本文件自身被扫描豁免（其源码必然提及
这些 token）；注释里请勿含精确违规 token 以免误报。
另外: 文件头部带 "guard: allow-write" 标记的模块整份豁免 —— 给"写配置文件、不属于数据层"
的模块用(见 scan_file)。另有一个**逐名**放行的会话记忆清除模块(见 _PURGE_ALLOWED)。
这是给本包内自己看的纪律检查, 不是安全边界。
"""
import os
import re
import sys

HARNESS_ROOT = os.path.dirname(os.path.abspath(__file__))

# 破坏性文件原语（整词命中即违规）
_FILE_DESTRUCT_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:"
    r"os\.(?:remove|unlink|rmdir|removedirs|rename|renames|replace|truncate)\s*\("
    r"|shutil\.(?:rmtree|move|copy|copyfile|copy2|copytree)\s*\("
    r"|\.(?:unlink|truncate|write_text|write_bytes)\s*\("
    r")"
)

# 破坏性 SQL（DELETE/DROP/TRUNCATE/UPDATE..SET；容忍大小写）
_SQL_DESTRUCT_RE = re.compile(
    r"\b(DELETE\s+FROM|DROP\s+TABLE|DROP\s+INDEX|TRUNCATE\s+TABLE"
    r"|UPDATE\s+\w+\s+SET)\b",
    re.IGNORECASE,
)

_OPEN_RE = re.compile(r"\bopen\s*\(")
_MODE_TOKEN_RE = re.compile(r"['\"]([a-zA-Z+]{1,4})['\"]")


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def _open_write_modes(src):
    """找 open(...) 参数表里以 'w' 开头的模式字面量 = 截断/覆盖写。"""
    hits = []
    for m in _OPEN_RE.finditer(src):
        depth = 0
        j = m.end() - 1
        while j < len(src):
            c = src[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            continue  # 括号不平衡（罕见），跳过
        arg = src[m.start():j + 1]
        for tm in _MODE_TOKEN_RE.finditer(arg):
            if tm.group(1).startswith("w"):
                hits.append((m.start(), tm.group(1)))
    return hits


def scan_source(src, path="<mem>"):
    """扫一段源码文本，返回违规描述列表（空 = 干净）。"""
    problems = []
    for regex, kind in ((_FILE_DESTRUCT_RE, "文件"), (_SQL_DESTRUCT_RE, "SQL")):
        for m in regex.finditer(src):
            problems.append("%s:%d 破坏性%s原语: %s"
                            % (path, _line_of(src, m.start()), kind, m.group(0)[:80]))
    for pos, mode in _open_write_modes(src):
        problems.append("%s:%d 写模式 open(..., %r) 会截断/覆盖: 改只读或 'a' 追加"
                        % (path, _line_of(src, pos), mode))
    return problems


# 文件**头部**出现这行 = 声明豁免。只给"写配置文件、不是数据层"的模块用 —— 目前仅 settings.py
# (它写 settings.json / secrets.json, 临时文件 + os.replace 原子落盘是应该的, 不该按数据层办)。
# 限定"头部"而非全文任意位置: 豁免一眼可见, 也免得别的文件随手一句注释把自己放行了。
_EXEMPT_MARK = "guard: allow-write"
_EXEMPT_WINDOW = 2000

# 逐名放行的**清除模块**: 会话记忆的物理清除(前台「删除这个对话」), 要真删 checkpoint/session_claim
# 的行、真删流水文件。只增契约管的是**证据池**(爬到的原帖永不改写、永不遗忘), 会话记忆本来就该让
# 用户能清掉 —— schema.sql 那句「物理清除是前台删除按钮的独立任务」说的就是它。
# 按**文件名**放行, 不用 _EXEMPT_MARK 那种注释标记: 标记是"整份豁免"的声明, 抄一句就能放行,
# 而这里要的是"全 harness 只有这一处", 名字写死才看得出是不是被扩大了。
_PURGE_ALLOWED = ("forget.py",)


def scan_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        src = f.read()
    if _EXEMPT_MARK in src[:_EXEMPT_WINDOW]:
        return []
    return scan_source(src, path=path)


def check_harness(root=None):
    """扫 root（默认 harness/）下全部 .py，返回违规列表；guard.py 自身豁免。"""
    root = root or HARNESS_ROOT
    problems = []
    for base, _dirs, files in os.walk(root):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            if fn == os.path.basename(__file__):  # 豁免本文件（源码必然含这些 token）
                continue
            if fn in _PURGE_ALLOWED:              # 唯一的清除模块, 见 _PURGE_ALLOWED
                continue
            problems.extend(scan_file(os.path.join(base, fn)))
    return problems


def main(argv):
    root = argv[1] if len(argv) > 1 and argv[1] != "--root" else None
    if argv and argv[0] == "--root" and len(argv) > 1:
        root = argv[1]
    problems = check_harness(root)
    for p in problems:
        print("  VIOLATION", p)
    print("harness append-only guard: %d 违规%s"
          % (len(problems), "（数据层只增不改）" if not problems else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
