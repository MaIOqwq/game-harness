# -*- coding: utf-8 -*-
"""「停止」自检(离线, 不烧模型不打平台): python _syscheck/verify_cancel.py

真跑一题要几分钟还要花 token, 而这支要能在新装机器上随手跑 —— 所以用**假模型 + 假爬虫**,
只验"停止"这件事本身的契约:

  ① 引擎认不认停止信号、认在**哪些安全点**上(出发前 / 一轮里第 2 个词之前 / 等待中);
  ② 停止**不许**留副作用 —— 尤其不许把 bilibili 记成"平台挂了"顺手冷却五分钟(用户停个题
     不该把平台关掉), 也不许丢已经爬回来的样本(那一轮的 commit 必须照常走完);
  ③ 网页那条链接上了没有(按钮 / 接口 / 卡片标记 / PDF 标记) —— 接口通但页面没接线,
     用户那儿就是"没这功能"(这条吃过亏)。
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

os.environ["HARNESS_FLOW"] = "1"
os.environ.pop("HARNESS_LIVE", None)          # 别让引擎去连真爬虫
_TMP_DB = tempfile.mkdtemp(prefix="cancelchk_db_")
# ⑧ 那一节要起**真的**网页后端, 它会按 db.DEFAULT_DB 开库 —— 必须在 import db 之前把它指到临时目录,
# 否则这支自检会往用户自己的记忆库里塞一道假题(自检污染产品数据, 比不跑还糟)。
os.environ["HARNESS_DB"] = os.path.join(_TMP_DB, "web.db")

from harness import db, flow, pdfout, webview            # noqa: E402
from harness import engine as E                          # noqa: E402
from harness.runner.session import Session               # noqa: E402

# 第 ①~④ 节会把引擎那两个模块级全局换成假货(换了不还), 第 ⑧ 节要跑"真接口", 得换回来。
_REAL_REGISTRY = E.tool_registry

TMP = tempfile.mkdtemp(prefix="cancelchk_")
flow._FLOW_DIR = os.path.join(TMP, "flow")               # 流水写临时目录, 不碰产品包的数据

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


# ---------------------------------------------------------------- 桩
FINAL = ("本轮证据有限。\n结论: 证据不足以坐实，只给出有限判断。")


class FakeCrawl:
    """假爬虫。sent 记**真发出去**的检索(平台, 词) —— 自检就是靠它判"这一条到底发了没"。
    on_fetch(第几条) 让用例在"爬到一半"那一刻置位停止信号。"""

    def __init__(self, on_fetch=None):
        self.sent = []
        self.commits = 0          # commit = 样本落库(已经爬回来的样本必须走完这一步)
        self.on_fetch = on_fetch
        self._rid = 0

    def fetch(self, ctx, params):
        self.sent.append((params["platform"], params.get("query")))
        if self.on_fetch:
            self.on_fetch(len(self.sent))
        time.sleep(0.05)          # 模拟"抓取要一会儿": 两个平台并发时才都进得来
        return [{"id": "fake"}]

    def commit(self, ctx, params, got):
        self.commits += 1
        self._rid += 1
        return {"summary": {"run_id": self._rid, "new": 2, "dup": 0, "leak": 0, "stale": 0,
                            "shown": 2, "game": params.get("game"),
                            "platform": params.get("platform"), "query": params.get("query"),
                            "total_obs": 2},
                "evidence": []}

    def official(self, ctx, params):
        raise AssertionError("停止之后不该再发官号探针")


def make_llm(script):
    """按脚本依次回话(用完一律回 FINAL)。返回 (函数, 调用计数)。"""
    state = {"i": 0}
    calls = {"n": 0}

    def _llm(self, messages, tools=True, stream=False):
        calls["n"] += 1
        i = state["i"]
        state["i"] += 1
        msg = script[i] if i < len(script) else {"content": FINAL}
        return {"choices": [{"message": msg}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    return _llm, calls


_TCN = [0]


def tc(name, query):
    _TCN[0] += 1
    return {"id": "call_%d" % _TCN[0], "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps({"query": query, "game": "鸣潮"})}}


def prep(scope, script, fake=None):
    """一套干净的"会话": 临时库 + 假爬虫 + 假模型。引擎那两处真爬/真工具面换掉, 全程不出网。"""
    conn = db.connect(os.path.join(TMP, "db", re.sub(r"\W", "_", scope) + ".db"))
    db.init(conn)
    s = Session(conn=conn, provider=lambda **k: [], scope=scope)
    fake = fake or FakeCrawl()
    E.crawl_tools = fake
    E.tool_registry = types.SimpleNamespace(BY_NAME={}, enabled=lambda n: True, defs=lambda: [])
    llm, calls = make_llm(script)
    s.engine._llm = types.MethodType(llm, s.engine)
    s.engine._ext_tools = lambda: {}
    return s, fake, calls


# ---------------------------------------------------------------- ① 不传信号 = 老样子
s, fake, calls = prep("t:plain", [
    {"content": "", "tool_calls": [tc("crawl_nga", "版本"), tc("crawl_bili", "版本")]},
])
res = s.ask_turn("鸣潮这版本怎么样", game="鸣潮")
chk("1.1 没传停止信号时照常双平台各爬一次(行为与从前一样)",
    sorted(p for p, _q in fake.sent) == ["NGA", "bilibili"], fake.sent)
chk("1.2 没传信号时 cancelled 恒 False", res["cancelled"] is False, res.get("cancelled"))
chk("1.3 答案照常收口", "结论" in res["answer"], res["answer"][:60])

# ---------------------------------------------------------------- ② 出发前就停了
s, fake, calls = prep("t:early", [])
ev = threading.Event()
ev.set()
res = s.ask_turn("鸣潮这版本怎么样", game="鸣潮", cancel=ev)
chk("2.1 出发前就停: 一条检索都没发出去", fake.sent == [], fake.sent)
chk("2.2 出发前就停: cancelled=True", res["cancelled"] is True, res.get("cancelled"))
chk("2.3 出发前就停: 仍收敛出一份带结论的答复(不是空手报错)",
    "结论" in res["answer"], res["answer"][:60])
chk("2.4 出发前就停: 只烧一次收口调用(不空转模型)", calls["n"] == 1, calls)
chk("2.5 出发前就停: 没把平台记成故障",
    s.engine._plats.get("bilibili", {}).get("cool_until", 0) == 0, s.engine._plats)

# ---------------------------------------------------------------- ③ 停在"模型还在想"的那一瞬
# 这一瞬最难: 工具调用**已经在手上了**(模型这一轮的话说完了)、还没发出去。停在别处都有兜底,
# 只有这里必须专门挡一道 —— 不挡的话用户点了停止, 这一轮照样打平台、白等一分多钟才收手。
ev = threading.Event()
s, fake, calls = prep("t:mid", [
    {"content": "", "tool_calls": [tc("crawl_nga", "版本"), tc("crawl_bili", "版本")]},
    {"content": "", "tool_calls": [tc("crawl_nga", "角色"), tc("crawl_bili", "角色")]},
])
_llm0 = s.engine._llm


def _llm_and_stop(self, messages, tools=True, stream=False):
    """第二轮的工具调用刚到手就把停止信号置上 —— 模拟"模型话说完的那一瞬用户按了停止"。"""
    r = _llm0(messages, tools)
    if r["choices"][0]["message"].get("tool_calls") and len(fake.sent) >= 2:
        ev.set()
    return r


s.engine._llm = types.MethodType(_llm_and_stop, s.engine)
res = s.ask_turn("鸣潮这版本怎么样", game="鸣潮", cancel=ev)
chk("3.1 停在「工具调用到手、还没发出去」那一瞬: 这一轮的检索一条都不发",
    len(fake.sent) == 2 and all("角色" not in q for _p, q in fake.sent), fake.sent)
chk("3.2 已经爬到的那一轮样本照常落库(不丢)", fake.commits == 2, fake.commits)
chk("3.3 爬到一半停: cancelled=True", res["cancelled"] is True, res.get("cancelled"))
chk("3.4 爬到一半停: 答案照常带结论", "结论" in res["answer"], res["answer"][:60])
chk("3.5 停止不算平台故障: bili 不进冷却、不熔断",
    s.engine._plats.get("bilibili", {}).get("cool_until", 0) == 0
    and s.engine._plats.get("bilibili", {}).get("ask_fail") is None, s.engine._plats)
recs = flow.records("t:mid")
chk("3.6 流水里记着这一题是被停的(刷新页面后照样认得出来)",
    bool(recs) and recs[-1].get("cancelled") is True, recs[-1].get("cancelled") if recs else None)
chk("3.7 没被停的那题不误标(流水里 plain 那条)",
    [r.get("cancelled") for r in flow.records("t:plain")] == [False],
    [r.get("cancelled") for r in flow.records("t:plain")])

# ---------------------------------------------------------------- ④ 一轮里发好几个短词, 停在第 1 个之后
ev = threading.Event()
s, fake, calls = prep("t:words", [
    {"content": "", "tool_calls": [tc("crawl_nga", "词一"), tc("crawl_nga", "词二"), tc("crawl_nga", "词三")]},
], fake=FakeCrawl(on_fetch=lambda n: ev.set() if n >= 1 else None))
res = s.ask_turn("鸣潮这版本怎么样", game="鸣潮", cancel=ev)
chk("4.1 同一平台排着的后两个词没接着打完(只发了第 1 个)", fake.sent == [("NGA", "词一")], fake.sent)
chk("4.1b 已经抓回来的那一条照常落库(停不等于白爬)", fake.commits == 1, fake.commits)
chk("4.2 轮内被停掉的那几个词: 也不许记成平台故障",
    s.engine._plats.get("NGA", {}).get("ask_fail") is None, s.engine._plats)
chk("4.3 这一题仍收口出答案", "结论" in res["answer"] and res["cancelled"] is True, res["answer"][:40])

# ---------------------------------------------------------------- ⑤ 等待能被立刻打断
e2 = E.Engine.__new__(E.Engine)
e2._cancel = None
e2._pause = None                              # 另一面旗(「暂停」), 见 verify_pause.py
e2._resume_from = None                        # 不是续爬那一跑
chk("5.1 没传信号时 _stopped 恒 False", e2._stopped() is False, e2._stopped())
e2._cancel = threading.Event()
t0 = time.time()
threading.Timer(0.3, e2._cancel.set).start()
e2._sleep(30)
dt = time.time() - t0
chk("5.2 停止能打断长等待(bili 爬距最长要等 120 秒, 不能干等)", dt < 3, "%.2fs" % dt)
e2._cancel = threading.Event()                # 换一面没置位的旗: 上一条刚被置位过
t0 = time.time()
e2._sleep(0.2)
chk("5.3 没被打断时该等多久等多久", 0.2 <= time.time() - t0 < 3, "%.2fs" % (time.time() - t0))

# ---------------------------------------------------------------- ⑥ 翻译层/PDF 认得这个标记
conn = db.connect(os.path.join(TMP, "db", "rec.db"))
db.init(conn)
base = {"ts": "2026-09-16T10:00:00", "mode": "快速", "question": "q", "game_hint": "鸣潮",
        "answer": "证据不足。\n结论: 证据不足以坐实。", "concl": "证据不足以坐实。",
        "ev": "cites", "trace": []}
a1 = webview.build_ask(conn, dict(base, cancelled=True))
a2 = webview.build_ask(conn, dict(base))
chk("6.1 翻译层把流水里的 cancelled 带出来", a1["cancelled"] is True, a1.get("cancelled"))
chk("6.2 老流水(没这个字段)不误标", a2["cancelled"] is False, a2.get("cancelled"))
t1 = pdfout.text_of(a1)
t2 = pdfout.text_of(a2)
chk("6.3 PDF 里标明「这一题被提前停了」", "停止" in t1 and "停止" not in t2, "")

# ---------------------------------------------------------------- ⑦ 网页那条链有没有接上
html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
css = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
js = "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S))
markup = re.sub(r"<style>.*?</style>|<script.*?</script>", "", html, flags=re.S)
wa = open(os.path.join(ROOT, "harness", "webapp.py"), encoding="utf-8").read()
en = open(os.path.join(ROOT, "harness", "engine.py"), encoding="utf-8").read()
se = open(os.path.join(ROOT, "harness", "runner", "session.py"), encoding="utf-8").read()

chk("7.1 页面上有「停止」按钮", 'id="stop"' in markup, "")
chk("7.2 停止键有样式且平时不占位", re.search(r"\.stop\s*\{", css) and ".stop.show" in css, "")
chk("7.3 停止键接到 /api/cancel", re.search(r'\$\("#stop"\)\s*\.onclick', js)
    and "/api/cancel" in js, "")
chk("7.4 发送/停止两个键的状态一起切(不脱钩)", "function setAsking" in js
    and re.search(r'\$\("#stop"\)\.classList\.toggle\("show"', js), "")
chk("7.5 卡片上标明「已提前停止」", re.search(r"ask\.cancelled", js), "")
chk("7.6 后端有 /api/cancel 且认这个事件", '"/api/cancel"' in wa and "_CANCELS" in wa, "")
chk("7.7 后端把信号一路传到引擎(cancel 不断链)",
    re.search(r"sess\.ask_turn\([^)]*cancel=cancel", wa) and "cancel=None" in se
    and re.search(r"def ask\(self, user, game_hint=None, mode=", en), "")
chk("7.8 引擎里有「轮内哨兵」(停止掉的检索不当成平台故障)", "_STOPPED" in en, "")

# ---------------------------------------------------------------- ⑧ 按页面那套请求, 走真接口跑一遍
# 「接口通 ≠ 功能通」这条吃过亏(后端加了接口、页面没接上, 用户那儿就是没这功能), 所以这一节
# **按网页原样打**: 发题 -> 点停止 -> 轮询 -> 看答案与历史。不启浏览器(铁律), 爬虫与模型都换成
# 进程内的假货 —— 不出网、不烧 token, 但 HTTP 那一层(建事件/找事件/置旗/落流水/回历史)全是真的。
#
# ⚠ 上面那几节是**换了模块级全局并且不还**的, 到这儿得换回来:
#   crawl_tools 换成假爬虫 —— 这一节正需要(真爬虫会真开浏览器);
#   tool_registry 换成空壳 —— 这一节**必须换回真的**。不换回来引擎这一遍的工具面就是空的,
#   模型拿不到任何工具只好张口就答, 这一题 0.3 秒就收口, "点停止"压根没有窗口, 8.2/8.4/8.7 全红。
#   (本机当初绿过一次是侥幸: 产品树里遗留了一份 mcp_servers.json, 白白补了个外部工具顶上 ——
#    那份文件已删; 换个干净目录跑必红, 部署机上就是这么暴露出来的。)
E.tool_registry = _REAL_REGISTRY
E.crawl_tools = FakeCrawl()

FINAL_HTTP = "证据不足，只能给有限判断。\n结论: 证据不足以坐实，先给有限判断。"
_STUB = {"slow_done": False}

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer        # noqa: E402
from urllib.request import Request, urlopen                                # noqa: E402


class _StubLLM(BaseHTTPRequestHandler):
    """假的模型服务(OpenAI 兼容形状): 第一次开口先想 3 秒 —— 那 3 秒就是测试点「停止」的窗口。
    网页那条 emit 接上之后引擎走流式, 所以这里要照**真服务商那套**回: 带 stream_options 的请求
    回 SSE 分帧 + 末尾 usage 尾块 + `data: [DONE]`。不照这个回, 引擎一律判截断 —— 那不是产品坏了,
    是假服务的协议不对(流式解析的逐条契约在 verify_llmstream.py 里单独盯着)。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, ctype, out):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _json(self, msg):
        self._send("application/json", json.dumps(
            {"choices": [{"message": msg}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode("utf-8"))

    def _sse(self, msg):
        """顺带把「跨分片拼 tool_calls」也过一遍 HTTP 全程: arguments 故意拆成两片,
        后一片不带 id/name(真流就是这样, 只送增量)。"""
        evs = []
        if msg.get("content"):
            evs.append({"choices": [{"index": 0, "finish_reason": None,
                                     "delta": {"content": msg["content"]}}]})
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c["function"]
            args = fn["arguments"]
            cut = max(1, len(args) // 2)
            evs.append({"choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
                {"index": i, "id": c["id"],
                 "function": {"name": fn["name"], "arguments": args[:cut]}}]}}]})
            evs.append({"choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
                {"index": i, "function": {"arguments": args[cut:]}}]}}]})
        evs.append({"choices": [{"index": 0, "delta": {},
                                 "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}]})
        evs.append({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        blob = "".join("data: %s\n\n" % json.dumps(e, ensure_ascii=False)
                       for e in evs) + "data: [DONE]\n\n"
        self._send("text/event-stream", blob.encode("utf-8"))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        tools = body.get("tools")
        if tools and not _STUB["slow_done"]:
            _STUB["slow_done"] = True
            time.sleep(3)
        msg = ({"content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "crawl_nga",
                                             "arguments": json.dumps({"query": "版本", "game": "鸣潮"})}},
                               {"id": "c2", "type": "function",
                                "function": {"name": "crawl_bili",
                                             "arguments": json.dumps({"query": "版本", "game": "鸣潮"})}}]}
               if tools else {"content": FINAL_HTTP})
        if body.get("stream"):
            self._sse(msg)
        else:
            self._json(msg)


import harness.webapp as WA                                                # noqa: E402

os.environ["LLM_BASE_URL"] = "http://127.0.0.1:0"     # 占位, 下面拿到真端口再改
os.environ["LLM_API_KEY"] = "stub"
os.environ.pop("HARNESS_LIVE", None)                  # 上面 import 时被 setdefault 设回来了, 再摘掉

_llm_srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubLLM)
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:%d/v1" % _llm_srv.server_address[1]
threading.Thread(target=_llm_srv.serve_forever, daemon=True).start()

_web = WA._QuietServer(("127.0.0.1", 0), WA.Handler)
BASE = "http://127.0.0.1:%d" % _web.server_address[1]
threading.Thread(target=_web.serve_forever, daemon=True).start()


def _post(path, obj):
    req = Request(BASE + path, data=json.dumps(obj).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path):
    with urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


try:
    job = _post("/api/ask", {"session": "selfcheck", "question": "鸣潮这版本怎么样",
                             "mode": "快速", "game": "鸣潮", "ratio": 50})
    chk("8.1 发题接口收下了(202 + job)", bool(job.get("job")), job)
    # 等到"轮次"这条真推出来了再点停止。原来写死睡 0.8 秒 —— 停止要是抢在第一轮就绪前面,
    # 事件流里压根不会有 round, 8.12 就偶发红灯(那是测试抢跑, 不是产品坏了)。等不到也照停。
    _cur0 = 0
    for _ in range(20):
        time.sleep(0.3)
        _j0 = _get("/api/job?id=%s&since=%d" % (job["job"], _cur0))
        _cur0 = _j0.get("cursor") or _cur0
        if "round" in [e.get("type") for e in (_j0.get("events") or [])]:
            break
    c = _post("/api/cancel", {"session": "selfcheck"})
    chk("8.2 点停止: 接口认了", c.get("ok") is True, c)
    state = None
    cursor = 0
    evs = []                                          # 一路累积"增量取回来"的事件
    for _ in range(40):
        time.sleep(0.5)
        j = _get("/api/job?id=%s&since=%d" % (job["job"], cursor))
        evs.extend(j.get("events") or [])
        cursor = j.get("cursor") or cursor
        state = j.get("state")
        if state in ("done", "error"):
            break
    chk("8.3 被停的题照常收口(不是报错)", state == "done", (state, j.get("error")))
    a = j.get("ask") or {}
    chk("8.4 接口回来的卡片上标着「已提前停止」", a.get("cancelled") is True, a.get("cancelled"))
    chk("8.5 停了就没真爬: 本题新归档 0 条、爬 0 次",
        (a.get("stats") or {}).get("total") == 0 and (a.get("stats") or {}).get("runs") == 0,
        a.get("stats"))
    chk("8.6 卡片的 id/结论/引用核验一应俱全(页面照着画就行)",
        a.get("id") and a.get("concl") and isinstance(a.get("cites"), list)
        and (a.get("cite_check") or {}).get("cited") == 0, sorted(a.keys())[:8])
    # 实时事件那条通道: 页面靠 since 游标增量取, 一路累起来必须**不重不漏**(重了页面正文会叠,
    # 漏了秒表/进度行就卡住不动)。累计条数 == 最终游标, 正是"不重不漏"的充要判据。
    kinds = [e.get("type") for e in evs]
    chk("8.11 实时事件: 游标只回新增段(累计条数 == 最终游标)",
        cursor > 0 and len(evs) == cursor, (len(evs), cursor, kinds))
    chk("8.12 实时事件: 推了轮次与正文增量(页面的实时画面靠这两样)",
        "round" in kinds and "text-delta" in kinds, kinds)
    h = _get("/api/history?session=selfcheck")
    asks = h.get("asks") or []
    chk("8.7 刷新/重开页面(重新拉历史)也认得这一题是被停的",
        bool(asks) and asks[-1].get("cancelled") is True, asks[-1].get("cancelled") if asks else None)
    chk("8.8 历史里的 id 与当场那份一致(卡 id 不因刷新而变)", bool(asks) and asks[-1].get("id") == a.get("id"),
        (asks[-1].get("id") if asks else None, a.get("id")))
    c2 = _post("/api/cancel", {"session": "selfcheck"})
    chk("8.9 没题在跑时点停止: 如实说一声, 不当成功", c2.get("ok") is False and "没有" in (c2.get("error") or ""), c2)
    st = _get("/api/status?session=selfcheck")
    chk("8.10 停完状态接口照常答(没把后端带崩)", isinstance(st.get("crawlers"), list), list(st.keys()))
finally:
    _web.shutdown()
    _llm_srv.shutdown()

print("-" * 60)
print("%d 通过 / %d 失败" % (ok, total - ok))
for f in fails:
    print("  FAIL " + f)
sys.exit(0 if ok == total else 1)
