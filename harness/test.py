#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键自测入口: python -m harness.test。

分组(见 GATES):
  * offline gate —— 纯离线确定性回归(_tmp/verify_*/test_*), 每次必跑, 零网络/零外部依赖
  * server gate  —— 需远端隧道 / LoRA token / DEEPSEEK 的脚本, 默认跳过(--server 才追加)
  * gold replay  —— M1.2 黄金回放(evalgold), 接入后并入 offline gate

判据: guard 进程内直调取违规数; 其余脚本子进程跑, 退出码 0 且输出无 Traceback => PASS。
    分发包里没有 _tmp/(那是开发期脚本, 不随产品走), 这些记 SKIP 不记 FAIL;
   黄金回放要本机录过流水的底, 新装库是空的, 同样 SKIP。
用法:
  python -m harness.test            # guard + offline gate(纯标准库, 无网络)
  python -m harness.test --server   # 追加 server gate(需隧道与凭证在位)
  python -m harness.test --list     # 列出各分组脚本与一句说明
退出码: 0 无 FAIL/ERR; 1 有 FAIL/ERR。
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根 = harness/..
TMP = os.path.join(ROOT, "_tmp")

# 回归脚本往哪儿找(**按序取第一个存在的**):
#   _tmp/      —— 开发期那批(源码仓库才有, 不随产品走)
#   _syscheck/ —— 随产品走的自检(挂在这里的脚本新装机器上也必须能跑)
# 同名脚本两处都有时以 _tmp/ 为准(开发期那份更全), 分发包里自然落到 _syscheck/。
GATE_DIRS = (TMP, os.path.join(ROOT, "_syscheck"))

# (文件名, 一句说明)。说明随脚本头部 docstring, 供 --list 与人快速判断归属。
OFFLINE_GATE = [
    ("verify_flow.py",             "engine.ask 收口自动落 jsonl 流水(A1)"),
    ("verify_a2.py",               "runner/verify.py verifier 确定性(A2)"),
    ("verify_a3_final_answer.py",  "run3b q2 收口关单回归(A3)"),
    ("verify_budget_rounds.py",    "预算按轮计 + 双平台同轮并发放行(B)"),
    ("verify_keyword_fanout.py",   "同轮多短词 fan-out: 同平台串行+跨平台并发(09-12)"),
    ("verify_feedback.py",         "feedback 回路: correct/supersede+留痕"),
    ("verify_context_wiring.py",   "context token 计量接线自检"),
    ("verify_l2_l4_activation.py", "L2 checkpointer + L4 alias registry 转活"),
    ("verify_nga_body_fallback.py","NGA 主板最近帖正文 grep 兜底"),
    ("verify_nga_resolver.py",     "NGA 两段式主板定位(fid)"),
    ("verify_nga_single_search.py","NGA 定主板 + 逐词单搜合并"),
    ("verify_append_only_guard.py","guard 自检: 命中/无误报/真库零违规"),
    ("test_finalize.py",           "engine 收口对垃圾/干净样本行为(只读)"),
    ("test_scope_iso.py",          "namespace 记忆隔离 scope 落地"),
    ("test_live_map.py",           "live.py 响应->items 映射转换(probe payload)"),
    ("verify_cite_guard.py",       "M1.3 收口证据校验: 剔编造/跨scope [id=N]"),
    ("verify_determiner_discipline.py", "M2 determiner 纪律+软信号读取契约(09-10)"),
    ("verify_web_wiring.py",       "前台接线自检: 改版后 id/类/语法不脱钩(09-12)"),
    ("verify_sessions.py",         "刷新后还找得回对话: 开跑标记/没跑完的那题/后端侧栏清单(09-17)"),
    ("verify_evidence_pool.py",    "证据池+追问自检: 记忆按会话隔离/证据跨会话可回捞(09-15)"),
    ("verify_session_lru.py",      "网页后端按会话缓存: 同 sid 同 Session/回收不伤在跑的(09-15)"),
    ("verify_session_memo.py",     "会话压缩+答案回捞: 早前轮次压成纪要/recall_answer 取全文(09-15)"),
    ("verify_followup_scan.py",    "追问位置扫描: 长会话逐位置回捞/被挤出上下文的题不许丢(09-15)"),
    ("verify_threaded_session.py", "跨线程会话: 缓存 Session + 每题新线程下的第二题不许挂(09-15)"),
    # 下面四支只住在 _syscheck/(随产品走), 源码仓库那侧没有 —— 一直没挂进门里, 顺手补上。
    ("verify_guardrail.py",        "收口前压上下文: 压到线下/不丢 tool 消息/配得上 tool_call_id"),
    ("verify_bili_risk.py",        "B站风控: 412 不重试 -> 进会话冷却 -> 挡下一题(起回环假服务端)"),
    ("verify_mcp.py",              "工具挂 MCP: 注册表一处管三处 + 外部服务连得上连不上"),
    ("verify_status.py",           "爬虫状态: 两盏灯取值 + 忙态/冷却态优先级(受桩保护)"),
    ("verify_official_probe.py",   "官号探针 crawl_official: 映射/每ask一次/占爬轮/落库(09-12)"),
    ("verify_official_mid_pick.py", "官号 mid 判定: 认证过筛+名字/粉丝定选+无官方拒答(09-13)"),
    ("verify_scratch_strip.py",    "答案开头漏出的英文草稿: 确定性剔除 + 不该删的不误删(09-16)"),
    ("verify_session_forget.py",   "删除一个对话: 会话记忆真删干净/证据池与注册表一行不动(09-16)"),
    ("verify_pdf.py",              "PDF 导出: 答案按网页同款排版/引用清单/度量如实标注(09-16)"),
    ("verify_cancel.py",           "「停止」: 引擎认停的安全点/不停成平台故障 + 网页接线(09-16)"),
    ("verify_pause.py",            "「暂停/继续」: 不落答案不占锁/断点续爬不吃爬距/侧栏可回溯(09-21)"),
    ("verify_llmstream.py",        "流式解析: SSE 分帧/tool_calls 分片拼接/截断判失败(09-16)"),
    ("verify_tokens.py",           "token 消耗: 每炮都记账(含流式) + 缓存分档不重复计(09-16)"),
    ("verify_crawlgate.py",        "爬虫闸门: 互斥/先进先出/爬距/停止·暂停能叫走排队的人(09-22)"),
    ("verify_secrets.py",          "分发包体检: 树里没有凭证(cookie/令牌/密钥字面量)(09-22)"),
]

SERVER_GATE = [
    ("verify_crawl_noquery.py",    "缺 query 受控 error 不报废 + 真爬(需隧道/远端服务)"),
    ("verify_nlp_judge_chain.py",  "爬虫→NLP→judge 度量链(需隧道+LoRA token+DSK)"),
]

# 黄金回放: harness/runner/evalgold.py 读 gold_cases.json 对录得 flow 条目确定性打分,
# 并入 offline gate(不烧 LLM/不触网, 数据全在 harness/data)。


SCRATCH = os.path.join(ROOT, "_scratch")


def child_env():
    """自检脚本会 mkdtemp 建临时库, 默认落到系统临时目录(Windows 上就是 C 盘)。
    用户要求产物一律不落 C 盘, 所以统一把它们指到仓库下的 _scratch/。

    这里**只建不删**: 十来支自检脚本大多跑完不收拾自己的 mkdtemp, 攒在 _scratch/ 里
    (实测跑一次留十个目录、约 1MB)。删目录要用破坏性原语, 而 harness/guard.py 正是
    拦这个的 —— 它管的是数据层只增不改, 但扫描是整目录的, 没有必要为收垃圾去给它开口子。
    要清就手工删 _scratch/。"""
    os.makedirs(SCRATCH, exist_ok=True)
    e = dict(os.environ)
    e["TEMP"] = e["TMP"] = e["TMPDIR"] = SCRATCH
    # tiktoken 没设缓存目录时也往 TEMP 里塞(data-gym-cache, 1.6MB)。跟着上面走的话每跑一次
    # 就换一个新目录、于是每跑一次都重新联网下一遍。给它钉一个固定位置, 下过一次就一直用。
    e["TIKTOKEN_CACHE_DIR"] = os.path.join(SCRATCH, "tiktoken")
    return e


def run_child(name):
    """子进程跑 _tmp/<name> 或 _syscheck/<name>, 返回 (tag, rc, tail)。tag ∈ PASS/FAIL/ERR/SKIP。"""
    path = next((os.path.join(d, name) for d in GATE_DIRS
                 if os.path.exists(os.path.join(d, name))), None)
    if path is None:
        # 两处都没有: 开发期脚本(不随产品走)在产品包里就是这样。不是失败, 是没这份。
        return "SKIP", None, "没有 %s(这脚本不属于本包)" % name
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, path], cwd=ROOT, env=child_env(),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600)
        out, err, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        return "ERR", None, "超时(>600s)"
    dt = time.time() - t0
    blob = (out or "") + "\n" + (err or "")
    if "Traceback" in blob:
        return "FAIL", rc, "Traceback(见下方尾部)\n" + _tail(blob)
    if rc == 0:
        return "PASS", rc, "%.1fs" % dt
    return "FAIL", rc, "rc=%s\n" % rc + _tail(blob)


def _tail(blob, n=30):
    lines = [l for l in blob.splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def _has_recorded_flow():
    """本机有没有录到题(harness/data/flow/*.jsonl)。新装的库是空的 —— 黄金回放没东西可放。"""
    d = os.path.join(ROOT, "harness", "data", "flow")
    return os.path.isdir(d) and any(f.endswith(".jsonl") for f in os.listdir(d))


def _gold_scopes():
    """gold_cases.json 里各题的 scope; 读不出来返回 None(让回放自己去报错)。"""
    p = os.path.join(ROOT, "harness", "runner", "gold_cases.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    return [c.get("scope") for c in (d.get("cases") or []) if isinstance(c, dict) and c.get("scope")]


def run_evalgold():
    """子进程跑黄金回放打分(python -m harness.runner.evalgold --json)。"""
    if not os.path.exists(os.path.join(ROOT, "harness", "runner", "gold_cases.json")):
        return "SKIP", "没有 gold_cases.json"
    if not _has_recorded_flow():
        return "SKIP", "harness/data/flow 还是空的(新装没问过题) —— 没东西可回放"
    scopes = _gold_scopes()
    if scopes:
        # 黄金回放的"底"是开发期录的那批题(scope 写死在 gold_cases.json 里), 不随产品走。
        # 本机没这批流水时逐题报 ERR 只会把自测刷成一片红 —— 那是"没有可回放的底", 不是产品的错。
        from . import flow
        if not any(os.path.exists(flow.fpath(s)) for s in scopes):
            return "SKIP", ("本机没有这 %d 道题的流水(黄金回放的底是开发期录的, 不随产品走); "
                            "装的是产品包时跳过是正常的" % len(scopes))
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, "-m", "harness.runner.evalgold", "--json"],
                           cwd=ROOT, env=child_env(), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600)
        blob = (p.stdout or "") + "\n" + (p.stderr or "")
        rc = p.returncode
    except subprocess.TimeoutExpired:
        return "ERR", "超时(>600s)"
    if "Traceback" in blob:
        return "FAIL", "Traceback\n" + _tail(blob)
    return ("PASS" if rc == 0 else "FAIL"), "rc=%s\n" % rc + _tail(blob)


def run_guard():
    from . import guard
    probs = guard.check_harness()
    for p in probs:
        print("  VIOLATION", p)
    return probs


def run_gate(items, tag):
    n_pass = n_fail = n_skip = 0
    for i, (name, desc) in enumerate(items, 1):
        st, rc, info = run_child(name)
        if st == "SKIP":
            n_skip += 1
            print("  [%s] %-34s -- SKIP" % (tag, name))
            continue
        mark = {"PASS": "PASS", "FAIL": "FAIL", "ERR": "ERR "}[st]
        flag = "ok " if st == "PASS" else "XX "
        print("  [%s] %-34s %s %s" % (tag, name, flag, mark))
        if st == "PASS":
            n_pass += 1
        else:
            n_fail += 1
            print("        %s\n" % info)
    return n_pass, n_fail, n_skip


def main(argv):
    t0 = time.time()
    want_list = "--list" in argv
    want_server = "--server" in argv

    items = list(OFFLINE_GATE)
    n_pass = n_fail = 0
    nskip = 0

    if want_list:
        print("== offline gate(零外部依赖, 每次必跑) ==")
        for i, (name, desc) in enumerate(items, 1):
            print("  %2d. %-34s %s" % (i, name, desc))
        print("\n== server gate(--server 才跑, 需隧道/凭证) ==")
        for name, desc in SERVER_GATE:
            print("     %-34s %s" % (name, desc))
        print("\n== gold replay(黄金回放确定性打分) ==")
        print("     python -m harness.runner.evalgold   %s" % "读 gold_cases.json: 结论在/引用对回/无引即避/有真爬")
        return 0

    print("=== harness.test: guard + offline gate%s ==="
          % (" + server gate" if want_server else ""))

    # 1) guard
    probs = run_guard()
    if probs:
        n_fail += 1
        print("  [guard] append-only 扫描: %d 违规  XX FAIL" % len(probs))
    else:
        n_pass += 1
        print("  [guard] append-only 扫描: 0 违规  ok PASS")

    # 2) gold replay(黄金回放确定性打分; 失败/弱项如期望才算 PASS)
    st, info = run_evalgold()
    if st == "PASS":
        n_pass += 1
        print("  [gold] evalgold                        ok PASS")
    elif st == "SKIP":
        nskip += 1
        print("  [gold] evalgold                        -- SKIP (%s)" % info)
    else:
        n_fail += 1
        print("  [gold] evalgold                        XX %s\n      %s" % (st, info))

    # 3) offline gate
    gp, gf, gs = run_gate(items, "off")
    n_pass += gp
    n_fail += gf
    nskip += gs

    # 4) server gate(可选)
    if want_server:
        sp, sf, ss = run_gate(SERVER_GATE, "srv")
        n_pass += sp
        n_fail += sf
        nskip += ss
    else:
        nskip += len(SERVER_GATE)

    print("-" * 60)
    print("汇总: %d PASS  %d FAIL  %d SKIP  (%.1fs)"
          % (n_pass, n_fail, nskip, time.time() - t0))
    if nskip and not os.path.isdir(TMP):
        print("说明: 本机没有 _tmp/, 开发期的离线回归脚本不随产品走 —— 跳过是正常的；")
        print("      随产品走的那几个在 _syscheck/, 它们照跑。想跑全套: 去源码仓库跑 python -m harness.test。")
    if n_fail:
        print("有 FAIL/ERR: 见上方对应脚本尾部输出。")
    return 1 if n_fail else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
