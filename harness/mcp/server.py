# -*- coding: utf-8 -*-
"""MCP 服务端 —— 把自带工具按协议摆出去, 任何支持 MCP 的客户端都能连上来调。

两种接法, 同一份处理逻辑(handle):
  标准输入输出(serve_stdio)  —— 桌面 AI 客户端那种"填一行命令"的接法, 主打;
  网页后端的 POST /mcp        —— webapp.py 转进来的, 给走 HTTP 的客户端用。

工具清单直接来自 tools/registry.py —— 和引擎看到的、设置页显示的是**同一份**,
在设置页关掉哪个, 这里 tools/list 就不列哪个(结构性防线, 不是靠提示词劝)。
"""
import json
import sys
import threading

from .. import settings
settings.apply_to_env()          # 必须排在 registry 之前: measure/live 在 import 时就把环境变量读成常量了

from .. import db                                    # noqa: E402
from ..sources import live                           # noqa: E402
from ..tools import registry                         # noqa: E402
from . import protocol as P                          # noqa: E402

_LOCK = threading.Lock()
_CTX = None

_INSTRUCTIONS = (
    "游戏社区问答用的取证工具。爬 NGA 论坛与 B站 的视频/评论, 检索本机历史归档, "
    "另有可选的本地情感打分。爬取会自动归档到本机记忆库(scope=mcp)。"
)


def _ctx():
    """工具执行要的那点运行时(数据库连接)。**懒建**: 没人调工具就不碰库。"""
    global _CTX
    with _LOCK:
        if _CTX is None:
            conn = db.connect()
            db.init(conn)
            # provider 必须显式给 live.crawl: 留空会退回 mock —— 拿编造的样本回答是真爬工具最坏的失败形态
            _CTX = {"conn": conn, "provider": live.crawl, "scope": "mcp",
                    "official_provider": None}
        return _CTX


def handle(msg):
    """一条协议消息 -> 一条响应(该不该回由协议定, 通知不回 -> None)。"""
    if not isinstance(msg, dict):
        return P.error(None, P.INVALID_REQUEST, "顶层必须是对象")
    method = msg.get("method")
    if method is None:
        return None                       # 我们没发过请求, 收到响应只能忽略
    if P.is_notification(msg):
        return None                       # notifications/* 一律不回(回了会被当成乱序响应)
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return P.result(mid, {
            "protocolVersion": P.pick_protocol(params.get("protocolVersion")),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": P.SERVER_NAME, "version": P.SERVER_VERSION},
            "instructions": _INSTRUCTIONS,
        })
    if method == "ping":
        return P.result(mid, {})
    if method == "tools/list":
        return P.result(mid, {"tools": registry.mcp_tools()})
    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        try:
            payload = registry.call(_ctx(), name, args)
        except Exception as e:
            # 执行期出错按协议放进 result 里报(isError), 不是 JSON-RPC 层的错 ——
            # 这样客户端能把"这个工具失败了"如实摆给用户看, 而不是整条连接崩掉。
            return P.result(mid, P.text_result("工具 %s 执行出错: %r" % (name, e), is_error=True))
        is_err = isinstance(payload, dict) and set(payload) == {"error"}
        return P.result(mid, P.text_result(payload, is_error=is_err))
    return P.error(mid, P.METHOD_NOT_FOUND, "本服务没这个方法: %s" % method)


def handle_http(body):
    """网页后端转过来的 HTTP 体(可能是单条, 也可能是批量)。返回 (状态码, 响应体)。"""
    try:
        msg = json.loads(body or b"{}")
    except Exception as e:
        return 200, P.error(None, P.PARSE_ERROR, "不是合法 JSON: %s" % e)
    if isinstance(msg, list):
        out = [r for r in (handle(m) for m in msg) if r is not None]
        return (200, out) if out else (202, {})
    resp = handle(msg)
    return (200, resp) if resp is not None else (202, {})


def serve_stdio(stdin=None, stdout=None):
    """标准输入输出那条路。**协议通道之外的任何 print 都必须去 stderr** ——
    往真正的 stdout 多写一个字, 客户端就解析不了, 整条连接等于废了。
    所以这里把 sys.stdout 换掉: 下游模块(supervisor/live 等)照常 print, 但落到 stderr。"""
    real = stdout or sys.stdout
    saved = sys.stdout
    sys.stdout = sys.stderr
    try:
        src = stdin or sys.stdin
        for line in src:
            line = line.strip()
            if not line:
                continue
            msg, err = P.loads(line)
            resp = err or handle(msg)
            if resp is None:
                continue
            real.write(P.dumps(resp) + "\n")
            real.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        sys.stdout = saved
