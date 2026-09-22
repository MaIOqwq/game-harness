# -*- coding: utf-8 -*-
"""「刷新之后还找得回我的对话吗」自检(离线, 不烧模型不打平台): python _syscheck/verify_sessions.py

盯的是用户 2026-09-17 报的那件事:「我刷新浏览器还是看不到我之前那个会话啊」。
两个洞都能静默发生, 页面上看不出来, 所以逐条钉死:

  ① 流水读口 —— 「开跑」标记不许混进"题"里(混进去的话引擎拼前几轮上下文会带上一条没答案的,
                 webview 会画出一张空卡, 既有自检里"这题是不是被停的"断言也会被带偏);
  ② 真跑一题 —— 一题落两条: 开头那条"开跑"(只有问句)、收口那条(有答案), 共用一个 qid;
  ③ 断了的那题 —— 进程在收口前没了, 这一题仍要在库里留下"问过什么", 且认得出来"没跑完";
  ④ 侧栏清单 —— 后端要能自己列一遍对话(浏览器那本账清了/换了台机器也找得回来);
  ⑤ 前端接线 —— 接口通但页面没接线, 用户那儿就是"没这功能"(这条吃过亏)。
"""
import json
import os
import re
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8")

os.environ["HARNESS_FLOW"] = "1"
os.environ.pop("HARNESS_LIVE", None)          # 别让引擎真去连爬虫
_TMP = tempfile.mkdtemp(prefix="sesschk_")
# webapp 会按 db.DEFAULT_DB 开库 —— 必须在 import db **之前**把它指走, 否则自检会往用户自己的
# 记忆库里塞假对话(自检污染产品数据, 比不跑还糟)。
os.environ["HARNESS_DB"] = os.path.join(_TMP, "web.db")

from harness import db, flow, webview                                   # noqa: E402
from harness import engine as E                                          # noqa: E402
from harness.runner.session import Session                               # noqa: E402

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


# ---------------------------------------------------------------- ① 流水读口
print("=== 1. 「开跑」标记不许混进'题'里 ===")
flow.append("web:A", {"event": "start", "qid": "q1", "question": "第一问", "mode": "快速"})
flow.append("web:A", {"qid": "q1", "mode": "快速", "question": "第一问", "concl": "结论: 甲",
                      "answer": "答案正文 [id=1]", "ev": [], "trace": [], "rounds": 1})
chk("1.1 records() 只给'题'(开跑那条滤掉) —— 既有自检按题数断言, 这条一变全都会假 FAIL",
    [r.get("question") for r in flow.records("web:A")] == ["第一问"],
    [r.get("question") for r in flow.records("web:A")])
chk("1.2 starts() 才是开跑标记", [r.get("qid") for r in flow.starts("web:A")] == ["q1"])
chk("1.3 收口了的不算'没跑完'", flow.pending("web:A") == [], flow.pending("web:A"))
chk("1.4 read() 同样只给'题'(回放/核引/scoring 都不该看见没答案的那种)",
    len(flow.read("web:A")) == 1, flow.read("web:A"))

# 老流水: 那时还没有 qid 这个字段 —— 不许把历史题误报成"没跑完"
flow.append("web:B", {"mode": "快速", "question": "老题", "concl": "结论: 乙", "answer": "x"})
chk("1.5 没有 qid 的老题不当'没跑完'(不然历史对话一开页面全是红字)",
    flow.pending("web:B") == [], flow.pending("web:B"))

# ---------------------------------------------------------------- ② 真跑一题
print("=== 2. 真跑一题: 开头落'开跑', 收口落答案, 两条共用一个 qid ===")


class FakeCrawl:
    def fetch(self, ctx, params):
        return [{"id": "fake"}]

    def commit(self, ctx, params, got):
        return {"summary": {"run_id": 1, "new": 2, "dup": 0, "leak": 0, "stale": 0, "shown": 2,
                            "game": params.get("game"), "platform": params.get("platform"),
                            "query": params.get("query"), "total_obs": 2},
                "evidence": []}

    def official(self, ctx, params):
        raise AssertionError("这题没调官号探针")


def run_ask(scope, question, script=None):
    conn = db.connect(os.path.join(_TMP, "db", re.sub(r"\W", "_", scope) + ".db"))
    db.init(conn)
    s = Session(conn=conn, provider=lambda **k: [], scope=scope)
    E.crawl_tools = FakeCrawl()
    E.tool_registry = types.SimpleNamespace(BY_NAME={}, enabled=lambda n: True, defs=lambda: [])
    state = {"i": 0}
    steps = script or []

    def _llm(self, messages, tools=True, stream=False):
        i = state["i"]
        state["i"] += 1
        msg = steps[i] if i < len(steps) else {"content": "本轮证据有限。\n结论: 证据不足以坐实。"}
        return {"choices": [{"message": msg}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    s.engine._llm = types.MethodType(_llm, s.engine)
    s.engine._ext_tools = lambda: {}
    return s, s.ask_turn(question, game="鸣潮")


ask_scope = "web:真跑"
_s, res = run_ask(ask_scope, "鸣潮这版本怎么样")
chk("2.1 一题落两条: 开跑 + 收口", len(flow._lines(flow.fpath(ask_scope))) == 2,
    [r.get("event") or "finish" for r in flow._lines(flow.fpath(ask_scope))])
_qids = [r.get("qid") for r in flow._lines(flow.fpath(ask_scope))]
chk("2.2 两条共用一个 qid(靠它才认得出哪次开跑没回来)", _qids[0] and _qids[0] == _qids[1], _qids)
chk("2.3 题数还是 1(开跑标记不算题)", len(flow.records(ask_scope)) == 1)
chk("2.4 跑完的题不进'没跑完'", flow.pending(ask_scope) == [], flow.pending(ask_scope))
_ix = flow.index().get(ask_scope) or {}
chk("2.5 index(): asked=1 / unfinished=0 / 题名就是问句 / 带最后动过的时候",
    _ix.get("asked") == 1 and _ix.get("unfinished") == 0
    and _ix.get("first_q") == "鸣潮这版本怎么样" and bool(_ix.get("last_ts")), _ix)

# ---------------------------------------------------------------- ③ 断了的那题
print("=== 3. 跑到一半进程没了: 那一题仍要留下'问过什么' ===")
flow.append("web:断", {"event": "start", "qid": "zz", "question": "官号探针:崩坏星穹铁道",
                       "mode": "快速", "started_at": "2026-09-17T00:38:18"})
chk("3.1 只有开跑、没有收口 -> 认得出来没跑完", len(flow.pending("web:断")) == 1)
_ix = flow.index().get("web:断") or {}
chk("3.2 这个对话照样进索引(哪怕一条答案都没有) —— 不然它整段消失, 正是用户报的那件事",
    bool(_ix) and _ix.get("asked") == 0 and _ix.get("unfinished") == 1, _ix)
_p = webview.build_pending(flow.pending("web:断")[0])
chk("3.3 翻给前端的形状: unfinished 标记 + 问句原话",
    _p.get("unfinished") is True and _p.get("question") == "官号探针:崩坏星穹铁道"
    and _p.get("started_at") == "2026-09-17T00:38:18", _p)
chk("3.4 没有答案/统计/引用这些字段(后端一条都没有, 编不出来也不该编)",
    "answer" not in _p and "stats" not in _p and "cites" not in _p, sorted(_p))

# 混在一起时: 历史接口要按时刻排好, 不能把没跑完的一律甩到最前/最后
flow.append("web:混", {"event": "start", "qid": "m1", "question": "第一问", "mode": "快速"})
flow.append("web:混", {"qid": "m1", "mode": "快速", "question": "第一问", "concl": "结论: 一",
                       "answer": "答案", "ev": [], "trace": [], "rounds": 1})
flow.append("web:混", {"event": "start", "qid": "m2", "question": "第二问", "mode": "快速"})
_conn = db.connect()
_hist = webview.history(_conn, "web:混")
chk("3.5 历史 = 答过的题 + 没跑完的那题, 按时刻排",
    [a.get("question") for a in _hist] == ["第一问", "第二问"]
    and _hist[-1].get("unfinished") is True, [a.get("question") for a in _hist])

# ---------------------------------------------------------------- ④ 侧栏清单
print("=== 4. /api/sessions: 后端自己列一遍对话 ===")
from harness import webapp                                            # noqa: E402  (会 apply_to_env, 放在临时库定好之后)

flow.append("exp:calib:q1", {"mode": "快速", "question": "标定题", "answer": "x", "ev": []})
_c = db.connect()
db.init(_c)
_c.execute("INSERT OR REPLACE INTO trash(scope,title,deleted_at,expire_at) VALUES(?,?,?,?)",
           ("web:删过的", "删过的对话", 0, 1))
_c.commit()
_c.close()

_sp = webapp.sessions_payload()["sessions"]
_ids = [s["id"] for s in _sp]
chk("4.1 只列网页对话(标定/实验的 scope 不进侧栏)",
    all(not i.startswith("exp") for i in _ids) and "真跑" in _ids, _ids)
chk("4.2 垃圾桶里的那个不列(不然删掉了还挂在侧栏)",
    "删过的" not in _ids, _ids)
chk("4.3 没跑完的那个也列得出来, 且带着没跑完的条数",
    any(s["id"] == "断" and s["unfinished"] == 1 for s in _sp),
    [(s["id"], s["unfinished"]) for s in _sp])
_ats = [s["at"] for s in _sp]
chk("4.4 按最后动过的时候倒序", _ats == sorted(_ats, reverse=True), _ats)
chk("4.5 题名照流水里最早那句问话给(不是让用户自己认会话号)",
    (dict((s["id"], s["title"]) for s in _sp).get("真跑")) == "鸣潮这版本怎么样",
    dict((s["id"], s["title"]) for s in _sp))

# ---------------------------------------------------------------- ⑤ 前端接线
print("=== 5. 页面那几处接线还在 ===")
html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
chk("5.1 启动时先画本地那本、再用后端那份补齐",
    re.search(r"paintEmpty\(\);[\s\S]{0,90}renderSessions\(\);[\s\S]{0,90}refreshSessions\(\);"
              r"[\s\S]{0,90}refreshStatus", html)
    is not None, "")
chk("5.2 refreshSessions 打的是真接口 /api/sessions",
    re.search(r'api\("/api/sessions"\)', html) is not None, "")
chk("5.3 后端给的是本地钟点、本机记的是 UTC —— 并进来时要转成同一种写法(否则排序差一个时区)",
    "new Date(s.at)" in html and "toISOString()" in html, "")
chk("5.4 没答上来的那题走另一张简卡(不拿空卡冒充答案)",
    re.search(r"function renderUnfinished", html) is not None
    and re.search(r"if\(ask\.unfinished\) return renderUnfinished\(ask\)", html) is not None, "")
chk("5.5 它没有统计/引用可画, renderAll 要跳过它的图表(跳不过去就是当场报错白屏)",
    re.search(r"if\(!a\.unfinished\) renderCharts\(a\)", html) is not None, "")
chk("5.6 「会话时间轴」已按用户要求整个删掉(一题一行那条, 连拼装函数和画图函数一起)",
    "sessionTimelineBlock" not in html and "sess-tl" not in html and ".sesstl" not in html, "")

# ---------------------------------------------------------------- ⑥ 合并那一步真跑一遍
# 接口通 ≠ 功能通(这条吃过亏): /api/sessions 回得再对, 前端那步并进侧栏要是没生效 / 字段名对不上 /
# 时刻写法不统一, 用户那儿就是"还是看不到我之前那个会话"。所以把那段函数抠出来, 在 node 里喂真形状的数据跑。
print("=== 6. 「并进侧栏」那一步真跑一遍(node, 喂后端真形状的数据) ===")
import shutil                                                          # noqa: E402
import subprocess                                                      # noqa: E402

_NODE = shutil.which("node")
if not _NODE:
    chk("6.0 本机没有 node, 这一段跳过(不记失败)", True, "skip")
else:
    _api = {"sessions": [
        {"id": "old1", "title": "后端知道的旧对话", "at": "2026-09-16T10:00:00", "asked": 3,
         "unfinished": 0},
        # 本机这条名字还是空的(题刚问出去那会儿建的) -> 要用后端的名字补上
        {"id": "mine", "title": "后端给的名字", "at": "2026-09-16T16:00:00", "asked": 1,
         "unfinished": 0},
        {"id": "dead", "title": "问了没跑完", "at": "2026-09-17T00:38:18", "asked": 0,
         "unfinished": 1},
    ]}
    _js = r"""
const fs=require("fs");
const html=fs.readFileSync(process.argv[2],"utf8");
function grab(name){
  const at=html.indexOf("function "+name+"(");
  if(at<0) throw new Error("找不到函数 "+name);
  let i=html.indexOf("{",at), depth=0, j=i;
  for(;j<html.length;j++){
    if(html[j]==="{") depth++;
    else if(html[j]==="}"){ depth--; if(depth===0){ j++; break; } }
  }
  return html.slice(at,j);
}
const SRC=["sidFind","sidOrder","registryPut","refreshSessions"].map(grab).join("\n");
new Function("fs","process", `
/* orphan = 早先版本留下的空壳行: 没名字、后端不认识、也不是当前这条 —— 该被扫掉 */
let SIDS=[{id:"mine",title:"",at:"2026-09-16T17:00:00.000Z"},
          {id:"orphan",title:"",at:"2026-09-15T00:00:00.000Z"}];
let SID="mine";
const localStorage={setItem:function(){}};
let paints=0;
function renderSessions(){ paints++; }
function api(path){
  if(path!=="/api/sessions") throw new Error("refreshSessions 打了别的接口: "+path);
  return Promise.resolve(JSON.parse(fs.readFileSync(process.argv[3],"utf8")));
}
` + SRC + `
refreshSessions().then(function(){
  console.log(JSON.stringify({ids:SIDS.map(function(s){return s.id;}),
                              titles:SIDS.map(function(s){return s.title;}),
                              ats:SIDS.map(function(s){return s.at;}), paints:paints}));
});
`)(fs, process);
"""
    _jsf = os.path.join(_TMP, "merge.js")
    _apif = os.path.join(_TMP, "api.json")
    open(_jsf, "w", encoding="utf-8").write(_js)
    open(_apif, "w", encoding="utf-8").write(json.dumps(_api, ensure_ascii=False))
    _p = subprocess.run([_NODE, _jsf, os.path.join(ROOT, "web", "index.html"), _apif],
                        capture_output=True, text=True, encoding="utf-8", timeout=60)
    if _p.returncode != 0 or not (_p.stdout or "").strip():
        chk("6.0 这一段跑得起来", False, (_p.stderr or "")[-500:])
    else:
        _o = json.loads(_p.stdout.strip().splitlines()[-1])
        chk("6.1 后端知道、本机没有的补进来了; 本机那条没变成两行",
            sorted(_o["ids"]) == ["dead", "mine", "old1"]
            and _o["ids"].count("mine") == 1, _o["ids"])
        chk("6.2 本机那条名字还空着 -> 用后端给的补上",
            _o["titles"][0] == "后端给的名字", _o["titles"])
        chk("6.3 后端给的是本地钟点(尾上没时区), 并进来要转成本机那种写法(带 Z)",
            all(str(a).endswith("Z") for a in _o["ats"]), _o["ats"])
        chk("6.4 排在一起按**真实时刻**排 —— 不换算的话 09-17 那条会排到 UTC 的 09-16 前面(差一个时区)",
            _o["ids"] == ["mine", "dead", "old1"], _o["ids"])
        chk("6.5 补完重画一次侧栏(不然数据进来了页面还是旧的)",
            _o["paints"] == 1, _o["paints"])
        chk("6.6 本机那行名字空、后端又不认识、也不是当前这条 -> 扫掉(不然刷新一次多挂一行「新对话」)",
            "orphan" not in _o["ids"], _o["ids"])
        chk("6.7 当前这条哪怕没名字也留着 —— 刚点开还没问出口的新对话不能被自己扫掉",
            "mine" in _o["ids"], _o["ids"])

print("")
if fails:
    print("汇总: %d 项没过" % len(fails))
    for f in fails:
        print("   " + f)
    sys.exit(1)
print("汇总: 全部通过(%d 项)" % total)
