# -*- coding: utf-8 -*-
"""MCP 客户端 —— 把**用户自己加的外部工具服务**接进来用。

两种接法(用户在设置页填的就是这两种之一):
  stdio —— 给一条命令(如 `npx -y 某工具包`), 我们起子进程、经它的标准输入输出说话;
  http  —— 给一个地址(如 `http://127.0.0.1:8121/mcp`), 我们直接 POST JSON-RPC。

工程上的几条硬要求:
  - **外部服务坏了不能拖垮整题**: 连不上/超时/回垃圾一律转成一句人话错误返回给模型,
    不抛异常往上炸(那会把一整道题作废)。
  - **超时必须是真的超时**: 读子进程输出在 Windows 上没法用 select, 所以每个会话配一个
    读取线程把消息塞进队列, 请求侧在队列上等 —— 子进程卡死也能按时收场。
  - 会话按"服务名"缓存复用(起一次 `npx` 要好几秒, 每次调用都重起太亏); 进程死了下次自动重起。
"""
import atexit
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from . import protocol as P

_START_TIMEOUT = 30          # 起子进程 + 握手
_LIST_TIMEOUT = 60           # 列工具
_CALL_TIMEOUT = 600          # 调工具(外部工具也可能在慢慢爬, 给足)

_GUARD = threading.Lock()
_SESSIONS = {}               # 缓存键 -> _Stdio / _Http
_EOF = object()


def _err(msg):
    print("[mcp] %s" % msg, file=sys.stderr, flush=True)


def _spec_key(spec):
    return json.dumps([spec.get("transport"), spec.get("command"), spec.get("url"),
                       spec.get("headers")], ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- stdio
class _Stdio:
    def __init__(self, spec):
        self.spec = spec
        self.proc = None
        self.q = queue.Queue()
        self.tail = []                 # 子进程 stderr 的最后几行, 出错时给人看
        self._n = 0
        self._lock = threading.Lock()

    # ---- 起进程 + 握手
    def start(self):
        cmd = self.spec.get("command") or []
        if not cmd:
            raise RuntimeError("没填命令")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        for k, v in (self.spec.get("env") or {}).items():
            env[str(k)] = str(v)
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        cwd = self.spec.get("cwd") or None
        self.proc = subprocess.Popen([str(c) for c in cmd], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, cwd=cwd, **kw)
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        res = self.request("initialize", {
            "protocolVersion": P.LATEST_PROTOCOL, "capabilities": {},
            "clientInfo": {"name": P.SERVER_NAME, "version": P.SERVER_VERSION}},
            timeout=_START_TIMEOUT)
        if res.get("error"):
            raise RuntimeError("握手失败: %s" % res["error"].get("message"))
        self.notify("notifications/initialized")

    def _read_out(self):
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    self.q.put(json.loads(line))
                except Exception:
                    _err("外部服务吐了非协议内容(已忽略): %s" % line[:120])
        except Exception:
            pass
        # EOF 时队列里可能还留着没被 request() 取走的旧响应; 清掉, 否则 _Stdio 复用时会读到上一个进程的消息
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.q.put(_EOF)

    def _read_err(self):
        """把子进程 stderr 收进环形缓冲 —— 出问题时那几行通常就是答案。"""
        try:
            for raw in self.proc.stderr:
                self.tail.append(raw.decode("utf-8", "replace").rstrip())
                del self.tail[:-8]
        except Exception:
            pass

    def _why(self):
        return ("; 它最后说: " + " | ".join(self.tail[-3:])) if self.tail else ""

    # ---- 说话
    def _write(self, msg):
        self.proc.stdin.write((P.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        with self._lock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method, params=None, timeout=_CALL_TIMEOUT):
        with self._lock:
            if self.proc.poll() is not None:
                raise RuntimeError("外部服务进程已退出(码 %s)%s" % (self.proc.returncode, self._why()))
            self._n += 1
            rid = self._n
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            deadline = time.time() + timeout
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    raise RuntimeError("等 %s 回应超时(%ds)" % (method, timeout))
                try:
                    msg = self.q.get(timeout=remain)
                except queue.Empty:
                    raise RuntimeError("等 %s 回应超时(%ds)%s" % (method, timeout, self._why()))
                if msg is _EOF:
                    raise RuntimeError("外部服务的输出断了%s" % self._why())
                if msg.get("id") == rid:
                    return msg
                # 我们串行发请求, 收到别的 id / 通知(例如进度)时直接丢

    def close(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------- http
class _Http:
    def __init__(self, spec):
        self.spec = spec
        self.url = spec.get("url") or ""
        self.headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
        self.session_id = None
        self._n = 0
        self._lock = threading.Lock()

    def start(self):
        res = self.request("initialize", {
            "protocolVersion": P.LATEST_PROTOCOL, "capabilities": {},
            "clientInfo": {"name": P.SERVER_NAME, "version": P.SERVER_VERSION}},
            timeout=_START_TIMEOUT)
        if res.get("error"):
            raise RuntimeError("握手失败: %s" % res["error"].get("message"))
        try:
            self.notify("notifications/initialized")
        except Exception:
            pass          # 有的服务端不接受这条通知, 不影响后面

    def notify(self, method, params=None):
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}}, _START_TIMEOUT)

    def request(self, method, params=None, timeout=_CALL_TIMEOUT):
        with self._lock:
            self._n += 1
            rid = self._n
            out = self._post({"jsonrpc": "2.0", "id": rid, "method": method,
                              "params": params or {}}, timeout)
            if out is None:
                raise RuntimeError("服务端只回了空响应")
            return out

    def _post(self, msg, timeout):
        if not self.url:
            raise RuntimeError("没填地址")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        headers.update(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=P.dumps(msg).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                raw = r.read().decode("utf-8", "replace")
                ctype = (r.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            raise RuntimeError("HTTP %s: %s" % (e.code, body))
        except Exception as e:
            raise RuntimeError("连不上: %r" % (e,))
        if "text/event-stream" in ctype:
            # 流式响应: 逐行找 data: {...}, 取第一条能解析成 JSON 的
            for line in raw.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    return json.loads(line[5:].strip())
                except Exception:
                    continue
            raise RuntimeError("流式响应里没找到 JSON")
        return json.loads(raw)

    def close(self):
        self.session_id = None


# ---------------------------------------------------------------- 对外的几张皮
def _session(spec):
    """按服务名缓存会话; 挂了就扔掉重起一次。"""
    key = _spec_key(spec)
    with _GUARD:
        sess = _SESSIONS.get(key)
    if sess is not None:
        return sess
    sess = _Http(spec) if (spec.get("transport") == "http") else _Stdio(spec)
    try:
        sess.start()
    except Exception:
        sess.close()
        raise
    with _GUARD:
        _SESSIONS[key] = sess
    return sess


def _drop(spec):
    key = _spec_key(spec)
    with _GUARD:
        sess = _SESSIONS.pop(key, None)
    if sess is not None:
        sess.close()


def _once(spec, method, params, timeout):
    """发一次请求; 会话失效(进程死了/连接断了)自动重起再试一次。"""
    try:
        return _session(spec).request(method, params, timeout=timeout)
    except Exception as e:
        _drop(spec)
        if spec.get("transport") == "http":
            # http 那边没有「进程」可重起, 重试只会把同一个请求原样再发一遍 —— 有的工具不是幂等的, 直接抛。
            raise RuntimeError(str(e))
        _err("外部服务 %s 会话失效(%s), 重起一次" % (spec.get("name"), e))
        return _session(spec).request(method, params, timeout=timeout)


def list_tools(spec):
    """列出该服务提供的工具。返回 (工具列表, 错误串) —— 错一个不影响别的服务。"""
    try:
        out, cursor = [], None
        for _ in range(20):                       # 分页保险丝, 别被服务端牵着转
            params = {"cursor": cursor} if cursor else {}
            res = _once(spec, "tools/list", params, _LIST_TIMEOUT)
            if res.get("error"):
                return [], "服务端报错: %s" % res["error"].get("message")
            r = res.get("result") or {}
            out.extend(r.get("tools") or [])
            cursor = r.get("nextCursor")
            if not cursor:
                break
        return out, None
    except Exception as e:
        return [], str(e)


_TOOLS_CACHE = {}            # 会话键 -> (取到的时刻, 工具列表)
_TOOLS_TTL = 300             # 5 分钟: 够省掉"每题都去问一遍", 又不至于让配置改动半天不生效


def tools_cached(spec):
    """列工具(带缓存)。引擎每 ask 要组工具面, 不能每次都去起一个子进程问一遍。
    **连不上就不缓存** —— 用户改好配置点一下就能立刻生效, 不用等缓存过期。"""
    key = _spec_key(spec)
    now = time.time()
    with _GUARD:
        hit = _TOOLS_CACHE.get(key)
    if hit and now - hit[0] < _TOOLS_TTL:
        return hit[1]
    tools, err = list_tools(spec)
    if err:
        _err("外部服务 %s 列工具失败: %s" % (spec.get("name"), err))
        with _GUARD:
            _TOOLS_CACHE.pop(key, None)
        return []
    with _GUARD:
        _TOOLS_CACHE[key] = (now, tools)
    return tools


def peek_tools(spec):
    """只看缓存里有没有, **不真去连**(设置页刷一次不能把外部服务挨个连一遍)。
    没测过 = None, 和"测过、但它一个工具都没有"([]) 是两回事, 别混。"""
    with _GUARD:
        hit = _TOOLS_CACHE.get(_spec_key(spec))
    return None if hit is None else hit[1]


def probe(spec):
    """设置页点「测试」走这条: 真去连一次。连上了就把清单写进缓存, 页面随即显示它提供什么。"""
    tools, err = list_tools(spec)
    if err:
        return [], err
    with _GUARD:
        _TOOLS_CACHE[_spec_key(spec)] = (time.time(), tools)
    return tools, None


def call_tool(spec, remote, args, timeout=_CALL_TIMEOUT):
    """调一个外部工具。返回 (结果体, 错误串)。结果体优先解析成 JSON 对象。"""
    try:
        res = _once(spec, "tools/call", {"name": remote, "arguments": args or {}}, timeout)
    except Exception as e:
        return None, str(e)
    if res.get("error"):
        return None, "服务端报错: %s" % res["error"].get("message")
    r = res.get("result") or {}
    text = ""
    for c in (r.get("content") or []):
        if isinstance(c, dict) and c.get("type") == "text":
            text += c.get("text") or ""
    if r.get("isError"):
        return None, text or "外部工具报错"
    if not text:
        return r, None
    try:
        return json.loads(text), None
    except Exception:
        return {"text": text}, None


# ---------------------------------------------------------------- 工具名
_SAFE = re.compile(r"[0-9A-Za-z_\-]+\Z")


def _tag(s, keep, cap, prefix):
    """把一段人起的名字压成模型认的那种名字（只许字母数字下划线短横）。

    本来就干净就**照搬** —— 这样一眼看得出是哪家服务/哪件工具；
    有中文或符号的压成一个短哈希 —— 直接换成下划线是不行的: 「我的工具」和「工具我的」
    会压成一模一样, 两个服务就串了; 而且原名还原不回来, 调的时候喊错名字。
    还原不靠反推: 谁组的名谁留一张对照表（见 engine._ext_defs）。
    """
    s = str(s or "")
    if s and len(s) <= cap and _SAFE.match(s):
        return s
    return prefix + hashlib.md5(s.encode("utf-8")).hexdigest()[:10]


def wire_name(server, remote):
    """外部工具在模型面前的名字 = mcp__<服务>__<工具>。
    加前缀一是避免和自带工具重名, 二是引擎一看前缀就知道该转给哪个外部服务。
    **只用它组名、不要拿它反推原名** —— 中文名被压成哈希了, 反推不回来。"""
    s = _tag(server, "server", 32, "s")
    t = _tag(remote, "tool", 40, "t")
    name = "mcp__%s__%s" % (s, t)
    return name if len(name) <= 64 else "mcp__%s__%s" % (s, hashlib.md5(name.encode("utf-8")).hexdigest()[:10])


def openai_def(spec, tool, wire):
    """一件外部工具 -> 模型可见的 function-calling 定义。

    说明里带上「从哪来」, 模型才能判断这答案是什么性质的东西 —— 外部工具的结果不是我们
    爬的样本, 引不成 [id=N], 这点在工具描述里就说明白, 不指望提示词兜。"""
    sname = spec.get("name") or ""
    desc = (tool.get("description") or tool.get("name") or "")[:600]
    return {"type": "function", "function": {
        "name": wire,
        "description": "[外部工具·%s] %s（结果来自外部服务, 不是我们爬的社区样本, 不能引 [id=N]）"
                       % (sname, desc),
        "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
    }}


def shutdown_all():
    with _GUARD:
        alive = list(_SESSIONS.values())
        _SESSIONS.clear()
    for s in alive:
        try:
            s.close()
        except Exception:
            pass


atexit.register(shutdown_all)
