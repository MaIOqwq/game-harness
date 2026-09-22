# -*- coding: utf-8 -*-
"""MCP 的线上格式: JSON-RPC 2.0, 一条消息一行(换行分隔的 UTF-8), 走标准输入输出。

**为什么自己写而不装官方的 mcp 包**: 这个产品是"下载下来就能装"的, 装一份深度学习
依赖链的事已经干过一次(小模型那条线), 不想再来一次; 而这里用到的协议面很窄 ——
握手 + 列工具 + 调工具, 标准库就够。协议细节以官方 spec 为准, 这里只实现这几条,
不认识的请求一律如实回"没这个方法", 不假装支持。

方法(请求/响应)与通知(单向, 不需要回)严格分开: 通知回了消息, 客户端会当成乱序响应。
"""
import json

# 我们认的协议版本(从新到旧)。客户端报的版本在表里就照它回, 不在就回我们最新的 ——
# 这是 spec 允许的做法, 客户端据此自己决定要不要降级。
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL = PROTOCOL_VERSIONS[0]

SERVER_NAME = "gameharness"
SERVER_VERSION = "1.0"

# JSON-RPC 2.0 错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def dumps(obj):
    """协议消息一律单行 UTF-8 —— 消息里绝不能出现裸换行, 否则对端读串。"""
    return json.dumps(obj, ensure_ascii=False)


def loads(line):
    """解析一行。返回 (消息, None) 或 (None, 错误响应)。"""
    try:
        msg = json.loads(line)
    except Exception as e:
        return None, error(None, PARSE_ERROR, "不是合法 JSON: %s" % e)
    if not isinstance(msg, dict):
        return None, error(None, INVALID_REQUEST, "顶层必须是对象")
    return msg, None


def result(msg_id, value):
    return {"jsonrpc": "2.0", "id": msg_id, "result": value}


def error(msg_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def is_notification(msg):
    """通知 = 没有 id 的方法调用(如 notifications/initialized)。这种不回消息。"""
    return "id" not in msg


def pick_protocol(wanted):
    """客户端想要的版本我们能给就给, 给不了回自己最新的。"""
    return wanted if wanted in PROTOCOL_VERSIONS else LATEST_PROTOCOL


def text_result(payload, is_error=False):
    """工具返回体 -> MCP 的 tools/call 结果。
    内容按 spec 走 content 数组; 我们把结构化结果整份 JSON 放进 text 里 ——
    这是所有客户端都认的那一种, 不赌谁支持 structuredContent。"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    out = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out
