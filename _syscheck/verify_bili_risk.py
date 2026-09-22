# -*- coding: utf-8 -*-
"""B站风控链路自检(离线: 只起一个回环 HTTP 服务, 不打网、不烧模型):
    python _syscheck/verify_bili_risk.py

为什么要有它: 这条链只在"B站真甩 412"时才走到, 而真去撞一次既慢又伤平台(还要守 5-10 分钟间隔),
所以把它拆成可离线复现的几段 —— 服务端回什么形状、下游怎么认、引擎最后有没有真的冷却。

查五段:
  ① 412/403 在 client 那层就被打上 risk 标记(DataFetchError.risk), 不再是"一个普通报错";
  ② live 收错误体时留得住 risk 字段(改前是一行字符串, risk 直接丢);
  ③ live 把"风控/超时/429"认成 throttle, 普通报错不算, 非 bili 平台一律不算;
  ④ 真爬回空列表 = "真没搜到", 不许被当成失败(否则会白冷却);
  ⑤ 串到底: 一次风控 CrawlError -> 引擎把 bili 记成风控并会话冷却, 冷却中再调被挡下;
     且 NGA 不受连累(只熔断不冷却)。
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from harness.sources import CrawlError
from harness.sources import live

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


# ---------------------------------------------------------------- 假服务端
# 只复现"错误体形状"这几条: 形状对了, 下游才有得认。真服务端在 crawlers/bili/bili_search_server.py。
_ROUTES = {
    "/ok":      (200, {"query": "x", "videos": [{"bvid": "BV1"}]}),
    "/risk":    (503, {"error": "bilibili 风控拦截(HTTP 412), 本次不再重试", "risk": True}),
    "/inner":   (500, {"error": "crawl internal error"}),
    "/timeout": (504, {"error": "crawl timeout 300s"}),
    "/html":    (500, "<html>拦截页</html>"),
}


class _H(BaseHTTPRequestHandler):
    def do_POST(self):
        code, body = _ROUTES.get(self.path, (404, {"error": "not found"}))
        raw = body.encode("utf-8") if isinstance(body, str) else \
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), _H)                 # 端口 0 = 让系统挑个空闲口, 不撞服务
threading.Thread(target=srv.serve_forever, daemon=True).start()
live._BASE["bilibili"] = "http://127.0.0.1:%d" % srv.server_address[1]
live._read_token = lambda plat: "t"                     # 自检不打真服务, 鉴权这层不掺进来


def post(path):
    return live._post_path("bilibili", path, {"query": "x"}, timeout=5)


# ① 风控标记本身。直接按文件路径加载 exception.py —— 走包导入会把 bilibili/__init__ 那一串
#    (config/playwright 之类, 爬虫自己那套 venv 才有)一起拖进来, 而这条链只关心这个类。
import importlib.util

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_exc_path = os.path.join(_here, "crawlers", "bili", "media_platform", "bilibili", "exception.py")
DataFetchError = None
if not os.path.exists(_exc_path):
    # 部署机上爬虫不在树内(如 <DEPLOY_ROOT>/bili-demo) —— 这一条只验"那个类本身", 树里没有就跳过。
    # 下面 ②~⑦(错误体形状 -> risk 标记 -> 冷却 -> 熔断)才是这条链的主体, 照跑不误。
    print("-- exception.py 可加载 (这棵树里没有 crawlers/: 爬虫不在树内(部署布局))")
else:
    try:
        _spec = importlib.util.spec_from_file_location("_bili_exception", _exc_path)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        DataFetchError = _mod.DataFetchError
        chk("DataFetchError 默认不带 risk", DataFetchError("x").risk is False)
        _e = DataFetchError("x")
        _e.risk = True
        chk("risk 可被置真", _e.risk is True)
    except Exception as exc:
        chk("exception.py 可加载", False, repr(exc))
        DataFetchError = None

# ② 错误体里的 risk 字段留不留得住
r = post("/risk")
chk("503 错误体解析成 dict", isinstance(r, dict), r)
chk("risk 字段留住了", r.get("risk") is True, r)
chk("error 文本留住了", "风控" in str(r.get("error")), r)
chk("http 码留住了", r.get("http") == 503, r)
r_ok = post("/ok")
chk("200 正常体原样透传", r_ok.get("videos") == [{"bvid": "BV1"}], r_ok)
r_html = post("/html")
chk("非 JSON 错误体退回纯文本", isinstance(r_html, dict) and "拦截页" in str(r_html.get("error")), r_html)

# ③ throttle 判定
def raised(res, hint=True):
    try:
        live._raise_if_err("bilibili", res, throttle_hint=hint)
        return None
    except CrawlError as e:
        return e


chk("风控 -> throttle", raised(post("/risk")) and raised(post("/risk")).throttle is True)
chk("超时 -> throttle", raised(post("/timeout")).throttle is True)
chk("真出错 -> 不算 throttle", raised(post("/inner")).throttle is False)
chk("非 bili 平台一律不算 throttle",
    raised(post("/risk"), hint=False).throttle is False)

# ④ 空列表 = 真没搜到, 不许当失败
chk("空 videos 不抛错", raised({"query": "x", "videos": []}) is None)

# ⑤ 串到引擎: 真冷却
from harness.runner.session import Session

_tmpdb = os.path.join(tempfile.gettempdir(), "verify_bili_risk.db")
for _p in (_tmpdb, _tmpdb + "-wal", _tmpdb + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
s = Session(db_path=_tmpdb, scope="syscheck:bili-risk", nga_ratio=50)
st = s.engine._plat("bilibili")
chk("开跑时 bili 没在冷却", st["cool_until"] == 0.0 and not st["ask_fail"], st)

msg = s.engine._mark_fail("bilibili", raised(post("/risk")))
st = s.engine._plat("bilibili")
chk("风控失败 -> 进会话冷却", st["cool_until"] > time.time(), st["cool_until"])
chk("风控失败 -> 本 ask 熔断", bool(st["ask_fail"]), st["ask_fail"])
chk("报给模型的话里点明是风控", "风控" in json.dumps(msg, ensure_ascii=False), msg)
pre = s.engine._precheck("bilibili")                    # 同一 ask 内: 先被"本 ask 熔断"挡住
chk("同 ask 内再调被熔断挡下",
    pre is not None and "熔断" in json.dumps(pre, ensure_ascii=False), pre)
st["ask_fail"] = None                                   # 翻到下一题: 熔断随 ask 清, 冷却跨 ask 留
pre = s.engine._precheck("bilibili")
chk("下一题再调被冷却挡下",
    pre is not None and "冷却" in json.dumps(pre, ensure_ascii=False), pre)
s.engine._mark_fail("NGA", CrawlError("nga 挂了", platform="NGA"))
chk("NGA 只熔断不冷却", s.engine._plat("NGA")["cool_until"] == 0.0)
s.conn.close()
for _p in (_tmpdb, _tmpdb + "-wal", _tmpdb + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)

srv.shutdown()

print("-" * 56)
print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  FAIL:", f)
sys.exit(1 if fails else 0)
