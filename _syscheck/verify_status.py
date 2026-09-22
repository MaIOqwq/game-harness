# -*- coding: utf-8 -*-
"""爬虫状态那两个新态(冷却中 / 正在使用)的后端那一半, 离线自己验自己。

为什么不真起服务: 这两个态都是"此刻正忙", 要制造出真冷却得先让 B站爬真挂一次(碰网络、碰风控),
要制造 busy 得真跑一次 /crawl。那就换个不吃进程的验法 —— 把 /api/status 收数那段
(status_payload / _cool_left)抠出来, 拿桩数据喂进去, 断言吐出来的形状对不对。

验三件事:
  1. 冷却剩余秒数: 从 cooldown(**全机器一份**, 见 harness/cooldown.py)读、算得对、进一位;
  2. 只读不建: 查一个没跑过题的会话, 不许顺手建出 Session(那会把空 scope 写进记忆库);
  3. 两态进得了 payload: busy 来自爬虫 /health, cool_left 来自 cooldown, 两个都随每行爬虫下发。

跑: python _syscheck/verify_status.py
"""
import json
import os
import sys
import time

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
from harness import cooldown, supervisor, webapp  # noqa: E402

FAIL = 0


def ok(cond, label, extra=""):
    global FAIL
    print("  [%s] %s%s" % ("ok" if cond else "FAIL", label, ("  " + extra) if extra else ""))
    if not cond:
        FAIL += 1


class _Sess(object):
    """假的 Session: 只要它身上有 engine._plats 就够——status 只从这读冷却。"""
    def __init__(self, plats):
        self.engine = type("E", (), {"_plats": plats})()


# ---- 把爬虫那侧收数打桩: 不碰网络, 由每个用例自己摆布 ----
HEALTH = {}


def fake_health(plat):
    h = HEALTH.get(plat)
    if h is None:
        raise RuntimeError("桩里没给 %s 的 health" % plat)
    return h


webapp.live.health = fake_health
webapp.live.measure_mode = lambda: "keyword"
supervisor.enabled = lambda: True
supervisor.is_running = lambda plat: plat in HEALTH
# 放行那条要打真爬虫 —— 这里也换掉, 免得测试真去连 8771/8770(本机没起就成 500、起了就真开浏览器)
webapp.live._post_path = lambda plat, path, payload=None, timeout=10: {"ok": True, "stub": path}
webapp.live._get_path = lambda plat, path, timeout=10: {"ok": True, "stub": path}


def reset(cool_until=None, fail_streak=0):
    """清空会话表 + 给 bili 摆一个冷却截止时刻。

    **冷却是全机器一份的**(harness/cooldown.py), 不挂在会话上 —— 所以状态要摆到那里去;
    摆进下面那个桩 Session 是没用的, 它只用来验"查状态不该顺手建会话"那几条。
    平台名照**引擎那边的真实键**给(NGA 大写、bilibili 小写, 见 engine._plat 的调用方)——
    这里要是图省事写成 "nga", 就验不出"大小写不归一 -> 那行查不到冷却"这个坑。"""
    webapp._SESSIONS.clear()
    HEALTH.clear()
    webapp._SESSIONS["web:s1"] = _Sess({"NGA": {"cool_until": 0.0, "fail_streak": 0},
                                        "bilibili": {"cool_until": 0.0, "fail_streak": 0}})
    cooldown.reset()
    if cool_until:
        cooldown._ST["bilibili"] = {"until": cool_until, "streak": fail_streak}
    HEALTH["nga"] = {"ok": True, "login": "ok", "busy": False, "queries_done": 12}
    HEALTH["bilibili"] = {"ok": True, "login": "ok", "busy": False, "queries_done": 7}


def row(payload, key):
    return [c for c in payload["crawlers"] if c["key"] == key][0]


print("=== 1. 冷却剩余秒数: 按会话从引擎读, 算得对 ===")
reset(cool_until=time.time() + 125)
left = webapp._cool_left("s1")
ok("bili" in left, "bili 在冷却 -> 报得出剩余")
ok(124 <= left["bili"] <= 126, "剩余 ~125 秒(进一位)", "拿到 %s" % left.get("bili"))
ok("nga" not in left, "NGA 不冷却(它只熔断) -> 不出现这个键")

# NGA 眼下确实不冷却, 但"查不查得到"得单独验一次: 引擎那侧记冷却用的是大写 "NGA", 状态视图查的
# 是小写 "nga", 不归一就会静默查不到 —— 哪天 NGA 也加了冷却, 前端会一声不吭地不显示。
cooldown.mark_fail("NGA", 40, 900)                  # 走真写入口, 键是大写
ok("nga" in cooldown._ST and "NGA" not in cooldown._ST,
   "账本里的键归一到小写(写和读用同一把尺, 不是只归一其中一头)")
ok(webapp._cool_left("s1").get("nga") in (40, 41), "大写写进去、小写查出来: 大小写归一后才查得到",
   "拿到 %s" % webapp._cool_left("s1").get("nga"))

reset(cool_until=time.time() - 1)
ok(webapp._cool_left("s1") == {}, "冷却时间已过 -> 一个字都不报(不倒扣成负数)")

reset(cool_until=time.time() + 0.2)
ok(webapp._cool_left("s1").get("bili") == 1, "还剩不到 1 秒也报 1 —— 倒计时走到 0 时它才该真好",
   "拿到 %s" % webapp._cool_left("s1").get("bili"))

print("=== 2. 只读不建: 查状态不许把空会话写进记忆库 ===")
reset()
n0 = len(webapp._SESSIONS)
ok(webapp._cool_left("从没问过题的会话") == {}, "没跑过题的会话 -> 没有冷却")
ok(len(webapp._SESSIONS) == n0, "且**没有**因此建出一个 Session",
   "会话数 %d -> %d" % (n0, len(webapp._SESSIONS)))
ok(webapp._cool_left(None) == {}, "连 sid 都没给(/api/status 裸调) 也不炸、也不建")

print("=== 3. 两态随每行爬虫下发 ===")
reset(cool_until=time.time() + 300)
HEALTH["nga"]["busy"] = True                      # NGA 此刻有题在爬
HEALTH["bilibili"]["busy"] = False
p = webapp.status_payload("s1")
ok(row(p, "bili")["cool_left"] > 0, "bili 行带冷却剩余 -> 前端画「冷却中」倒计时")
ok(row(p, "nga")["cool_left"] == 0, "NGA 行没冷却 -> cool_left 是 0(不是缺字段, 前端好判)")
ok(row(p, "nga")["busy"] is True, "NGA 行 busy=true -> 前端画「正在使用」")
ok(row(p, "bili")["busy"] is False, "bili 行 busy=false -> 照常报登录态")
ok(p["measure"] == "keyword", "度量那格照旧带着")
ok(json.dumps(p, ensure_ascii=False), "整包 JSON 化得出去(前端拿得到)")

p2 = webapp.status_payload(None)
ok(row(p2, "bili")["cool_left"] > 0, "不带 session 查也照样看得到冷却(它是全机器一份的, 不属于某个会话)",
   "cool_left=%s" % row(p2, "bili")["cool_left"])
ok(row(p2, "nga")["busy"] is True, "但 busy 不带 session 也照样有(它不属于某个会话)")

print("=== 4. 爬虫没起(静止态)时也要带着 cool_left ===")
reset(cool_until=time.time() + 60)
del HEALTH["bilibili"]                            # bili 服务没起
p3 = webapp.status_payload("s1")
b = row(p3, "bili")
ok(b["running"] is False, "没起 -> running=false(前端画灰灯「未启动」)")
ok(b["cool_left"] > 0, "没起也照报冷却剩余: 冷却是引擎的账, 与服务在不在跑无关")
ok(b["busy"] is None, "没起 -> busy=null(起都没起, 谈不上在用)")

print("=== 5. 冷却期**强制不可使用**: 修复那条链真被挡下(不是只把按钮藏起来) ===")
# 真起一个后端在空端口上, 逐个口敲一遍 —— "藏按钮"不算强制, 接口挡得住才算。
import threading                                    # noqa: E402
import urllib.error                                 # noqa: E402
import urllib.request                               # noqa: E402

ENSURE = []
supervisor.ensure = lambda plat: ENSURE.append(plat)  # 记下"有没有真去拉爬虫"(拉起来=会开浏览器)

reset(cool_until=time.time() + 240)
srv = webapp._QuietServer(("127.0.0.1", 0), webapp.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def post(path, body=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method="POST",
                                 data=json.dumps(body or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


ENSURE[:] = []
code, body = post("/api/login/bili", {"session": "s1"})
ok(code == 429, "冷却中打「开始扫码」-> 429 挡下", "拿到 HTTP %s" % code)
ok(u"冷却" in body.get("error", "") and "4:00" in body.get("error", ""),
   "挡下来的话里说清原因 + 还要多久", body.get("error", ""))
ok(body.get("cool_left") in (240, 241), "顺带把剩余秒数回给前端(它好摆倒计时)", str(body.get("cool_left")))
ok(ENSURE == [], "**且没有去拉爬虫** —— 挡在 ensure 前面, 连浏览器都不必起", str(ENSURE))

ENSURE[:] = []
code, body = post("/api/login/bili", {"session": "别人家的会话"})
ok(code == 429, "**换个会话照样挡** —— 冷却是全机器一份、不挂在会话上, 谁来了都别想再碰 bili",
   "拿到 HTTP %s" % code)
ok(ENSURE == [], "且照样不去拉爬虫", str(ENSURE))

ENSURE[:] = []
code, _ = post("/api/login/nga", {"session": "s1"})
ok(code == 200, "bili 冷却**不影响** NGA 那条修复链", "拿到 HTTP %s" % code)

reset()                                             # 冷却没了
srv.shutdown()
ok(webapp._cool_note("s1", "bilibili", "扫码") is None, "不在冷却 -> 不拦(None)")

print("=== 6. 引擎侧那半: 光有灯不够, 模型也调不动它 ===")
# 冷却真正的用处在这: 模型手里那把 crawl_bili 得真的挥不动, 否则"冷却"只是给用户看的。
from harness.engine import Engine                   # noqa: E402
from harness.sources import CrawlError              # noqa: E402


def risk(msg="bili crawl fail: 412 风控"):
    """平台真风控时引擎收到的就是这个形状(live._raise_if_err 打的 throttle 标记)。"""
    return CrawlError(msg, platform="bilibili", throttle=True)


def engine():
    e = Engine.__new__(Engine)                      # 不跑 __init__(那会连库/连网), 只摆出 _precheck 要的几样
    e._plats = {}
    e._nga_ratio = 50
    e._mode = "快速"
    e._n_crawl = 0
    e._ask_seq = 0                                  # _precheck 靠它认"同轮是不是同一题的"(跨题长等爬距)
    e._resume_from = None                           # 不是「继续」那一跑 -> 跨 ask 爬距照常吃
    return e


cooldown.reset()
e = engine()
e._mark_fail("bilibili", risk())
st = e._plats["bilibili"]
ok(st["cool_until"] > time.time(), "B站风控失败 -> 账上记下冷却")
ok(st["fail_streak"] == 1, "且记了一次连续失败(下次翻倍封顶)")
st["ask_fail"] = None                               # 下一题: 每 ask 清的熔断已经清了……
pre = e._precheck("bilibili")
ok(pre is not None and u"冷却" in pre.get("platform_error", ""),
   "**熔断清了、冷却还在 -> crawl_bili 照样被挡**", (pre or {}).get("platform_error", "")[:40])
ok(e._precheck("NGA") is None, "同一时刻 NGA 照常放行(它不冷却)")
cooldown.reset()                                    # 冷却走完
ok(e._precheck("bilibili") is None, "冷却走完 -> 自动恢复可用(不用手动清)")

cooldown.reset()
e2 = engine()
e2._mark_fail("bilibili", risk())
left1 = e2._plats["bilibili"]["cool_until"] - time.time()
e2._mark_fail("bilibili", risk())
left2 = e2._plats["bilibili"]["cool_until"] - time.time()
ok(left2 > left1 * 1.8, "连着被风控 -> 冷却时间翻倍(退避越挡越久)", "%.0fs -> %.0fs" % (left1, left2))
e2._after_crawl({"platform": "bilibili"}, {"summary": {"new": 1}})
ok(e2._plats["bilibili"]["fail_streak"] == 0, "成功爬一次 -> 连续失败归零(冷却从头算)")
ok(cooldown.remaining("bilibili") == 0, "且全机器那份也真解了(不是只清引擎里的镜像)")

# 冷却不能太贪: 一次网络抖动也锁死 bili 五分钟, 用户就成了"爬虫明明能跑却不让跑"。
cooldown.reset()
e3 = engine()
e3._mark_fail("bilibili", CrawlError("bili crawl fail: connection reset by peer", platform="bilibili"))
ok(cooldown.remaining("bilibili") == 0, "非风控失败(服务没起/网络抖) -> **不进冷却**")
ok(e3._plats["bilibili"]["ask_fail"], "但熔断照记 —— 这一题里这个平台确实已经不行了")

print("=== 7. 坏掉的 health 不许把冷却连带弄丢 ===")
reset(cool_until=time.time() + 90)
HEALTH.pop("nga")                                 # NGA 那侧炸了
p4 = webapp.status_payload("s1")
ok(row(p4, "nga")["ok"] is False, "拿不到 health -> ok=false(红灯, 不编)")
ok(row(p4, "nga")["cool_left"] == 0, "它没冷却 -> 0")
ok(row(p4, "bili")["cool_left"] > 0, "另一行(受桩保护)该有的冷却还在")

print("")
if FAIL:
    print("汇总: %d 项没过" % FAIL)
    sys.exit(1)
print("汇总: 全部通过")
