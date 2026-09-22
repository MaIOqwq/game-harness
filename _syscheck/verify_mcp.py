# -*- coding: utf-8 -*-
"""MCP 那条线自己验自己（离线，不碰网络、不碰爬虫）。

验四件事：
  1. 自带工具注册表：工具面能组出来、开关真的能把工具摘掉、可选工具缺依赖时自己让位；
  2. MCP 服务端：initialize / tools/list / tools/call / 通知 / 不认识的 method，各回什么；
  3. MCP 客户端：拿**我们自己的服务端**当外部服务接一遍（一次把两侧都验了）；
  4. web 后端要的 /api/tools 形状对不对。

跑：python _syscheck/verify_mcp.py
"""
import io
import json
import os
import sys

try:                                    # Windows 控制台默认 GBK, 中文会变乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("HARNESS_LIVE", "1")

from harness import settings                      # noqa: E402
settings.apply_to_env()
from harness.mcp import client as mc              # noqa: E402
from harness.mcp import protocol as P             # noqa: E402
from harness.mcp import server as ms              # noqa: E402
from harness.tools import registry                # noqa: E402

FAIL = []


def ok(cond, label, extra=""):
    print("  [%s] %s%s" % ("ok" if cond else "FAIL", label, ("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(label)


def rpc(method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return ms.handle(msg)


print("=== 1. 自带工具注册表 ===")
names = [t["name"] for t in registry.BUILTIN]
print("  工具: %s" % "、".join(names))
ok("crawl_nga" in names and "crawl_bili" in names, "两个爬虫各是一件工具")
ok("search_archive" in names, "翻记忆是一件工具")
ok("nlp_score" in names, "情感打分是一件工具")
nlp = registry.BY_NAME["nlp_score"]
ok(nlp["optional"] is True, "情感打分标了「可选」")
ok(registry.BY_NAME["crawl_nga"]["optional"] is False, "爬虫不是可选项")

d = registry.defs()
ok(all(set(x) == {"type", "function"} for x in d), "defs() 是 function-calling 的形状")
ok(all(set(x["function"]) >= {"name", "description", "parameters"} for x in d),
   "每个定义有名字/说明/参数表")
m = registry.mcp_tools()
ok(all(set(x) >= {"name", "description", "inputSchema"} for x in m), "mcp_tools() 是 MCP 的形状")
ok(set(x["function"]["name"] for x in d) == set(x["name"] for x in m),
   "两种形状来自同一份清单, 名字一致")
ok(len(m) == len(set(x["name"] for x in m)), "没有重名")

# 开关：关掉一个, 它就整个从工具面上消失（结构防线, 不是靠提示词求模型别用）
os.environ["TOOL_BILI"] = "0"
try:
    d2 = [x["function"]["name"] for x in registry.defs()]
    ok("crawl_bili" not in d2, "关掉 B站爬虫后, 它从工具面上消失了")
    ok("crawl_nga" in d2, "关一个不牵连另一个")
    ok(registry.enabled("crawl_bili") is False, "enabled() 如实说它关着")
    r = registry.call({}, "crawl_bili", {})
    ok(bool(r.get("error")), "关着的工具被直接调用 -> 回一句人话错误, 不真去爬", str(r)[:80])
finally:
    os.environ["TOOL_BILI"] = "1"
ok(registry.enabled("crawl_bili") is True, "开回来就又能用了")

# 注册表里有、引擎里没人认领 = 模型看得见却调不动（一调回 "unknown tool"）。这条能挡住
# 「新加一件工具忘了接线」——加进注册表只是让模型看得见, 还得有个分支真去跑它。
from harness.tools import crawl_live               # noqa: E402
_src = io.open(os.path.join(ROOT, "harness", "engine.py"), encoding="utf-8").read()
for _n in registry.BY_NAME:
    # 两条爬虫走 CRAWL_TOOLS 那张表(名字不写死在引擎里), 其余靠引擎里的字面分支
    _ok = (_n in crawl_live.TOOL_BY_NAME) or (('"%s"' % _n) in _src)
    ok(_ok, "引擎里认领了 %s（不会出现「看得见、调不动」）" % _n)

mode = __import__("harness.sources.live", fromlist=["x"]).measure_mode()
print("  本机打分服务: %s" % mode)
ok(registry.enabled("nlp_score") == (mode != "keyword"),
   "情感打分能不能用, 跟着本机有没有打分服务走")
if mode == "keyword":
    ok("nlp_score" not in [x["function"]["name"] for x in registry.defs()],
       "没有打分服务时, 它不在工具面上(而不是在、但一调就报错)")
    ok(bool(nlp["needs"]()), "needs() 给了一句能看懂的原因: %s" % nlp["needs"]())

print("=== 2. MCP 服务端（消息层）===")
r = rpc("initialize", {"protocolVersion": P.LATEST_PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "check", "version": "0"}})
res = (r or {}).get("result") or {}
ok(res.get("protocolVersion") == P.LATEST_PROTOCOL, "握手回了协议版本")
ok((res.get("serverInfo") or {}).get("name") == P.SERVER_NAME, "握手回了服务名")
ok("tools" in ((res.get("capabilities") or {})), "握手说了自己提供 tools")

ok(ms.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None,
   "通知不回话(按协议)")

r = rpc("tools/list", {}, 2)
lst = ((r or {}).get("result") or {}).get("tools") or []
ok(set(x["name"] for x in lst) == set(x["name"] for x in registry.mcp_tools()),
   "tools/list 给的就是注册表里开着的那些, 共 %d 件" % len(lst))
ok("nextCursor" not in ((r or {}).get("result") or {}), "一页给完, 不吊着客户端")

r = rpc("ping", {}, 3)
ok("result" in (r or {}), "ping 有回")

r = rpc("no/such/method", {}, 4)
ok((r or {}).get("error", {}).get("code") == P.METHOD_NOT_FOUND, "不认识的 method 回方法不存在")

r = rpc("tools/call", {"name": "没这件工具", "arguments": {}}, 5)
res = (r or {}).get("result") or {}
ok(res.get("isError") is True, "调不存在的工具 -> isError, 不是协议崩")

bad = io.StringIO()
old = sys.stdout
sys.stdout = bad
try:
    r = ms.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                   "params": {"name": "crawl_nga", "arguments": {}}})
finally:
    sys.stdout = old
ok(sys.stdout is old and True, "坏参数调用没把解释器打挂")
ok(((r or {}).get("result") or {}).get("isError") is True, "坏参数调用也走 isError 那条路")

print("=== 3. MCP 客户端：把我们自己的服务端当外部服务接一遍 ===")
spec = {"name": "自己", "transport": "stdio",
        "command": [sys.executable, "-m", "harness.mcp"], "cwd": ROOT, "enabled": True}
tools, err = mc.list_tools(spec)
ok(err is None, "客户端连上了自带的 MCP 服务", str(err or ""))
ok(len(tools) == len(registry.mcp_tools()),
   "客户端看到的工具数一致（%d）" % len(tools))
wire = [mc.wire_name("自己", t["name"]) for t in tools]
ok(all(w.startswith("mcp__") for w in wire), "外部工具在模型面前带 mcp__ 前缀: %s" % wire[0])
ok(all(len(w) <= 64 for w in wire), "名字都没超 64 个字符")
ok(len(set(wire)) == len(wire), "名字互不相同")
ok(all((w.split("__", 2)[1].isascii() if w.split("__", 2)[1] else False) for w in wire),
   "前缀那段是纯 ASCII(模型那边只认这个): %s" % wire[0].split("__", 2)[1])
# 中文服务名不能被压成一堆下划线 —— 那样「我的工具」和「工具我的」会压成同一个名字, 路由就串了
a = mc.wire_name("我的工具", "search")
b = mc.wire_name("工具我的", "search")
ok(a != b, "两个中文服务名压出来不一样: %s / %s" % (a, b))
ok(mc.wire_name("My-Tools", "search") == "mcp__My-Tools__search",
   "本来就是干净名字的照搬, 一眼看得出是哪家")
ok(mc.wire_name("x" * 90, "y" * 90).count("__") >= 2 and len(mc.wire_name("x" * 90, "y" * 90)) <= 64,
   "超长名字压得住, 且还认得出是 mcp__ 开头")

defs = [mc.openai_def(spec, t, w) for t, w in zip(tools, wire)]
ok(len(defs) == len(tools), "外部工具也组得出 function-calling 定义")
ok(all(x["function"]["name"].startswith("mcp__") for x in defs), "外部定义用的也是那条带前缀的名字")
ok(all("不能引 [id=N]" in x["function"]["description"] for x in defs),
   "每条外部工具的说明里都写了「结果不能当样本引」")

print("=== 3.5 引擎那边：名字对得上、路由不串 ===")
from harness.engine import Engine                  # noqa: E402
cn = {"name": "我的工具", "transport": "http", "url": "http://127.0.0.1:9/mcp", "enabled": True}
eng = Engine.__new__(Engine)                       # 只验组名/查表这两步, 不真起引擎
eng._ext_specs = lambda: [spec, cn]
ext = {"自己": tools, "我的工具": [{"name": "echo", "description": "回个话",
                                    "inputSchema": {"type": "object", "properties": {}}}]}
eds, route = eng._ext_defs(ext)
ok(len(eds) == len(tools) + 1, "两家的工具都进了工具面（%d 件）" % len(eds))
ok(all(w in route for w in (x["function"]["name"] for x in eds)), "每件工具都能查回它的出处")
cnspec, cnremote = route[mc.wire_name("我的工具", "echo")]
ok(cnspec["name"] == "我的工具" and cnremote == "echo",
   "中文服务名的工具也还原得回原名(不是靠反推, 是靠这张表)")
ok(route[mc.wire_name("自己", "crawl_nga")][0]["name"] == "自己", "两家没串: 名字各归各家")
eng._routes = route
r = eng._call_external("mcp__没有这家__echo", {})
ok(bool(r.get("error")), "不在表里的名字 -> 报错, 不瞎猜别家: %s" % str(r.get("error"))[:60])

out, err = mc.call_tool(spec, "search_archive", {"query": "这是一条自检, 不该有结果"})
ok(err is None, "真调一件自带工具(翻记忆)调通了", str(err or ""))
ok(isinstance(out, (dict, list)), "回的结果是个结构化对象: %s" % str(out)[:100])

out, err = mc.call_tool(spec, "no_such_tool", {})
ok(err is not None, "调不存在的工具 -> 客户端如实报错, 不抛异常: %s" % str(err)[:80])

out, err = mc.call_tool(spec, None, {})
ok(err is not None, "名字为空也不炸")

# 坏服务不能拖垮整题：给一个起不来的命令
bad_spec = {"name": "坏服务", "transport": "stdio",
            "command": [sys.executable, "-c", "import sys; sys.exit(3)"], "cwd": ROOT}
t2, e2 = mc.list_tools(bad_spec)
ok(t2 == [] and e2, "起不来的外部服务 -> 空的清单 + 一句原因, 不抛异常")
ok(mc.list_tools({"name": "x", "transport": "stdio", "command": []})[1],
   "没填命令 -> 也是错误串, 不抛")
ok(mc.tools_cached(bad_spec) == [], "列不出来的服务不进缓存（改好配置点一下就能立刻生效）")
mc.shutdown_all()
ok(mc.peek_tools(spec) is None, "关掉会话后缓存也没了, 不会拿着旧清单骗人")

print("=== 4. web 后端要的 /api/tools 形状 ===")
from harness import webapp                        # noqa: E402
p = webapp.tools_payload()
ok(isinstance(p.get("builtin"), list) and p["builtin"], "给了自带工具清单")
one = p["builtin"][0]
ok({"name", "label", "optional", "enabled", "env", "needs", "desc"} <= set(one),
   "每条自带工具都说清了: 叫什么/开没开/缺什么/干什么")
ok(isinstance(p.get("servers"), list), "给了外部服务清单")
ok(isinstance(p.get("mcp"), dict) and p["mcp"].get("command"), "给了「别人怎么连我们」那条命令")
print("  连我们的命令: %s %s" % (p["mcp"]["command"], " ".join(p["mcp"]["args"])))
ok(webapp._spec_of({"name": " a ", "transport": "HTTP", "enabled": "0",
                    "url": " http://x/mcp "})["transport"] == "http",
   "页面上填的一行能翻成客户端认的服务描述")
ok(webapp._spec_of({"name": "a", "command": "npx -y 包名"})["command"] == ["npx", "-y", "包名"],
   "命令按空格拆开")

print("=== 5. 走 HTTP 那条路（后端真起来, 逐个口敲一遍）===")
import threading                                  # noqa: E402
import urllib.request                             # noqa: E402

srv = webapp._QuietServer(("127.0.0.1", 0), webapp.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % PORT


def _read(resp_or_err):
    raw = resp_or_err.read().decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        return raw


def get(path):
    try:
        return _read(urllib.request.urlopen(BASE + path, timeout=30))
    except urllib.error.HTTPError as e:      # 4xx/5xx 也是后端在好好回话, 照收
        return _read(e)


def post(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        return _read(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        return _read(e)


orig_txt = None
if os.path.isfile(settings.MCP_SERVERS_PATH):
    orig_txt = io.open(settings.MCP_SERVERS_PATH, encoding="utf-8").read()
try:
    t = get("/api/tools")
    ok(bool(t.get("builtin")), "GET /api/tools 拿到了自带工具清单")

    r = post("/api/mcp", {"action": "save", "servers": [
        {"name": "自己", "transport": "stdio",
         "command": "%s -m harness.mcp" % sys.executable, "cwd": ROOT, "enabled": "1"},
        {"name": "", "transport": "stdio", "command": "垃圾行, 该被丢"},
    ]})
    sv = ((r.get("tools") or {}).get("servers")) or []
    ok(r.get("ok") and len(sv) == 1, "存了两个, 没名字的那个被丢掉, 只留 1 个")
    ok(sv[0]["transport"] == "stdio" and sv[0]["cwd"] == ROOT, "存回来的服务描述是完整的")
    ok(sv[0].get("tools") is None, "刚存进去的还没测过 -> 显示「还没测」, 不编个数")

    r = post("/api/mcp", {"action": "test", "server": sv[0]})
    ok(r.get("ok") and len(r.get("tools") or []) == len(registry.mcp_tools()),
       "点「测试」真去连了一次, 拿到 %d 件工具" % len(r.get("tools") or []))

    r = get("/api/tools")
    sv = r["servers"][0]
    ok(isinstance(sv.get("tools"), list) and sv["tools"], "测过之后这一页就有清单了(走的是缓存)")

    r = post("/api/mcp", {"action": "test",
                          "server": {"name": "坏地址", "transport": "http",
                                     "url": "http://127.0.0.1:1/mcp"}})
    ok(not r.get("ok") and r.get("error"), "连不上 -> ok=false + 一句原因, 不是 500")

    r = post("/api/mcp", {"action": "干嘛", "server": {}})
    ok(not r.get("ok"), "不认识的 action 也回人话")

    # 别的 MCP 客户端从 HTTP 连我们 —— 跟 stdio 是同一套处理逻辑
    r = post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": P.LATEST_PROTOCOL}})
    ok(((r or {}).get("result") or {}).get("serverInfo", {}).get("name") == P.SERVER_NAME,
       "POST /mcp 握手通了(HTTP 那条路)")
    r = post("/mcp", [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                      {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    ok(isinstance(r, list) and len(r) == 2, "一批一起发也认(批量)")
    ok(len((r[1].get("result") or {}).get("tools") or []) == len(registry.mcp_tools()),
       "批量里那件 tools/list 也答对了")
finally:
    if orig_txt is None:
        if os.path.isfile(settings.MCP_SERVERS_PATH):
            os.remove(settings.MCP_SERVERS_PATH)
    else:
        io.open(settings.MCP_SERVERS_PATH, "w", encoding="utf-8").write(orig_txt)
    srv.shutdown()
mc.shutdown_all()

print()
if FAIL:
    print("汇总: %d 项没过 -> %s" % (len(FAIL), "; ".join(FAIL)))
    sys.exit(1)
print("汇总: 全部通过")
