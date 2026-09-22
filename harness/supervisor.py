# -*- coding: utf-8 -*-
"""按需拉起两个爬虫服务, 闲置就关掉 —— 让「只常驻网页后端」这一条成真。

为什么需要它: 两个爬虫都是在**启动时就把浏览器拉起来**再开始收请求的
(nga_search_server.py 的 amain / bili_search_server.py 的 amain 里, 浏览器在 serve_forever
之前就起了)。于是「一直开着」的真实成本是两个 Chrome 干坐着 —— 实测三条服务常驻约 1.2 GB,
其中约 1 GB 是那两个浏览器。而大部分时间没人在问问题。
改成: 只常驻网页后端, 真要爬的时候才把对应平台的爬虫拉起来, 闲置 IDLE_SEC 秒后连它的浏览器
一起关掉。

登录态不受影响, 重开不用重扫(都在磁盘上):
  - bilibili: crawlers/bili/browser_data/ —— **注意它按「当前工作目录」算**, 所以拉起时必须
    cwd=那个目录, 否则会认不出旧登录态、另起一个空 profile(见 media_platform/bilibili/core.py)。
  - NGA:      crawlers/nga/config.json 的 cookies 段(__file__ 定的路径, 与 cwd 无关)。

只在本地模式(HARNESS_LOCAL 非 0)下管。走远程部署时爬虫不在本机, 这里一律不插手。
排障/测试想关掉自动化: 设 HARNESS_NO_LAZY=1(此时服务照旧要手工起)。
"""
import atexit
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.environ.get("HARNESS_LOCAL_ROOT") or os.path.normpath(
    os.path.join(_HERE, "..", "crawlers"))
_LOGS = os.path.join(os.path.dirname(_ROOT), "logs")

_LOCAL = os.environ.get("HARNESS_LOCAL", "1").strip().lower() not in ("", "0", "false", "no", "off")
_ENABLED = _LOCAL and os.environ.get("HARNESS_NO_LAZY", "") in ("", "0")

# 平台名与 live.py 的 _BASE/health 用的是同一套(nga / bilibili)
_SPEC = {
    "nga": {"port": 8770, "dir": "nga", "script": "nga_search_server.py", "name": "NGA"},
    "bilibili": {"port": 8771, "dir": "bili", "script": "bili_search_server.py", "name": "bilibili"},
}

IDLE_SEC = int(os.environ.get("HARNESS_IDLE_SEC", "600"))
START_TIMEOUT = int(os.environ.get("HARNESS_START_TIMEOUT", "180"))

_GUARD = threading.Lock()
_PROC = {}          # plat -> Popen(我们自己拉起来的那个)
_LAST = {}          # plat -> 最近一次「用到了」的时刻(time.time())
_LOCKS = {}         # plat -> 同一平台只让一个线程去拉
_reaper_on = False


def _log(msg):
    print("[supervisor] %s" % msg, flush=True)


def _plat_lock(plat):
    with _GUARD:
        if plat not in _LOCKS:
            _LOCKS[plat] = threading.Lock()
        return _LOCKS[plat]


def enabled():
    """本地模式才由我们管进程; 远程部署时爬虫不在本机, 一律不插手。"""
    return _ENABLED


def configure(idle_sec=None, start_timeout=None, lazy=None):
    """运行期改旋钮(页面上改设置时调)。三个参数都可省, 只改传进来的。

    lazy: True=按需拉起, False=爬虫常开(相当于 HARNESS_NO_LAZY=1)。
    """
    global IDLE_SEC, START_TIMEOUT, _ENABLED
    if idle_sec is not None:
        IDLE_SEC = int(idle_sec)
    if start_timeout is not None:
        START_TIMEOUT = int(start_timeout)
    if lazy is not None:
        _ENABLED = _LOCAL and bool(lazy)
        if _ENABLED:
            start_reaper()          # 从"常开"切回"按需"时, 巡检线程得补上


def _port_open(plat):
    try:
        with socket.create_connection(("127.0.0.1", _SPEC[plat]["port"]), timeout=1):
            return True
    except Exception:
        return False


def is_running(plat):
    """它在不在听。只看端口 —— 端口在听就说明它已经过了「拉浏览器 + 验登录」那一关
    (两个服务的 amain 都是先把浏览器弄好、再 serve_forever)。"""
    if plat not in _SPEC:
        return False
    with _GUARD:
        p = _PROC.get(plat)
    if p is not None and p.poll() is None:
        return True
    return _port_open(plat)


def touch(plat):
    """记「刚用过」。闲置计时从这里算。"""
    with _GUARD:
        _LAST[plat] = time.time()


def ensure(plat, timeout=None):
    """确保这个平台的爬虫在跑: 在跑直接回 True; 没跑就拉起来并等它端口在听。
    起不来(超时 / 进程秒退)回 False —— **不抛**: 照常往下走, 让原来那条「连不上就报错」的
    路径去如实说失败原因, 不在这一层变语义。"""
    if not _ENABLED or plat not in _SPEC:
        return True
    if is_running(plat):
        touch(plat)
        return True
    with _plat_lock(plat):
        if is_running(plat):            # 排队等锁的时候, 别的线程已经把它起好了
            touch(plat)
            return True
        return _spawn(plat, timeout or START_TIMEOUT)


def _spawn(plat, timeout):
    sp = _SPEC[plat]
    d = os.path.join(_ROOT, sp["dir"])
    script = os.path.join(d, sp["script"])
    if not os.path.isfile(script):
        _log("%s 找不到启动脚本: %s" % (sp["name"], script))
        return False
    os.makedirs(_LOGS, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    logf = open(os.path.join(_LOGS, sp["dir"] + ".log"), "ab")
    try:
        # cwd 必须是它自己的目录: bili 的 browser_data(登录态) 是按 cwd 找的
        p = subprocess.Popen([sys.executable, "-u", sp["script"], "--port", str(sp["port"])],
                             cwd=d, stdout=logf, stderr=subprocess.STDOUT, env=env, **kw)
    except Exception as e:
        logf.close()
        _log("%s 拉起失败: %r" % (sp["name"], e))
        return False
    with _GUARD:
        _PROC[plat] = p
        # 拉起即计时: 冷启动这段(拉浏览器, 可达十几秒)还没「就绪」, 不记的话巡检会把它当成
        # 没有计时记录的僵尸 —— 见 _reap_once 里那条注释。
        _LAST[plat] = time.time()
    logf.close()          # 子进程已拿到自己那份句柄, 父进程这份留着只是白占
    _log("%s 拉起 pid=%s, 等它把浏览器拉起来(最多 %ds) …" % (sp["name"], p.pid, timeout))
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _port_open(plat):
            touch(plat)
            _log("%s 就绪, 冷启动用了 %.1fs" % (sp["name"], time.time() - t0))
            return True
        if p.poll() is not None:
            _log("%s 起来又秒退了(退出码 %s) —— 看 logs/%s.log" % (sp["name"], p.returncode, sp["dir"]))
            return False
        time.sleep(0.5)
    _log("%s 等了 %ds 还没就绪, 放弃(进程留着, 它可能只是慢)" % (sp["name"], timeout))
    return False


def _tool_token(plat):
    try:
        with open(os.path.join(_ROOT, _SPEC[plat]["dir"], ".tool_token"), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def _busy(plat):
    """问爬虫自己忙不忙。读不到就当忙 —— 宁可多留一会儿, 也别把正在爬的那个掐了。"""
    tok = _tool_token(plat)
    if not tok:
        return True
    req = urllib.request.Request("http://127.0.0.1:%d/health" % _SPEC[plat]["port"],
                                 headers={"X-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("busy"))
    except Exception:
        return True


def _kill_tree(p):
    """连它拉起来的那些 Chrome 一起带走 —— 不然留下没主的浏览器进程白占内存。"""
    if p.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import signal
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass
    try:
        p.wait(timeout=15)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def shutdown(plat, why=""):
    with _GUARD:
        p = _PROC.pop(plat, None)
        _LAST.pop(plat, None)
    if p is None or p.poll() is not None:
        return
    _log("%s 关掉 pid=%s%s" % (_SPEC[plat]["name"], p.pid, ("（%s）" % why) if why else ""))
    _kill_tree(p)


def shutdown_all():
    for plat in list(_SPEC):
        try:
            shutdown(plat, "进程退出")
        except Exception as e:
            _log("收尾 %s 出错: %r" % (plat, e))


def _reap_once():
    if IDLE_SEC <= 0:
        return
    now = time.time()
    for plat in _SPEC:
        with _GUARD:
            p = _PROC.get(plat)
            last = _LAST.get(plat)
        if p is None or p.poll() is not None:
            continue
        if last is None:
            # 没有计时记录 ≠ 闲置了很久。早先这里写的是 `or 0`, 于是 idle = now - 0 会算成
            # 「闲置 29827970 分钟」, 刚拉起、还在冷启动的爬虫被当场砍掉(官号探针那次连接被
            # 重置就是踩了这个)。没记录就重新起计, 下一轮再看。
            touch(plat)
            continue
        idle = now - last
        if idle < IDLE_SEC:
            continue
        if _busy(plat):
            touch(plat)                 # 正在爬 / 问不到: 重新计时, 下一轮再看
            continue
        shutdown(plat, "闲置 %d 分钟" % int(idle // 60))


def _reap_loop():
    while True:
        time.sleep(15)
        try:
            _reap_once()
        except Exception as e:
            _log("闲置巡检出错: %r" % e)


def start_reaper():
    """起后台巡检线程(幂等)。网页后端启动时调一次。"""
    global _reaper_on
    if not _ENABLED or _reaper_on:
        return
    _reaper_on = True
    threading.Thread(target=_reap_loop, daemon=True, name="crawler-reaper").start()


atexit.register(shutdown_all)
