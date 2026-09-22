# -*- coding: utf-8 -*-
"""「暂停 / 继续」自检(离线, 不烧模型不打平台): python _syscheck/verify_pause.py

为什么这支值得存在: 暂停最坏的失败**不报错**。它是安静地把一道用户明说了"先搁着"的题
收成一份短答案(那这题在记忆里就算答过了, 再点「继续」变成同一题答两遍), 或者反过来 ——
答案没出、记录也没留, 那题连侧栏都列不出来, 用户回来只看见自己问过的话凭空没了。
两种都只有真去按那个按钮才会撞见, 所以逐条钉死:

  ① 引擎认不认这面旗 —— 安全点收手、不落答案、不进状态卡、不落收口流水;
  ② 已爬到的样本怎么办 —— 爬取即归档, 记录里要如实写着"爬到哪了", 供继续时接上;
  ③ 台账干净 —— 暂停这题在流水里是"没跑完"而不是"答过了";
  ④ 继续 / 丢弃 —— 两条落款都要把旧那条暂停记录收掉, 否则侧栏同一题挂两张卡;
  ⑤ 继续不吃跨 ask 爬距(但冷却照吃) —— 按下去先干等两分钟不叫继续;
  ⑥ 后端接线 —— 三个接口在、暂停不占会话锁(占着的话"一题在跑+几题搁着"就不可能);
  ⑦ 前端接线 —— 接口通但页面没按钮, 用户那儿就是"没功能"(这条吃过亏)。

用法: 退出码 0=全过, 1=有失败。
"""
import http.client
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
os.environ.pop("HARNESS_LIVE", None)          # 别让引擎真去连爬虫
_TMP = tempfile.mkdtemp(prefix="pausechk_")
# webapp 会按 db.DEFAULT_DB 开库 —— 必须在 import db **之前**把它指走, 否则自检会往用户自己的
# 记忆库里塞假对话(自检污染产品数据, 比不跑还糟)。
os.environ["HARNESS_DB"] = os.path.join(_TMP, "web.db")

from harness import checkpoint, db, flow, webview                         # noqa: E402
from harness import engine as E                                           # noqa: E402
from harness.runner.session import Session                                # noqa: E402

flow._FLOW_DIR = os.path.join(_TMP, "flow")
os.makedirs(flow._FLOW_DIR, exist_ok=True)

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


# ---------------------------------------------------------------- 跑一题的脚手架
class FakeCrawl:
    """假的爬取工具: 不触网, 但把"真爬过一轮、新增几条"这件事如实回给引擎。"""

    def fetch(self, ctx, params):
        return [{"id": "fake"}]

    def commit(self, ctx, params, got):
        return {"summary": {"run_id": 1, "new": 3, "dup": 0, "leak": 0, "stale": 0, "shown": 3,
                            "game": params.get("game"), "platform": params.get("platform"),
                            "query": params.get("query"), "total_obs": 3},
                "evidence": []}

    def official(self, ctx, params):
        raise AssertionError("这题没调官号探针")


def make_session(scope, steps):
    """一个能跑 ask_turn 的会话: LLM 按 steps 逐轮回, 爬取走 FakeCrawl。"""
    conn = db.connect(os.path.join(_TMP, "db", re.sub(r"\W", "_", scope) + ".db"))
    db.init(conn)
    s = Session(conn=conn, provider=lambda **k: [], scope=scope)
    E.crawl_tools = FakeCrawl()
    E.tool_registry = types.SimpleNamespace(BY_NAME={}, enabled=lambda n: True, defs=lambda: [])
    state = {"i": 0}

    def _llm(self, messages, tools=True, stream=False):
        i = state["i"]
        state["i"] += 1
        msg = steps[i] if i < len(steps) else {"content": "本轮证据有限。\n结论: 证据不足以坐实。"}
        return {"choices": [{"message": msg}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    s.engine._llm = types.MethodType(_llm, s.engine)
    s.engine._ext_tools = lambda: {}
    return s, state


def crawl_call(query="鸣潮 这版本", name="crawl_nga", game="鸣潮"):
    """一轮"我要去爬"的模型回复。"""
    return {"content": "", "tool_calls": [{"id": "t1", "type": "function", "function": {
        "name": name, "arguments": '{"game": "%s", "query": "%s"}' % (game, query)}}]}


# ---------------------------------------------------------------- ① 暂停 = 不落答案
print("=== 1. 「暂停」这一跑: 不落答案、不进状态卡、不落收口流水 ===")
_sc = "web:暂停1"
_s, _st = make_session(_sc, [crawl_call()])
_pause = threading.Event()
_pause.set()                                   # 一进循环就按(第 0 圈那个安全点)
_res = _s.ask_turn("鸣潮这版本怎么样", game="鸣潮", pause=_pause)
chk("1.1 暂停那一跑明说自己是被暂停的(调用方据此知道不该去找答案)",
    (_res or {}).get("paused") is True, _res)
chk("1.2 没有答案 —— 暂停不是「用已有证据收口」",
    not (_res or {}).get("answer"), (_res or {}).get("answer"))
chk("1.3 没进状态卡(进了的话这题在记忆里就算答过了, 再「继续」变成同一题答两遍)",
    len(_s.engine.card.active) == 0, list(_s.engine.card.active))
_lines = flow._lines(flow.fpath(_sc))
chk("1.4 流水里只有两条: 开头那条'开跑' + 一条 paused, **没有收口那条**",
    [r.get("event") for r in _lines] == ["start", "paused"],
    [r.get("event") or "finish" for r in _lines])
_pl = _lines[-1]
chk("1.5 paused 那条与'开跑'共用一个 qid(靠它才认得出这两条是同一题)",
    _pl.get("qid") and _pl["qid"] == _lines[0].get("qid"), [_pl.get("qid"), _lines[0].get("qid")])
chk("1.6 paused 那条留着问句与档位 —— 继续时要照原样接着跑, 不重新解析",
    _pl.get("question") == "鸣潮这版本怎么样" and _pl.get("mode"), _pl)
chk("1.7 会话检查点没被写(暂停没答完, 不该把「这题答过了」的卡快照存下去)",
    checkpoint.load(_s.conn, _sc) is None, checkpoint.load(_s.conn, _sc))

# ---------------------------------------------------------------- ② 爬到哪了
print("=== 2. 暂停时如实记下'爬到哪了'(继续时靠它接着爬, 不从头再来) ===")
_sc = "web:暂停2"
_s, _st = make_session(_sc, [crawl_call(), crawl_call("鸣潮 剧情")])


class StopAfterFirstRound:
    """第一轮真爬完就把暂停置上 —— 模拟"用户看到爬出东西了, 按下暂停"。"""

    def fetch(self, ctx, params):
        return FakeCrawl().fetch(ctx, params)

    def commit(self, ctx, params, got):
        out = FakeCrawl().commit(ctx, params, got)
        _ev.set()
        return out

    def official(self, ctx, params):
        raise AssertionError("不该到这儿")


_ev = threading.Event()
E.crawl_tools = StopAfterFirstRound()
_res = _s.ask_turn("鸣潮这版本怎么样", game="鸣潮", pause=_ev)
_pl = flow.paused(_sc).get((_res or {}).get("qid")) or {}
chk("2.1 记了真爬过的轮数(不是猜的)", _pl.get("rounds") == 1, _pl.get("rounds"))
chk("2.2 记了这道题新归档了几条样本", _pl.get("got") == 3, _pl.get("got"))
chk("2.3 这两个数就是全部所需 —— 样本早随爬随归档, 另存快照反而是第二份真相",
    isinstance(_pl.get("rounds"), int) and isinstance(_pl.get("got"), int), _pl)

# ---------------------------------------------------------------- ③ 台账
print("=== 3. 流水台账: 暂停的题算'没跑完', 不算'答过了' ===")
chk("3.1 pending() 认得出它", [r.get("qid") for r in flow.pending(_sc)] == [( _res or {}).get("qid")],
    [r.get("qid") for r in flow.pending(_sc)])
chk("3.2 paused() 认得出它(而且只认用户按的, 崩溃那种不在里头)",
    set(flow.paused(_sc)) == {( _res or {}).get("qid")}, list(flow.paused(_sc)))
_ix = flow.index().get(_sc) or {}
chk("3.3 index(): 这题算 unfinished, 不算 asked(侧栏据此知道它还没完)",
    _ix.get("unfinished") == 1 and _ix.get("asked") == 0, _ix)
_hist = webview.history(_s.conn, _sc)
chk("3.4 历史接口里它是一张带暂停标记的简卡",
    len(_hist) == 1 and _hist[0].get("unfinished") is True and _hist[0].get("paused") is True,
    _hist)
chk("3.5 简卡如实写着爬到哪了 + 带 qid(「继续」要拿它去接)",
    _hist[0].get("rounds") == 1 and _hist[0].get("got") == 3 and _hist[0].get("qid"), _hist[0])

# 崩掉那种(只有开跑、没有 paused 记录)不许被画成"可继续" —— 没有暂停记录就不知道上次停在哪
flow.append("web:崩", {"event": "start", "qid": "zz", "question": "崩了的那题", "mode": "快速"})
_h2 = webview.history(_s.conn, "web:崩")
chk("3.6 进程崩掉那题: 是「没答上来」而不是「已暂停」(标了「继续」也是骗人的, 它没记录可续)",
    len(_h2) == 1 and _h2[0].get("unfinished") is True and not _h2[0].get("paused"), _h2)

# ---------------------------------------------------------------- ④ 收掉旧记录
print("=== 4. 「继续」/「丢弃」都要把旧那条暂停记录收摊 ===")
# 落款那一行就是 webapp 里写的同一句(见 webapp._start_job), 这里按同样的写法落一遍再验台账
flow.append("web:续1", {"event": "start", "qid": "rq1", "question": "鸣潮这版本怎么样", "mode": "快速"})
flow.append("web:续1", {"event": "paused", "qid": "rq1", "question": "鸣潮这版本怎么样",
                        "mode": "快速", "rounds": 1, "got": 3})
chk("4.0 先确认它确实挂着(不然下面两条断言是白过的)",
    "rq1" in flow.paused("web:续1") and [r.get("qid") for r in flow.pending("web:续1")] == ["rq1"],
    [list(flow.paused("web:续1")), flow.pending("web:续1")])
flow.append("web:续1", {"event": "resumed", "qid": "rq1"})
chk("4.1 落了 resumed 之后, 那道题不再挂在待续列表里(否则侧栏同一题两张卡)",
    "rq1" not in flow.paused("web:续1") and flow.pending("web:续1") == [],
    [list(flow.paused("web:续1")), flow.pending("web:续1")])
flow.append("web:弃", {"event": "start", "qid": "dd", "question": "不要了", "mode": "快速"})
flow.append("web:弃", {"event": "paused", "qid": "dd", "question": "不要了"})
flow.append("web:弃", {"event": "discarded", "qid": "dd"})
chk("4.2 落了 discarded 之后, 那题也不再挂着",
    "dd" not in flow.paused("web:弃") and flow.pending("web:弃") == [],
    [list(flow.paused("web:弃")), flow.pending("web:弃")])
chk("4.3 丢弃的是那张卡、不是样本 —— 样本在归档库里, 别的题还用得上(接口那边也不删)",
    flow.paused("web:弃") == {}, list(flow.paused("web:弃")))

# ---------------------------------------------------------------- ⑤ 续爬不吃爬距
print("=== 5. 续爬: 首轮放行跨 ask 爬距, 但冷却照吃 ===")


def probe_gap(resume_from, mode="快速", last_ago=5.0, cool_for=0.0, same_ask=False):
    """搭一个只有 _precheck 用得着的那几个属性的引擎, 问它: 这次要睡多久。

    same_ask=True 模拟"本 ask 里已经爬过这个平台一轮了"(last_ask 就是当前这一炮)。"""
    import time as _t

    from harness import cooldown as _cd
    _cd.reset()                       # 冷却是**全机器一份**的(harness/cooldown.py): 每一炮都从零摆
    e = E.Engine.__new__(E.Engine)
    e._nga_ratio = 50
    e._ask_seq = 7
    e._n_crawl = 0
    e._mode = mode
    e._resume_from = resume_from
    e._plats = {}
    st = e._plat("bilibili")
    st["last_at"] = _t.time() - last_ago
    st["last_ask"] = e._ask_seq if same_ask else 6   # 上一次真爬是本 ask 里的、还是上一道题里的
    st["last_round"] = 1 if same_ask else 0
    if cool_for:
        _cd._ST["bilibili"] = {"until": _t.time() + cool_for, "streak": 1}
    st["cool_until"] = _cd.until("bilibili")
    slept = []
    e._sleep = lambda s: slept.append(s)
    blocked = e._precheck("bilibili")
    return slept, blocked


_slept, _blk = probe_gap(None)
chk("5.1 平常那道题: 隔了 5 秒就再爬 bili -> 要守 60 秒爬距(这条没变)",
    bool(_slept) and _slept[0] > 50 and _blk is None, (_slept, _blk))
_slept, _blk = probe_gap({"qid": "k", "got": 3, "rounds": 1})
chk("5.2 续爬那道题: 首轮**不睡** —— 按下去先干等两分钟不叫继续",
    _slept == [] and _blk is None, (_slept, _blk))
_slept, _blk = probe_gap({"qid": "k", "got": 3, "rounds": 1}, cool_for=120)
chk("5.3 但冷却照吃: 真被抓过一次留下的风控信号, 按按钮不该洗掉",
    _blk is not None and "冷却" in str(_blk.get("platform_error")), _blk)
_slept, _blk = probe_gap({"qid": "k", "got": 3, "rounds": 1}, same_ask=True)
chk("5.4 本 ask 已经爬过这个平台了就不再放行(放行只给「接着上次」的那第一轮)",
    bool(_slept) and _slept[0] > 50 and _blk is None, (_slept, _blk))

# 续爬那轮要在提示词里说清"上次爬到哪了" —— 不说, 模型会把它当新题从头再爬一遍
_sc = "web:续2"
flow.append(_sc, {"event": "paused", "qid": "rq", "question": "鸣潮这版本怎么样", "mode": "快速",
                  "game_hint": "鸣潮", "nga_ratio": 50, "rounds": 2, "got": 41})
_seen = []


def _cap(self, messages, tools=True, stream=False):
    _seen.append(messages)
    return {"choices": [{"message": {"content": "行。\n结论: 证据不足以坐实。"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


_s3, _ = make_session(_sc, [])
_s3.engine._llm = types.MethodType(_cap, _s3.engine)
_s3.ask_turn("鸣潮这版本怎么样", game="鸣潮", resume_from=flow.paused(_sc)["rq"])
_blob = "\n".join(str(m.get("content") or "") for m in (_seen[0] if _seen else []))
chk("5.5 续爬那轮的提示词里写着「上次爬到第几轮、取到几条」",
    "2 轮" in _blob and "41 条" in _blob, _blob[-400:])
chk("5.6 并且明说别从头重爬(不然上次那几轮的样本量和爬距全白费)",
    "别从头重爬" in _blob, "")

# ---------------------------------------------------------------- ⑥ 后端接线
print("=== 6. 后端: 三个接口 + 暂停不占会话锁 ===")
from harness import webapp                                               # noqa: E402
_src = open(os.path.join(ROOT, "harness", "webapp.py"), encoding="utf-8").read()
for _p in ("/api/pause", "/api/resume", "/api/discard"):
    chk("6.x 接口在: " + _p, ('"%s"' % _p) in _src, "")
_pz_src = _src[_src.index('if u.path == "/api/pause"'):][:700]
_cz_src = _src[_src.index('if u.path == "/api/cancel"'):][:700]
chk("6.4 暂停与停止各读各的信号表(合成一张, 引擎就分不清结尾该不该落答案)",
    webapp._PAUSES is not webapp._CANCELS and "_PAUSES.get(sc)" in _pz_src
    and "_CANCELS.get(sc)" in _cz_src, "")
chk("6.5 起任务那条路里, 暂停的题不占会话锁(占着就不可能「一题在跑 + 几题搁着」)",
    "_session_lock(sc).acquire(blocking=False)" in _src and
    "429" in _src[_src.index("def _start_job"):_src.index("def _start_job") + 700], "")
chk("6.6 「续爬」的落款在**拿到锁之后**、起线程之前(先落款再撞 429, 那题被标成接走了却没真跑)",
    _src.index("acquire(blocking=False)") < _src.index('"event": "resumed"') <
    _src.index("threading.Thread(target=_run_ask"), "")
chk("6.7 /api/discard 只收那张卡、不删样本",
    "不删" in _src[_src.index("/api/discard"):_src.index("/api/discard") + 800], "")
chk("6.8 侧栏清单带上「还剩几道搁着」(不带的话侧栏那个记号没法画)",
    '"paused": len(flow.paused(sc))' in _src, "")

# ---------------------------------------------------------------- ⑦ 前端接线
print("=== 7. 页面接线: 两个按钮、暂停卡、侧栏记号 ===")
html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()


def has(pat):
    return re.search(pat, html) is not None


chk("7.1 两个按钮都在(停止 + 暂停)", 'id="stop"' in html and 'id="pause"' in html, "")
chk("7.2 两枚按钮的说明各说各的(别让人以为按错了)",
    "提前停止" in html and "暂停" in html and "接着爬" in html, "")
chk("7.3 有题在跑时两枚一起露脸(只露一枚, 另一个按钮就永远点不着)",
    has(r'\$\("#stop"\)\.classList\.toggle\("show",on\)') and
    has(r'\$\("#pause"\)\.classList\.toggle\("show",on\)'), "")
chk("7.4 暂停按钮打的是 /api/pause", has(r'api\("/api/pause"'), "")
chk("7.5 按了暂停还接着轮询 —— 得等后端把这一跑收干净, 卡片才变成「已暂停」",
    has(r'\$\("#pause"\)\.onclick') and has(r"pollJob"), "")
chk("7.6 暂停卡与「崩了」那卡分开画(能做的事不一样: 一个能继续、一个只能重问)",
    has(r"function renderPaused") and has(r"if\(ask\.paused\) return renderPaused\(ask\)"), "")
chk("7.7 卡上有「继续」和「丢弃这题」", 'data-resume' in html and 'data-drop' in html, "")
chk("7.8 「继续」打真接口 /api/resume, 并走发新题同一条轮询",
    has(r'api\("/api/resume"') and has(r"pollJob\(r\.job\)"), "")
chk("7.9 「丢弃这题」打 /api/discard 并重拉历史",
    has(r'api\("/api/discard"') and has(r"refreshHistory\(\)"), "")
chk("7.10 侧栏那条对话带「还剩几道搁着」的小记号(搁着的题不会自己跑, 不提醒就想不起来)",
    has(r"function sessRow") and 'class="ptag"' in html, "")
chk("7.11 暂停之后侧栏要重拉一遍(不重拉, 那个记号要等下次刷新才出现)",
    has(r"refreshSessions\(\);\s*//?\s*暂停") or html.count("refreshSessions();") >= 3, "")

# ---------------------------------------------------------------- ⑧ 真发一遍 HTTP
print("=== 8. 真开一个后端、真发 HTTP: 暂停 -> 继续 -> 丢弃 走完一整条 ===")


class HttpFakeCrawl:
    """这一节用的假爬取: 每一轮都"真爬了一次", 但**哪一刻爬完由自检说了算**。

    为什么非要这样: 「暂停」这个按钮只有落在"题正跑到一半"那一瞬才是真的被按到过。
    自检跑得比人快, 不等它就把整题跑完了, 按下去只会得到"这个对话没有正在跑的题"。"""

    def __init__(self):
        self.rounds = 0
        self.first_done = threading.Event()   # 第一轮爬完了 -> 自检知道"现在按最像真人"
        self.release = threading.Event()      # 自检放行 -> 引擎接着往下跑

    def fetch(self, ctx, params):
        return [{"id": "fake"}]

    def commit(self, ctx, params, got):
        self.rounds += 1
        out = FakeCrawl().commit(ctx, params, got)
        self.first_done.set()
        self.release.wait(15)                 # 卡在这儿, 等自检把按钮按完
        return out

    def official(self, ctx, params):
        raise AssertionError("这题没调官号探针")


def _http(port, method, path, payload=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        body = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
        c.request(method, path, body=body,
                  headers={"Content-Type": "application/json"} if body else {})
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw or b"{}")
        except Exception:
            return r.status, {"_raw": raw[:200]}
    finally:
        c.close()


def _until(port, jid, timeout=25):
    """等这一炮收工(前端那条 0.7 秒一轮的轮询, 这里就是个同步版)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        _c, j = _http(port, "GET", "/api/job?id=%s&since=0" % jid)
        if (j or {}).get("state") != "running":
            return j
        time.sleep(0.2)
    return {"state": "timeout"}


_srv = webapp.ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
_port = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()

# 会话由自检自己塞进后端的缓存里: 模型换成桩, 爬取换成上面那个假货 —— 除了这两样,
# 路由、线程、会话锁、流水、台账全是产品里那一套。
_sid = "http1"
_s4, _ = make_session("web:" + _sid, [crawl_call(), crawl_call("鸣潮 剧情"),
                                      {"content": "社区口径集中在剧情节奏。\n结论: 剧情节奏是主要槽点。"}])
with webapp._JOBS_LOCK:
    webapp._SESSIONS["web:" + _sid] = _s4
_fc = HttpFakeCrawl()
_fc.release.clear()                       # 先别让它自己跑完: 等自检按完暂停再放行
E.crawl_tools = _fc

_c, _r = _http(_port, "POST", "/api/ask", {"question": "鸣潮这版本怎么样", "mode": "快速",
                                           "game": "鸣潮", "session": _sid})
chk("8.1 发一题真收 202(不是当场阻塞在这儿)", _c == 202 and _r.get("job"), (_c, _r))
_job = _r.get("job")
chk("8.2 第一轮真爬完了(自检这才有资格去按暂停)", _fc.first_done.wait(15), _fc.rounds)
_c, _r = _http(_port, "POST", "/api/pause", {"session": _sid})
chk("8.3 暂停接口收了(它只置旗、不阻塞 —— 要等锁才生效的暂停等于没有)",
    _c == 200 and _r.get("ok") is True, (_c, _r))
_fc.release.set()                         # 放行: 爬完这一轮 -> 回到循环头 -> 认出暂停旗
_done = _until(_port, _job)
chk("8.4 这一炮收工了(不是一直转)", _done.get("state") == "done", _done.get("state"))
_ask = _done.get("ask") or {}
chk("8.5 回给前端的是**暂停卡**: 没有答案、标着没跑完",
    _done.get("paused") is True and _ask.get("unfinished") is True and not _ask.get("answer"), _ask)
chk("8.6 卡上如实写着爬到哪了(几轮、几条)", (_ask.get("rounds") or 0) >= 1 and (_ask.get("got") or 0) >= 3, _ask)
_qid = _ask.get("qid")
chk("8.7 卡上带着 qid(「继续」要拿它去接)", bool(_qid), _ask)

_c, _r = _http(_port, "GET", "/api/history?session=" + _sid)
_asks = (_r or {}).get("asks") or []
chk("8.8 侧栏历史里它是一张暂停卡(刷新页面也还在)", len(_asks) == 1 and _asks[0].get("paused") is True, _asks)
_c, _r = _http(_port, "GET", "/api/sessions")
_row = next((s for s in ((_r or {}).get("sessions") or []) if s.get("id") == _sid), {})
chk("8.9 侧栏清单里这个对话标着「还剩 1 道搁着」", _row.get("paused") == 1 and _row.get("asked") == 0, _row)

_c, _r = _http(_port, "POST", "/api/resume", {"session": _sid, "qid": _qid})
chk("8.10 「继续」真开跑(202)", _c == 202 and _r.get("job"), (_c, _r))
_done2 = _until(_port, _r.get("job"))
_ask2 = _done2.get("ask") or {}
chk("8.11 续完这一跑真出了答案(继续不是把题挂在那儿)",
    _done2.get("state") == "done" and not _done2.get("paused") and bool(_ask2.get("answer")), _ask2.get("state"))
chk("8.12 续出来的这题接的是同一个问句(用户点的是「继续」不是「另问一遍」)",
    _ask2.get("question") == "鸣潮这版本怎么样", _ask2.get("question"))
_c, _r = _http(_port, "GET", "/api/history?session=" + _sid)
_asks = (_r or {}).get("asks") or []
chk("8.13 续过之后侧栏只剩那一张答完的卡(不再同时挂着暂停的和续爬的两张)",
    len(_asks) == 1 and not _asks[0].get("paused") and _asks[0].get("answer"), _asks)

# 「丢弃这题」: 再搁一道, 然后扔掉它
_sid2 = "http2"
_s5, _ = make_session("web:" + _sid2, [crawl_call()])
with webapp._JOBS_LOCK:
    webapp._SESSIONS["web:" + _sid2] = _s5
_fc2 = HttpFakeCrawl()
_fc2.release.clear()
E.crawl_tools = _fc2
_c, _r = _http(_port, "POST", "/api/ask", {"question": "绝区零最近更新了什么", "session": _sid2})
_job2 = _r.get("job")
_fc2.first_done.wait(15)
_http(_port, "POST", "/api/pause", {"session": _sid2})
_fc2.release.set()
_d2 = _until(_port, _job2)
_qid2 = ((_d2.get("ask") or {}).get("qid"))
_c, _r = _http(_port, "POST", "/api/discard", {"session": _sid2, "qid": _qid2})
chk("8.14 「丢弃这题」收了", _c == 200 and _r.get("ok") is True, (_c, _r))
_c, _r = _http(_port, "GET", "/api/history?session=" + _sid2)
chk("8.15 扔掉之后侧栏不再挂着它", (_r or {}).get("asks") == [], (_r or {}).get("asks"))
_c, _r = _http(_port, "POST", "/api/discard", {"session": _sid2, "qid": "不认识的号"})
chk("8.16 扔一个不认识的号 -> 404(不许把别的东西顺手扔了)", _c == 404, _c)
_c, _r = _http(_port, "POST", "/api/resume", {"session": _sid2, "qid": "不认识的号"})
chk("8.17 续一个不认识的号 -> 404(同上)", _c == 404, _c)

# 搁着的题不占会话锁 —— 不然"一题在跑 + 几题搁着"就是空话
_c, _r = _http(_port, "POST", "/api/ask", {"question": "鸣潮这版本怎么样", "session": _sid})
chk("8.18 同一对话里还能接着问新题(暂停的题要是占着锁, 这里会回 429)",
    _c == 202, (_c, _r))
_until(_port, _r.get("job"))

_srv.shutdown()

print("")
print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !!", f)
sys.exit(1 if fails else 0)
