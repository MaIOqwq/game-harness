# -*- coding: utf-8 -*-
"""harness 的 HTTP 入口 —— 给网页用的那一层(前端唯一要连的东西)。

为什么需要它: 浏览器跑不了这个进程里的 Python, 更不能拿 DEEPSEEK_API_KEY。
所以把已有的一切(引擎 / 工具体 / 记忆库)包一层薄 HTTP 壳, 页面只跟这层说话;
壳里面还是同一套循环、同一批工具、同一个库 —— 不是另起一套架构。

零依赖(标准库 http.server), 与两个爬虫服务同一种写法。
跑: python -m harness.webapp [--port 8780]
"""
import argparse
import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import settings

# 必须在 supervisor/measure **之前**: 那两个模块 import 时就把环境变量读成模块常量了,
# 而 settings.json 里存着用户在页面上改过的值, 得先灌进环境变量才轮得到它们读。
settings.apply_to_env()

from . import cooldown, crawlgate, db, flow, forget, pdfout, supervisor, webview  # noqa: E402
from .engine import BILI_GAP                               # noqa: E402
from .mcp import client as mcp_client                      # noqa: E402
from .mcp import server as mcp_server                      # noqa: E402
from .runner.session import Session                        # noqa: E402
from .sources import live                                  # noqa: E402
from .tools import registry as tool_registry               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(HERE), "web")
DEFAULT_PORT = 8780

# 产品形态**默认真爬**: Engine 的 provider 缺省是 None, 而 None 会落到 mock provider ——
# 不打开这个开关, 页面会拿编造的样本作答(铁律不许编数字)。mock 只留给 CLI/测试/demo。
# 必须在建 Session(懒建, 首次请求时)之前设好, 故放模块级: 只要走 webapp 这条产品入口就一定是 live。
os.environ.setdefault("HARNESS_LIVE", "1")

# (前端 key, 显示名, live.py 里的平台名)
CRAWLERS = (("nga", "NGA 爬虫", "nga"), ("bili", "bilibili 爬虫", "bilibili"))

_MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
         ".json": "application/json; charset=utf-8", ".ico": "image/x-icon"}

# 管理面(改配置 / 管外部工具服务 / 删对话)只让本机与内网调。
# 这几条动的都是**这台机器**: 「接口地址」一改, 大模型密钥下次就发到那个地址去了(见 settings.llm_cfg
# 每次现读); 「测试」外部工具服务是拿本机真去起一个进程。产品是装在自己电脑上的, 本机安装天然
# 只有 127.0.0.1; 走隧道(ssh -L)或同机反代进来的, 源地址也仍是本机 —— 该用的人照样能用。
# 判据只看**来源地址**, 不看任何请求头(X-Forwarded-For 之类随手就能伪造)。问问题那条链不在这里面,
# 演示时给人点的功能一条都不挡。
_ADMIN_PATHS = ("/api/settings", "/api/model", "/api/mcp", "/mcp",
                "/api/forget", "/api/restore", "/api/trash")


def _is_local(host):
    """来源地址是不是本机/内网。"""
    h = (host or "").strip().lower()
    if h.startswith("::ffff:"):              # IPv4-mapped(双栈监听时常见)
        h = h[7:]
    if h in ("localhost", "::1") or h.startswith("127."):
        return True
    if h.startswith("10.") or h.startswith("192.168."):
        return True
    if h.startswith("172."):
        try:
            return 16 <= int(h.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False

# **入口不设门**（2026-09-22 用户拍板：demo 要能直接打开给人看，不能在入口拦人）。
# 现在只靠两头兜着：管理类接口只认本机(`_ADMIN_PATHS` + `_is_local`)，对话按会话号存取。
# ⚠ 归属校验**还没有**：`/api/sessions` 会把所有对话列出来，知道端口的人据此就能读任意对话 ——
# 要开 8780 给外人点之前得先堵这个口子(HANDOFF「待拍」里有记)。
_SESSIONS = {}     # scope -> Session
_JOBS = {}         # job id -> {"state": "running|done|error", "events": [...], "ask"/"error"}
_LOCKS = {}        # scope -> threading.Lock(一题只跑一个)
_CANCELS = {}      # scope -> threading.Event(这一题被点了「停止」) —— 与 _JOBS 同一把锁护着
# scope -> threading.Event(这一题被点了「暂停」)。与 _CANCELS 分开两张表, 不合成一个信号:
# 两者在"别接着爬了"这一步上一样, 但下场完全不同 —— 停止要落一份短答案, 暂停一个字都不落。
_PAUSES = {}
_JOBS_LOCK = threading.Lock()
# 跑完的任务留最近这么多条。为什么要管: 每条任务现在挂着**一串实时事件**(答案正文也在里面),
# 而 _JOBS 从前从不清理 —— 那就是随"问过多少题"一直涨。只丢跑完的, 正在跑的那条还有人轮询。
JOB_CAP = 60


def _evict_jobs():
    """丢掉最老的一批跑完的任务(调用方须持 _JOBS_LOCK)。dict 保插入序, 所以从头丢就是最老的。"""
    done = [k for k, v in _JOBS.items() if v.get("state") != "running"]
    for k in done[:max(0, len(done) - JOB_CAP)]:
        _JOBS.pop(k, None)


def _running_job(sid):
    """这个会话此刻有没有题在跑。有就把它整条(不含事件)拿出来 —— 页面刷新后靠它接回去。

    为什么按会话找而不是按任务号: 刷新把页面上的任务号弄丢了, 会话号却还在 localStorage 里,
    所以"我这个对话现在跑着哪一题"是页面**唯一还能问得出口**的问题。"""
    sc = _scope(sid)
    with _JOBS_LOCK:
        for jid, job in _JOBS.items():
            if job.get("state") == "running" and job.get("session") == sc:
                return jid, dict((k, v) for k, v in job.items() if k != "events"), \
                    list(job.get("events") or [])
    return None, None, None


def _job_update(job_id, **kw):
    """就地改这条任务(调用方不必持锁)。**必须就地改, 不能整条换掉**: 这条里挂着 events ——
    整条替换会把实时事件全丢, 而收尾那几下(最后一段正文、usage)恰恰是在"跑完"前后推的,
    前端游标当场归零, 最后一次轮询什么也拿不到(页面正文停在半截, 秒表也不归位)。"""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            job = _JOBS[job_id] = {"state": "running", "events": []}
        job.update(kw)

# 跨会话的爬取闸门(见 harness/crawlgate.py): **全进程一个实例**, 所有会话共用一个 ——
# 这才是"公用"的关键: 不同的人爬同一个平台会排队, 而不是一起挤爬虫服务那把单飞锁。
# _LOCKS(按会话)管的是"同一个对话别同时跑两题"; 这个管的是"同一台机器别同时爬同一个平台"。
_GATE = crawlgate.CrawlGate()

# 同时留在内存里的会话数上限。每个 Session 占一条 SQLite 连接 + 一张状态卡, 会话开多了不回收
# 就是慢慢漏。踢掉是安全的: 卡每轮收口都落了检查点(checkpoint 表), 下次回到这个会话会续上。
SESSION_CAP = 12


def _scope(sid):
    return "web:" + (sid or "anon")


def _session(sid):
    """按浏览器会话取\建一个 Session(scope 隔离, 记忆互不串)。

    顺带按"最久没用"回收: dict 的插入序当 LRU 队列使 —— 取用后重新插到队尾 = 刚用过。"""
    sc = _scope(sid)
    with _JOBS_LOCK:
        s = _SESSIONS.pop(sc, None)
        if s is None:
            s = Session(scope=sc, gate=_GATE)
        _SESSIONS[sc] = s
        _evict_sessions()
        return s


def _evict_sessions():
    """超出上限就从最久未用的那头踢(调用方须持 _JOBS_LOCK)。

    **正在跑题的会话不踢**: 那一轮的卡还在内存里, 抽走它等于把答到一半的上下文丢了
    (检查点只在收口时落, 中途被踢的那一轮就白答了)。锁对象(_LOCKS)不回收 ——
    它只有几十字节, 而回收它有真风险: 另一条线程可能正握着旧锁对象去拿同一把锁。"""
    for sc in list(_SESSIONS):
        if len(_SESSIONS) <= SESSION_CAP:
            return
        lk = _LOCKS.get(sc)
        if lk is not None and lk.locked():
            continue
        _SESSIONS.pop(sc, None)


def _session_lock(sc):
    with _JOBS_LOCK:
        if sc not in _LOCKS:
            _LOCKS[sc] = threading.Lock()
        return _LOCKS[sc]


def _login_flag(v):
    """爬虫 /health 报的是 'ok'/'bad'/'unknown' 字符串, 前端认的是 true/false/'unknown'。
    在边界归一: 两套词表各说各话的话, 'bad' 落不到前端的失效分支 —— 实测表现是登录态灯
    永远琥珀「待校验」、详情窗还写「无需处理」, 于是「去修复(扫码)」按钮根本不出现。"""
    if v is True or v == "ok":
        return True
    if v is False or v == "bad":
        return False
    return "unknown"


def _cool_secs(sid, plat):
    """这台机器在这个平台上还要冷却几秒(不在冷却 -> 0)。

    冷却现在是**全机器一份**(见 harness/cooldown.py): 被平台盯上的是这台机器的出口, 不是某个
    对话 —— 按对话记的话, A 刚被抓、界面提示"退 5 分钟", B 转头照爬不误, 等于没退避。
    sid 仍留在签名里(接口和前端都是按会话问状态的), 但答案与是哪个会话无关。
    这里**只读不建**: 轮询状态不该顺手建一个 Session(那会把空 scope 写进记忆库)。"""
    rem = cooldown.remaining(plat)
    return int(rem) + 1 if rem > 0 else 0       # 进一位: 倒计时显示 0 时它就该真的好了


def _cool_left(sid):
    """前端 key -> 剩余秒(不在冷却的键不出现)。"""
    out = {}
    for key, _name, plat in CRAWLERS:
        n = _cool_secs(sid, plat)
        if n:
            out[key] = n
    return out


def _ask_mode(sid):
    """这个会话最近一题用的档位 —— 决定 B 站爬距是 60 还是 120 秒(见 BILI_GAP)。取不到按快速档。"""
    sess = _SESSIONS.get(_scope(sid))
    return getattr(getattr(sess, "engine", None), "_mode", None) or "快速"


def _gap_note(sid, plat):
    """这个平台距下次能爬还剩几秒(爬距/平台级间距), 0 = 现在就能爬。

    这个数是**从闸门要来的**, 不是另算一份 —— 引擎真会等的就是它(见 crawlgate),
    所以页面上的倒计时与引擎走的是同一把尺。NGA 没有爬距概念(风控型爬距只在 B 站), 恒 0。"""
    if plat != "bilibili":
        return 0
    return _GATE.view(plat, _scope(sid), gap=BILI_GAP.get(_ask_mode(sid), 60))["gap_left"]


def _cool_note(sid, plat, doing):
    """平台在冷却 -> 一句能直接摆给用户看的话(含还需多久), 否则 None。

    **这是"冷却中强制不可使用"的落点**: 光把按钮藏起来不算数 —— 接口这层也得挡,
    否则手快连点、或别的东西直接打这个口, 就绕过去了。"""
    n = _cool_secs(sid, plat)
    if not n:
        return None
    return ("%s 正在冷却(爬太密被平台风控, 退避中), 还要 %d:%02d —— "
            "这期间不碰它, %s也等它走完再来" % (plat, n // 60, n % 60, doing))


def status_payload(sid=None):
    """两个爬虫的健康 + 登录态 + 此刻在不在用/在不在冷却。拿不到就 ok=false(前端红灯), 不编。

    爬虫是按需拉起的(见 supervisor), 所以"还没起"是**正常的静止态**, 不是故障:
    这时报 running=false, 前端画灰灯「未启动」, 而不是红灯吓人。
    这里**只读不拉** —— 页面每隔几秒就刷一次状态, 要是查状态把爬虫拉起来, 那就白按需了。

    busy / cool_left / gate 是三种"此刻正忙"的态, 前端各画一个灯(见 index.html 的 crawlerState):
    busy 来自爬虫自己(有 /crawl 在跑), cool_left 来自引擎(风控退避, 带倒计时),
    gate 来自跨会话闸门(正在爬的是不是我 / 我前面还排着几个 / 我能爬的倒计时)。

    公用的时候这三种要分清楚, 否则页面会说假话: 别人在爬时我不会被冷却, 我只是在排队;
    冷却是我自己的风控退避, 跟别人没关系。"""
    cool = _cool_left(sid)
    sc = _scope(sid)
    mode = _ask_mode(sid)
    out = []
    for key, name, plat in CRAWLERS:
        gate = _GATE.view(plat, sc, gap=BILI_GAP.get(mode, 60) if plat == "bilibili" else 0.0)
        running = supervisor.is_running(plat) if supervisor.enabled() else True
        if not running:
            out.append({"key": key, "name": name, "ok": False, "login": "unknown",
                        "running": False, "busy": None, "queries_done": None, "error": None,
                        "cool_left": cool.get(key, 0), "gate": gate})
            continue
        try:
            h = live.health(plat)
        except Exception as e:
            h = {"ok": False, "login": "unknown", "error": repr(e)}
        item = {"key": key, "name": name,
                "ok": bool(h.get("ok")),
                "login": _login_flag(h.get("login")),
                "busy": h.get("busy"), "queries_done": h.get("queries_done"),
                "error": h.get("error"),
                "cool_left": cool.get(key, 0), "gate": gate}
        if supervisor.enabled():
            item["running"] = True
        out.append(item)
    return {"crawlers": out, "measure": live.measure_mode()}


def _spec_of(row):
    """页面上填的那点字段 -> mcp/client 认的服务描述(命令按空格拆, 不认 shell 引号规则 ——
    参数里有空格就整条填进 command 数组里, 页面上也是这么提示的)。"""
    row = row or {}
    transport = "http" if str(row.get("transport") or "").lower() == "http" else "stdio"
    spec = {"name": (row.get("name") or "").strip(), "transport": transport,
            "enabled": str(row.get("enabled", "1")) != "0",
            "env": row.get("env") if isinstance(row.get("env"), dict) else {},
            "headers": row.get("headers") if isinstance(row.get("headers"), dict) else {}}
    if transport == "http":
        spec["url"] = (row.get("url") or "").strip()
    else:
        cmd = row.get("command")
        if isinstance(cmd, str):
            cmd = cmd.split()
        spec["command"] = [str(c) for c in (cmd or []) if str(c).strip()]
        spec["cwd"] = (row.get("cwd") or "").strip()
    return spec


def tools_payload():
    """设置页「工具」那一栏要的东西: 自带工具有哪些/开没开/缺什么 + 外部服务各自提供什么。

    外部服务的工具清单是**真去问一遍**(带 5 分钟缓存) —— 页面上说"这一个能提供 3 个工具"
    就得是真话; 问不到就把原因写在 error 里, 不编一个数。"""
    builtin = [{"name": t["name"], "label": t["label"], "optional": bool(t["optional"]),
                "enabled": tool_registry.enabled(t["name"]), "env": t["env"],
                "needs": t["needs"](), "desc": t["desc"]}
               for t in tool_registry.BUILTIN]
    servers = []
    for spec in settings.mcp_servers():
        item = dict(spec)
        # 页面刷一次就真去连一遍外部服务的话, 一个填错的地址能让整个设置面板卡住一分钟 ——
        # 所以这里**只看缓存**: 没测过的显示"还没测", 由用户点「测试」触发真连。
        tools = mcp_client.peek_tools(spec) if spec.get("enabled") else None
        item["tools"] = (None if tools is None else
                         [{"name": t.get("name"), "description": (t.get("description") or "")[:200]}
                          for t in tools])
        servers.append(item)
    return {"builtin": builtin, "servers": servers,
            "mcp": {"command": "python", "args": ["-m", "harness.mcp"],
                    "cwd": os.path.dirname(HERE)}}


def sessions_payload():
    """后端知道有哪些对话 —— 侧栏的兜底来源。

    侧栏那本账存在浏览器里(localStorage), 那是"这台机器这个浏览器"的记忆: 清一次站点数据、
    换个浏览器/换台机器打开就空了, 可对话内容(答案、记忆)明明都还在后端这本账里 ——
    表现就是"我之前那个会话不见了"。这里按后端再列一遍, 页面拿它并进侧栏。

    只认**对话自己的流水**(flow), 不认证据池里的 scope: 证据是跨会话共用的(见 schema.sql 开头的
    池/记忆之分), 拿它当"对话"会把删掉的对话又从池子里捞回来。

    另回一份 gone = 垃圾桶里那些对话的会话号: 侧栏那本浏览器账是**只补不删**的(见 index.html
    refreshSessions), 删对话时它靠"自己删的那一行自己抹掉"保持一致 —— 一旦删不是从这台浏览器发起的
    (换了台机器删、或后台直接清的), 那一行就永远赖在侧栏里下不去。报给页面, 让它照着抹。"""
    try:
        conn = db.connect()
        try:
            gone = {r["scope"] for r in conn.execute("SELECT scope FROM trash")}
        finally:
            conn.close()
    except Exception:
        gone = set()
    out = []
    for sc, e in flow.index().items():
        if not sc.startswith("web:") or sc in gone:
            continue
        out.append({"id": sc[len("web:"):], "title": e.get("first_q") or "",
                    "at": e.get("last_ts") or "", "asked": e.get("asked") or 0,
                    "unfinished": e.get("unfinished") or 0,
                    # 其中几道是用户自己按的暂停(有 paused 记录、还没被接走)。单列出来是要让
                    # 侧栏能把"还剩几道题搁着"说清楚 —— 搁着的题不会自己跑, 不说用户就想不起来。
                    "paused": len(flow.paused(sc))})
    out.sort(key=lambda s: s["at"], reverse=True)
    return {"sessions": out[:80],
            "gone": sorted(sc[len("web:"):] for sc in gone if sc.startswith("web:"))}


def _bili_raw(path, timeout=10):
    """直接打 bili 服务拿原始字节(二维码是 PNG, 不是 JSON)。失败回 (None, 错误串)。"""
    tok = live._read_token("bilibili")
    if not tok:
        return None, "bili token 读不到"
    req = urllib.request.Request(live._BASE["bilibili"] + path, method="GET",
                                 headers={"X-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])
    except Exception as e:
        return None, repr(e)


def _run_ask(job_id, sid, question, mode, game, ratio=None, cancel=None, emit=None,
             pause=None, resume_from=None):
    """后台跑一题(可能几分钟), 前端轮询 /api/job 取结果 + 增量取实时事件。

    cancel / pause 是这一题的「停止」「暂停」信号(事件对象), 由 /api/ask(或 /api/resume)
    当场建好传进来 —— **不在线程里建**: 前端完全可能在事件建好之前就点了按钮(冷启动那一两秒),
    那时 /api/cancel、/api/pause 得找得到它。
    emit 同理由调用方建好传进来(**不是线程里建**): 引擎会从并发抓取的 worker 线程里推事件,
    所以它得是个线程安全的、当场就能用的东西。"""
    sc = _scope(sid)
    try:
        sess = _session(sid)
        res = sess.ask_turn(question, mode=mode, game=game, nga_ratio=ratio, cancel=cancel,
                            emit=emit, pause=pause, resume_from=resume_from)
        if (res or {}).get("paused"):
            # 暂停: 这一跑**没有答案**。回那张"可续"的卡(前端见到 unfinished 就走简卡),
            # 不 build_ask —— 流水里根本没有这一题的收口记录, 硬拼一张出来就是编。
            pz = flow.paused(sc).get((res or {}).get("qid")) or {}
            return _job_update(job_id, state="done", paused=True, ask={
                "id": "p%s" % re.sub(r"[^\w]", "", str((res or {}).get("qid") or ""))[:14],
                "unfinished": True, "paused": True, "qid": (res or {}).get("qid"),
                "question": question, "mode": mode,
                "rounds": int(pz.get("rounds") or 0), "got": int(pz.get("got") or 0),
                "ts": pz.get("ts") or time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
        recs = flow.read(sc)
        if not recs:
            raise RuntimeError("本轮没有落流水(引擎未收口?)")
        # claim_cid 只在当轮活跃: 前端纠错按钮要用它, 历史题没有(卡已翻篇)
        # **刻意不把 job_id 当卡 id**: job_id 是"这一次请求"的传输号, 卡 id 是"这一题"的名字。
        # 两者一旦绑一起, 同一题当场(用 job_id)和刷新后(从流水按 ts 重建)会得出两个不同的 id ——
        # 页面上的 DOM 锚点对不上不说, PDF 是按卡 id 命名的, 刷新后那张"下载 PDF"就直接消失了。
        ask = webview.build_ask(sess.conn, recs[-1], cid=(res or {}).get("claim_cid"))
        # 这一题实际用的平台比例(前端要如实标出来; 缺省那档没传值就不标, 别编一个数)
        ask["ratio"] = ratio
        # 点过「停止」的那一题: 当场这份也标出来(刷新后靠流水里的同名字段复原, 见 webview.build_ask)
        ask["cancelled"] = bool((res or {}).get("cancelled"))
        _job_update(job_id, state="done", ask=ask)
    except Exception as e:
        # 栈不能吞: 只把 repr 挂给前端的话, 这一题为什么挂就只能靠猜(2026-09-15 真跑吃过一次亏)
        traceback.print_exc()
        _job_update(job_id, state="error", error=repr(e))
    finally:
        # 先把这个 scope 的两个信号摘掉再放锁: 下一条请求进来建的是新事件, 别让旧的那面旗
        # 被后来的人捡到(捡到就变成"一开跑就已经被停了/被暂停了")。
        with _JOBS_LOCK:
            if _CANCELS.get(sc) is cancel:
                _CANCELS.pop(sc, None)
            if _PAUSES.get(sc) is pause:
                _PAUSES.pop(sc, None)
        _session_lock(sc).release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[web] %s | %s" % (self.address_string(), fmt % args), flush=True)

    # ---------- 出参 ----------
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 不让浏览器自己缓存: 静态页是**每次请求现读盘**的(改完 html 不用重启), 可要是不说这一句,
        # 浏览器拿不到 Last-Modified/ETag 就没法判断新旧, 干脆一直拿旧的那份 —— 表现就是
        # 「文件明明改了、刷新了却还是老样子」。二维码图同理(缓存的码早失效了)。本机环回, 重取不要钱。
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _start_job(self, sid, question, mode, game, ratio, resume_from=None, resume_qid=None):
        """开一炮(新题或续爬都一样): 拿会话锁 -> 建两个信号 -> 起线程 -> 回任务号。

        锁在这里 acquire(blocking=False): 同一个对话**同一时刻只跑一题**。拿不到直接回 429,
        让前端把正在跑的那一题接回来(见 /api/running)。
        **暂停的题不占这把锁** —— 暂停意味着那条线程已经收工, 锁也释放了; 所以"一题在跑 +
        几题搁着"是常态, 侧栏那几张暂停卡谁也不会挡着谁。

        resume_qid = 这次续爬接的是哪条暂停记录。**落款必须在拿到锁之后**: 先落款再撞上 429,
        那道题就被标成"已经接走了", 而实际上压根没开跑 —— 侧栏那张卡当场消失, 样本也没人管了。
        """
        sc = _scope(sid)
        if not _session_lock(sc).acquire(blocking=False):
            return self._json(429, {"error": "本题还在跑，等它出结果"})
        if resume_qid:
            flow.append(sc, {"event": "resumed", "qid": resume_qid})
        job_id = uuid.uuid4().hex[:12]
        cancel = threading.Event()          # 这一题的「停止」开关, 见 /api/cancel
        pause = threading.Event()           # 这一题的「暂停」开关, 见 /api/pause
        with _JOBS_LOCK:
            _evict_jobs()
            # 记下"这一炮属于哪个会话、问的是什么": 页面刷新后要靠这两样把正在跑的那一题接回来
            # (见 /api/running —— 题不会因为刷新就没了, 后端这条线程照跑)。
            _JOBS[job_id] = {"state": "running", "events": [],
                             "session": sc, "question": question,
                             "mode": mode, "started": time.time()}
            _CANCELS[sc], _PAUSES[sc] = cancel, pause
            events = _JOBS[job_id]["events"]
        # 引擎的实时事件就往这条列表里塞(取结果仍走 /api/job 的老路)。**不加锁**:
        # list.append 是原子的, 加锁会让引擎每次吐字都去抢那把全局锁, 而 /api/status
        # 每几秒就要拿它一次 —— 没必要为了外观上的整洁给热路径加争用。
        emit = lambda ev: events.append(ev)     # noqa: E731
        threading.Thread(target=_run_ask, args=(job_id, sid, question, mode, game, ratio,
                                               cancel, emit, pause, resume_from),
                         daemon=True).start()
        return self._json(202, {"job": job_id, "scope": sc})

    def _resume(self, sid, qid):
        """「继续」: 接着某道被暂停的题往下爬。

        接的是**原来那道题**: 问句/档位/游戏/平台比例都从那条暂停记录里读回来, 不重新解析 ——
        用户点的是"继续", 不是"另问一遍", 题面要是被重新解析过(比如别名、游戏锚点变了)
        就会变成另一道题, 上次爬到的样本反倒对不上。
        落了 resumed 落款再开跑: 这道题在流水里从此归到新的那次开跑名下, 旧那条暂停记录收摊,
        侧栏不会同时挂着"暂停的"和"续爬的"两张卡。
        """
        sc = _scope(sid)
        pz = flow.paused(sc)
        rec = pz.get(qid) if qid else None
        if rec is None:
            # 没给 qid(或给了个不认识的)但正好只有一条: 就是它。前端存的是页面上的卡 id,
            # 刷新过一版就可能对不上, 而"这个对话只有一道题搁着"时猜错不了。
            if qid or len(pz) != 1:
                return self._json(404, {"ok": False, "error": "这一题不在待续列表里"})
            rec = list(pz.values())[0]
            qid = rec.get("qid")
        return self._start_job(sid, rec.get("question") or "", rec.get("mode"),
                               rec.get("game_hint"), rec.get("nga_ratio"),
                               resume_from=rec, resume_qid=qid)

    def _pdf_make(self, sid, ask_id):
        """现导一份这一题的 PDF(题卡上那个「导出为 PDF」按钮)。

        答完不再自动导: 多数答案没人要纸, 按需导更省。
        导出的是**已经存在库里那一题**(按 id 从本会话的流水里找回来), 不是重新问一遍。
        """
        if not ask_id:
            return self._json(400, {"ok": False, "error": "缺 id"})
        if pdfout.exists(ask_id):
            # 导过就直接给地址: 文件按卡 id 命名, 内容一样, 没必要重排一遍。
            return self._json(200, {"ok": True, "url": "/api/pdf?id=%s" % ask_id, "cached": True})
        try:
            asks = webview.history(_session(sid).conn, _scope(sid), limit=100)
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": repr(e)})
        ask = next((a for a in asks if a.get("id") == ask_id), None)
        if ask is None:
            return self._json(404, {"ok": False, "error": "这一题不在本对话的记录里"})
        try:
            return self._json(200, {"ok": True, "url": pdfout.render(ask)})
        except Exception as e:
            # 导出失败不是"这题答坏了" —— 如实回报原因, 答案本身不动。
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": repr(e)[:300]})

    def _pdf_conv(self, sid, title):
        """把**整个对话**导成一份 PDF(侧栏那一行「⋯」里的「导出为 PDF」)。

        与题卡上那个按钮共用同一套渲染(逐题排下去, 每题照样是正文 + 引用清单), 所以两处
        内容一致, 不会"网页上是好的、纸上不一样"。**不缓存**: 对话会往下长, 每次重导覆盖
        同一份文件 —— 地址上带个时间戳, 免得浏览器把上一版给我们。
        没答过题的对话(比如刚开的那个)直接说清楚, 不生成一份空壳。
        """
        try:
            asks = [a for a in webview.history(_session(sid).conn, _scope(sid), limit=200)
                    if not a.get("unfinished")]
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": repr(e)})
        if not asks:
            return self._json(400, {"ok": False, "error": "这个对话还没有答过的题，没东西可导"})
        try:
            r = pdfout.render_conv(sid, title or "对话", asks)
        except Exception as e:
            # 导出失败不是"这题答坏了" —— 如实回报原因, 答案本身不动。
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": repr(e)[:300]})
        return self._json(200, {"ok": True, "url": r["url"] + "&t=%d" % int(time.time()),
                                "asks": len(asks)})

    def _pdf(self, ask_id):
        """把导出好的那一份 PDF 发出去(文件按题目 id 存在 out/pdf/ 下)。"""
        if not ask_id:
            return self._json(400, {"error": "缺 id"})
        path = pdfout.path_for(ask_id)
        if not os.path.isfile(path):
            return self._json(404, {"error": "还没有这一题的 PDF（导出可能失败了）"})
        with open(path, "rb") as f:
            return self._bytes(200, f.read(), "application/pdf")

    def _cool_guard(self, sid, plat, doing):
        """冷却期挡下修复这条链: 命中就回 429 并返回 True(调用方随即 return 掉), 没命中返回 None。

        注意**不能**写成 `if self._json(...)`: `_json` 只写响应、不返回值, 那样恒为假 ——
        响应看着是 429, 代码却一路往下走, 真去把爬虫拉起来、真去打平台的登录页。
        (这不是假想: 自检里"且没有去拉爬虫"那条断言就是这么抓出来的。)"""
        note = _cool_note(sid, plat, doing)
        if not note:
            return None
        self._json(429, {"error": note, "cool_left": _cool_secs(sid, plat)})
        return True

    def _forget(self, sid, title=None):
        """删掉一个对话的记忆(侧栏那一行的「删除」)。三件事缺一不可, 少一件就会"删了又活过来":

        ① **先拿到这个 scope 的锁** —— 正在跑题的会话拒删: 那一轮还在内存里, 收口时会把流水又写回来,
           等于白删; 顺便把「取锁 → 丢缓存 → 清库」做成一件不可插队的事。
        ② **把内存里缓存的那个 Session 丢掉** —— 它的状态卡/结论编号就在内存里, 不丢的话下次进这个
           会话照样接着旧卡答(库里搬空了也没用)。
        ③ **库里按 scope 整段搬进垃圾桶 + 挪走流水文件**(见 forget.trash_move)。证据池一行不动。

        搬进垃圾桶 = 7 天内可恢复(设置页那个「垃圾桶」); 过了 7 天由 sweep 真删。
        连接: 缓存里有就借它那条(顺手关掉, 这个 Session 已经不要了); 没有就现开一条用完关掉。"""
        sc = _scope(sid)
        lk = _session_lock(sc)
        if not lk.acquire(blocking=False):
            return self._json(409, {"ok": False, "error": "这个对话有题正在跑，等它答完再删"})
        stat = None
        try:
            with _JOBS_LOCK:
                sess = _SESSIONS.pop(sc, None)
            conn = sess.conn if sess is not None else db.connect()
            try:
                stat = forget.trash_move(conn, sc, title=title)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": repr(e)})
        finally:
            lk.release()
        return self._json(200, {"ok": True, "scope": sc, "cleared": stat["cleared"],
                                "expire_at": stat["expire_at"], "days": forget.TRASH_DAYS})

    def _trash(self):
        """垃圾桶里还有什么(设置页那一栏)。**先扫过期再列** —— 列表里不该出现已经过期的条目。"""
        try:
            conn = db.connect()
        except Exception as e:
            return self._json(500, {"error": repr(e)})
        try:
            forget.sweep(conn)
            return self._json(200, {"items": forget.trash_list(conn), "days": forget.TRASH_DAYS})
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": repr(e)})
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _restore(self, sid):
        """把垃圾桶里的对话捡回来(设置页那个「恢复」)。

        跟删一样要拿这个 scope 的锁: 恢复的当口不能有题在跑(否则它收口时落的那张卡会跟搬回来的那张打架)。
        搬回来之后**必须把缓存里的 Session 也丢掉**: 那上面挂着一张开会话时建的**空卡**,
        不丢的话下次进这个对话照样接着空卡答 —— 库里恢复了也白恢复(这一条是删那侧的镜像, 漏了就前功尽弃)。
        """
        sc = _scope(sid)
        lk = _session_lock(sc)
        if not lk.acquire(blocking=False):
            return self._json(409, {"ok": False, "error": "这个对话有题正在跑，等它答完再恢复"})
        stat = None
        try:
            with _JOBS_LOCK:
                sess = _SESSIONS.pop(sc, None)
            conn = sess.conn if sess is not None else db.connect()
            try:
                stat = forget.trash_restore(conn, sc)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": repr(e)})
        finally:
            lk.release()
        if stat is None:
            return self._json(404, {"ok": False, "error": "垃圾桶里没有这个对话（可能已经过期清掉了）"})
        return self._json(200, {"ok": True, "scope": sc, "restored": stat})

    # ---------- 入口 ----------
    def _admin_blocked(self, path):
        """这一条是不是"只许本机/内网调"的管理面, 而调用方来自外面。"""
        return path in _ADMIN_PATHS and not _is_local(self.client_address[0])

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if self._admin_blocked(u.path):
            return self._json(403, {"error": "这一条只允许本机/内网调用"})
        if u.path == "/api/status":
            # session 仍然收(前端照旧带着), 但冷却是全机器一份, 与它无关(见 _cool_secs)
            return self._json(200, status_payload(q.get("session")))
        if u.path == "/api/settings":
            return self._json(200, settings.payload())
        if u.path == "/api/tools":
            try:
                return self._json(200, tools_payload())
            except Exception as e:
                return self._json(500, {"error": repr(e)})
        if u.path == "/api/pdf":
            return self._pdf(q.get("id") or "")
        if u.path == "/api/trash":
            return self._trash()
        if u.path == "/api/sessions":
            # 侧栏的兜底清单(见 sessions_payload): 浏览器那本账没了也照样列得出来
            try:
                return self._json(200, sessions_payload())
            except Exception as e:
                return self._json(500, {"error": repr(e)})
        if u.path == "/api/history":
            sid = q.get("session") or "anon"
            try:
                return self._json(200, {"asks": webview.history(_session(sid).conn, _scope(sid))})
            except Exception as e:
                return self._json(500, {"error": repr(e)})
        if u.path == "/api/running":
            # 刷新/重开页面时用: 这个对话有没有题正在跑? 有就把**已经发生的实时事件整段**给回去,
            # 页面照着重放就能接回原来的样子(爬取步骤、正文草稿、累计消耗一个不少)。
            jid, info, evs = _running_job(q.get("session") or "anon")
            if not jid:
                return self._json(200, {"running": False})
            return self._json(200, {"running": True, "job": jid,
                                    "question": info.get("question"), "mode": info.get("mode"),
                                    "started": info.get("started"),
                                    "events": evs, "cursor": len(evs)})
        if u.path == "/api/job":
            # since = 前端已经拿到第几条事件(游标), 只回它后面那截 —— 事件正文可能几十 KB,
            # 每 0.7 秒整份重发一次纯属白烧流量, 而页面上只要增量。
            try:
                since = max(0, int(q.get("since") or 0))
            except (TypeError, ValueError):
                since = 0
            with _JOBS_LOCK:
                job = _JOBS.get(q.get("id") or "")
                if job is None:
                    return self._json(404, {"error": "no such job"})
                evs = list(job.get("events") or [])
                out = dict((k, v) for k, v in job.items() if k != "events")
            out["events"] = evs[since:]
            out["cursor"] = len(evs)
            return self._json(200, out)
        if u.path == "/api/login/bili/qr":
            # 服务端那边码要等页面渲染, 给足 40s(默认 10s 会半路断掉)
            body, err = _bili_raw("/login/qr/image", timeout=40)
            if err:
                return self._json(502, {"error": err})
            return self._bytes(200, body, "image/png")
        if u.path == "/api/login/bili/state":
            return self._json(200, live._get_path("bilibili", "/login/qr/state"))
        if u.path == "/api/login/nga/state":
            # NGA 的码是页面现画的 data URI, 直接跟着状态一起回, 不用单独取图
            return self._json(200, live._get_path("nga", "/login/qr/state"))
        return self._static(u.path)

    def do_POST(self):
        u = urlparse(self.path)
        # 请求体一次读干净: HTTP/1.1 keep-alive, 留着不读会把下一条请求读串
        # (实测表现: 点「去修复」后第一次查状态回 501 —— 那条连接上残留了 "{}")。
        b = self._body()
        # 只收 application/json: 跨站的 <form> 能凑出"看着像 JSON"的请求体打到本机这个端口,
        # 光看来源地址挡不住(浏览器是从本机发的)。要求这个头等于要求一次预检, 而本服务不应答
        # OPTIONS —— 于是跨站表单这条路直接断掉。页面自己每次都带这个头(见 index.html 的 api())。
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"error": "只收 application/json"})
        if self._admin_blocked(u.path):
            return self._json(403, {"error": "这一条只允许本机/内网调用"})
        if u.path == "/api/ask":
            question = (b.get("question") or "").strip()
            if not question:
                return self._json(400, {"error": "空问题"})
            sid = b.get("session") or "anon"
            return self._start_job(sid, question, b.get("mode"), b.get("game"), b.get("ratio"))
        if u.path == "/api/pause":
            # 用户点「暂停」: 与「停止」同款 —— 只置一个旗, **绝不拿会话锁、绝不阻塞**
            # (锁正被那一题握着, 要等锁才生效的"暂停"等于没有)。
            # 与「停止」的区别在这面旗落到引擎里之后: 暂停不落答案, 那题留成"还没答"等继续。
            sc = _scope(b.get("session") or "anon")
            with _JOBS_LOCK:
                ev = _PAUSES.get(sc)
            if ev is None:
                return self._json(200, {"ok": False, "error": "这个对话现在没有正在跑的题"})
            ev.set()
            return self._json(200, {"ok": True, "note": "已发出暂停信号：取完手上这一次就搁着"})
        if u.path == "/api/resume":
            return self._resume(b.get("session") or "anon", b.get("qid") or "")
        if u.path == "/api/discard":
            # 「丢弃这题」: 只是给流水补一条"用户不要了"的落款 —— 已经爬到的样本**不删**
            # (它们在库里是这台机器的既有存档, 删了下次别人问同一个话题就得重爬一遍)。
            # 落款之后这题不再算"没跑完", 侧栏那张卡也就跟着撤了(见 flow._closed_qids)。
            sid = b.get("session") or "anon"
            qid = (b.get("qid") or "").strip()
            if not qid:
                return self._json(400, {"error": "缺 qid"})
            if qid not in flow.paused(_scope(sid)):
                return self._json(404, {"error": "这一题不在待续列表里"})
            flow.append(_scope(sid), {"event": "discarded", "qid": qid})
            return self._json(200, {"ok": True})
        if u.path == "/api/cancel":
            # 用户点「停止」: 只置一个旗, **绝不拿会话锁、绝不阻塞** —— 一个要等锁才能生效的
            # "停止"按钮等于没有(锁正被那一题握着)。
            sc = _scope(b.get("session") or "anon")
            with _JOBS_LOCK:
                ev = _CANCELS.get(sc)
            if ev is None:
                return self._json(200, {"ok": False, "error": "这个对话现在没有正在跑的题"})
            ev.set()
            # 说明清"不是立刻断": 引擎在下一个安全点收手(已经发出去的那次抓取会跑完并落库),
            # 所以界面上要接着等它收口, 而不是当场把卡片当失败掉。
            return self._json(200, {"ok": True, "note": "已发出停止信号：取完手上这一次就收口"})
        if u.path == "/api/pdf":
            # 按需导出(题卡上那个按钮): 现渲染一份再回下载地址。取图那条 GET /api/pdf 只管发文件。
            return self._pdf_make(b.get("session") or "anon", b.get("id") or "")
        if u.path == "/api/pdf/conv":
            # 侧栏对话行那个「导出为 PDF」: 整个对话一份。
            return self._pdf_conv(b.get("session") or "anon", b.get("title") or "")
        if u.path == "/api/settings":
            return self._json(200, settings.save(b.get("values") or {}))
        if u.path == "/api/model":
            # 再去问一次服务端「你有哪些模型」(设置页那个「重新探测」)。**只有用户点了才联网** ——
            # 打开设置面板读的是 peek_model() 里的缓存, 不让面板卡在网络等待上。
            m, err = settings.detect_model(force=True)
            return self._json(200, {"ok": not err, "error": err, "model": m})
        if u.path == "/api/mcp":
            # 外部工具服务的增删改查 + 「测试」.
            act = (b.get("action") or "").strip()
            if act == "save":
                rows = [_spec_of(r) for r in (b.get("servers") or []) if (r or {}).get("name")]
                settings.save_mcp_servers(rows)
                return self._json(200, {"ok": True, "tools": tools_payload()})
            if act == "test":
                spec = _spec_of(b.get("server") or {})
                if not spec.get("name"):
                    return self._json(400, {"ok": False, "error": "先给它起个名字"})
                tools, err = mcp_client.probe(spec)
                return self._json(200, {"ok": not err, "error": err, "tools": tools})
            return self._json(400, {"ok": False, "error": "不认识的动作: %s" % act})
        if u.path == "/mcp":
            # 同一套 MCP 处理逻辑, 换成 HTTP 那条路送进来(走 HTTP 的客户端用这个地址)。
            code, obj = mcp_server.handle_http(json.dumps(b, ensure_ascii=False).encode("utf-8"))
            return self._json(code, obj)
        if u.path == "/api/correct":
            sess = _session(b.get("session") or "anon")
            ok, msg = sess.correct((b.get("cid") or "").strip(), b.get("concl") or "")
            return self._json(200 if ok else 400, {"ok": ok, "msg": msg})
        if u.path == "/api/forget":
            return self._forget(b.get("session") or "anon", b.get("title"))
        if u.path == "/api/restore":
            return self._restore(b.get("session") or "anon")
        # 扫码/重探都得爬虫活着(码是在它自己那个常驻浏览器里出的) —— 先按需把它拉起来。
        # 冷启动要拉一次浏览器, 所以这几条会等十几秒, 属正常。
        # 冷却期内**硬挡**修复这条链, 且挡在 supervisor.ensure **前面** —— 连爬虫带浏览器都不必拉起来,
        # 更不必去打平台的登录页(风控退避期里再碰它, 正是要避免的事)。
        # 对两个平台一视同仁地查: NGA 眼下不冷却(只熔断), 但这条规则是结构性的, 不靠"记得给新平台加"。
        if u.path == "/api/login/bili":
            sid = b.get("session") or "anon"
            if self._cool_guard(sid, "bilibili", "扫码"):
                return
            supervisor.ensure("bilibili")
            return self._json(200, live._post_path("bilibili", "/login/qr/start", {}, timeout=20))
        if u.path == "/api/login/nga":
            sid = b.get("session") or "anon"
            if self._cool_guard(sid, "nga", "重探登录态"):
                return
            supervisor.ensure("nga")
            return self._json(200, live._post_path("nga", "/login/recheck", {}, timeout=60))
        if u.path == "/api/login/nga/qr":
            sid = b.get("session") or "anon"
            if self._cool_guard(sid, "nga", "扫码"):
                return
            supervisor.ensure("nga")
            return self._json(200, live._post_path("nga", "/login/qr/start", {}, timeout=20))
        return self._json(404, {"error": "not found"})

    # ---------- 静态页 ----------
    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(full):
            return self._json(404, {"error": "not found"})
        ctype = _MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as f:
            return self._bytes(200, f.read(), ctype)


class _QuietServer(ThreadingHTTPServer):
    """浏览器中途关连接(轮询取消/换页)是常态, 别让它刷 traceback 把真日志淹掉。
    只吞连接类异常, 其他照旧打出来。"""
    def handle_error(self, request, client_address):
        import sys
        et = sys.exc_info()[0]
        if et is not None and issubclass(et, (ConnectionResetError, ConnectionAbortedError,
                                              BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def _startup_sweep():
    """起服务时先扫一遍垃圾桶里过期的(过期=物理清除, 这一步不做就永远留着)。

    只靠启动扫是不够的 —— 服务可能连开好几天, 所以 /api/trash 每次列之前也扫一遍。
    清扫失败不算致命(库还没建/被别处锁着都可能), 打一句就接着起服务。"""
    try:
        conn = db.connect()
    except Exception as e:
        print("[web] 垃圾桶清扫跳过(开不了库): %r" % (e,), flush=True)
        return
    try:
        n = forget.sweep(conn)
        if n:
            print("[web] 垃圾桶清扫: %d 条过期的已物理删掉" % n, flush=True)
    except Exception as e:
        print("[web] 垃圾桶清扫跳过: %r" % (e,), flush=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="harness 网页后端")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    _startup_sweep()                   # 垃圾桶里过期的先清掉(见 forget.sweep)
    supervisor.start_reaper()          # 闲置的爬虫由它定期收掉
    srv = _QuietServer((args.host, args.port), Handler)
    print("[web] harness 网页后端 listening http://%s:%d" % (args.host, args.port), flush=True)
    if supervisor.enabled():
        print("[web] 爬虫按需拉起, 闲置 %d 秒自动关(登录态在磁盘上, 重开不用重扫)"
              % supervisor.IDLE_SEC, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        supervisor.shutdown_all()      # 退出时把还活着的爬虫带走, 别留孤儿浏览器


if __name__ == "__main__":
    main()
