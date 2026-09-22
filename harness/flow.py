# -*- coding: utf-8 -*-
"""engine 工具层统一流水(§10.8): 每次 ask 收口落一条 jsonl, 按 scope 隔离成文件。
复盘 / verifier / 回放的数据底 —— 与 session_log(结论/翻篇)并存, 互不替代:
session_log 存"结论摘记"供检索回捞, 这里存"每题完整一条"(answer 全文 + 逐轮 trace + 引用集)。
写 = engine.ask 收口处的自动副作用(同落库, 不由模型/调用方决定);
关: 设环境变量 HARNESS_FLOW=0(测试等不想落盘的场景)。"""
import datetime
import json
import os
import re

_FLOW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "flow")


def _slug(scope):
    return re.sub(r"[^\w.\-]+", "_", (scope or "prod").strip() or "prod")[:80]


def fpath(scope, flow_dir=None):
    """本 scope 的流水文件。flow_dir 供测试/清除注入(与 read 同款参数), 不给就用默认目录。"""
    return os.path.join(flow_dir or _FLOW_DIR, _slug(scope) + ".jsonl")


def append(scope, record):
    """追加一条; 返回文件路径(或 None=已关)。record 会被补 ts/scope, 原 dict 不变。"""
    if os.environ.get("HARNESS_FLOW", "1") == "0":
        return None
    rec = dict(record)
    rec.setdefault("scope", scope)
    rec.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
    os.makedirs(_FLOW_DIR, exist_ok=True)
    p = fpath(scope)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return p


def _lines(path):
    """一个 jsonl 文件里的全部记录(坏行跳过)。文件不在 -> 空表。"""
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if not ln.strip():
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    return out


def records(scope):
    """本 scope 全部**题**的流水(写入序, 时间正序)。只读本 scope 那一个文件, 不扫整个目录 ——
    引擎每答一题都要拿它拼前几轮上下文, 别让这一步变成 O(全部会话)。
    `read()` 是"回放/排查用"的全库扫描, 热路径(每 ask)一律走这里。

    "开跑"标记(event=start, 见 starts)不算题, 在这里滤掉: 它没有答案, 混进来会被三处当真题用 ——
    引擎拼前几轮上下文、webview 出题卡、自检里那些"这题是不是被停的"断言。"""
    return [r for r in _lines(fpath(scope)) if not r.get("event")]


def starts(scope):
    """本 scope 的「开跑」标记(engine.ask 一开头就落的那一条)。

    为什么要它: 一题要爬几分钟, 中途进程断了/被重启, 光靠"收口那条流水"的话这个对话在整个
    记忆里就**什么都没发生过** —— 侧栏列不出来、点进去也没有这一题, 用户只看见自己问过的话凭空没了。
    谁要"这个对话问过什么"(哪怕还没答完)才调这里。"""
    return [r for r in _lines(fpath(scope)) if r.get("event") == "start"]


def _closed_qids(recs):
    """已经有着落的 qid。三条路都算"这题有了交代", 别再当没跑完:

      ① 落过收口流水(有答案) —— 答完了;
      ② 落过 event=resumed —— 用户点过「继续」, 那条新开跑接的是它, 旧的那条不该再挂一张卡;
      ③ 落过 event=discarded —— 用户点了「丢弃这题」, 明确不要了。

    缺了 ②③ 的话, 侧栏会同时挂着"暂停的那条"和"续爬出来的那条"同一道题两张卡,
    或者挂着用户已经扔掉的那张 —— 都是流水明明知道、界面却装作不知道。"""
    done = {r.get("qid") for r in recs if r.get("qid") and not r.get("event")}
    for r in recs:
        if r.get("event") in ("resumed", "discarded") and r.get("qid"):
            done.add(r["qid"])
    return done


def pending(scope):
    """本 scope 里**问了、却没留下答案**的那几题(进程跑到一半没了, 或用户按了暂停)。

    判据是并发号 qid: 收口那条流水带着开头那条约好的同一个号。老流水没有 qid -> 一律当已收口,
    不把历史题误报成"没跑完"。"""
    done = _closed_qids(_lines(fpath(scope)))
    return [r for r in starts(scope) if r.get("qid") and r.get("qid") not in done]


def paused(scope):
    """本 scope 里**用户主动按了暂停、还没接着跑**的那几题: qid -> 那条暂停记录。

    与 pending() 的区别是"谁把它停下的": pending 里多数是进程崩了(用户没按过任何按钮);
    这里这几条是用户明说了「先搁着」的 —— 所以要给出「继续」按钮, 而崩溃那几条只能重问。
    一条记录都没有 = 这个对话没暂停过任何题。"""
    recs = _lines(fpath(scope))
    done = _closed_qids(recs)
    out = {}
    for r in recs:
        if r.get("event") == "paused" and r.get("qid") and r["qid"] not in done:
            out[r["qid"]] = r
    return out


def index():
    """全库一览: scope -> {asked, unfinished, first_q, last_ts}。

    给网页侧栏列对话用(开页面时跑一次, 不在热路径上)。题数只数**有答案的**;
    一个只有"开跑"标记的对话也照样列出来 —— 那正是"问了没跑完"的那个, 最需要让人看见。"""
    out = {}
    if not os.path.isdir(_FLOW_DIR):
        return out
    for fn in sorted(os.listdir(_FLOW_DIR)):
        if not fn.endswith(".jsonl"):
            continue
        recs = _lines(os.path.join(_FLOW_DIR, fn))
        sc = next((r.get("scope") for r in recs if r.get("scope")), None)
        if not sc:
            continue
        done = _closed_qids(recs)
        e = {"asked": 0, "unfinished": 0, "first_q": "", "last_ts": ""}
        for r in recs:
            if r.get("event") == "start":
                if r.get("qid") and r["qid"] not in done:
                    e["unfinished"] += 1
            elif r.get("event"):
                # 台账行(paused/resumed/discarded)**不是题**: 把它算进 asked, 侧栏会对着一个
                # 只有一道搁着题的对话显示"已问 2 题"。但它也不是空气 —— 下面那两行照样要跑,
                # 不然刚按了暂停的那个对话在侧栏里排不到最上面(它确实刚动过)。
                pass
            else:
                e["asked"] += 1
            if not e["first_q"] and r.get("question"):
                e["first_q"] = r["question"]
            if r.get("ts"):
                e["last_ts"] = r["ts"]
        out[sc] = e
    return out


def recent(scope, n=3):
    """本 scope 最近 n 条流水(见 records)。"""
    return records(scope)[-n:]


def read(scope=None, flow_dir=None):
    """读全部(或某 scope)**题**的流水, 按文件名字典序(= 写入序)返回 dict 列表。flow_dir 供测试注入。
    "开跑"标记同 records() 一并滤掉 —— 读流水的都是要答案的(回放/核引/scoring), 没答案的那种不是题。"""
    base = flow_dir or _FLOW_DIR
    out = []
    if not os.path.isdir(base):
        return out
    for fn in sorted(os.listdir(base)):
        if not fn.endswith(".jsonl"):
            continue
        with open(os.path.join(base, fn), encoding="utf-8") as f:
            for ln in f:
                if not ln.strip():
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if rec.get("event"):
                    continue
                if scope is None or rec.get("scope") == scope:
                    out.append(rec)
    return out
