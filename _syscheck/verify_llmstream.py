#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流式解析层(harness/llmstream.py)的离线自检: 纯离线, 不触网、不起服务、不碰产品库。

为什么这支脚本值得存在: 流式链路上的错**不会当场报错**, 它只是让答案悄悄坏掉 ——
参数拼丢一段、工具名被空占位擦成 ""、流被截断了却当完整回答拿去解析。这些在真跑时
要么表现为"模型偶尔抽风", 要么炸在下游的工具层, 都极难定位。所以协议细节必须逐条盯死。

盯的是这几条:
  ① SSE 分帧: 增量 UTF-8 解码(一次 read 可能停在多字节字符中间) / CRLF / 注释行 / 多行 data;
  ② tool_calls 跨分片拼接: 按 index 分桶、空占位不覆盖、arguments 字符串相加、输出按首见顺序;
  ③ 收尾认 [DONE]: 没收到的流一律判截断, 不许当完整回答;
  ④ 畸形分片/空工具名一律失败, 不把半成品递给下游;
  ⑤ usage 取回来, 且**返回体形状与非流式一致**(engine 主循环只认那个形状);
  ⑥ 真发出去的请求带着 stream + stream_options(少了它们, 整个流式链路一上线就全挂)。

用法: python _syscheck/verify_llmstream.py   退出码 0=全过, 1=有失败。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from harness import llmstream                                            # noqa: E402

fails = []
total = ok = 0


def chk(name, cond, got=""):
    global total, ok
    total += 1
    print(("PASS" if cond else "FAIL"), name)
    if cond:
        ok += 1
    else:
        fails.append("%s -> %r" % (name, got))


# ---------- 假响应体 ----------
class FakeBody:
    """按**指定的碎片长度**吐字节。step=1 就是一次只给一个字节 ——
    专治"一次 read 停在 UTF-8 多字节字符中间"这类只在这条路上出现的坑(中文答案必踩)。"""

    def __init__(self, data, step=1):
        self.b = data if isinstance(data, bytes) else data.encode("utf-8")
        self.step = step
        self.i = 0
        self.closed = False

    def read(self, n):
        k = min(n, self.step, len(self.b) - self.i)
        if k <= 0:
            return b""
        out = self.b[self.i:self.i + k]
        self.i += k
        return out

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


# ---------- 造分片 ----------
def _txt(t):
    return {"choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}]}


def _call(i, cid=None, name=None, args=None):
    """一个 tool_call 分片。cid/name/args 传 None 就是"这个字段本片不出现"(真实流里后续片就是空占位)。"""
    fn = {}
    if name is not None:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    c = {"index": i, "function": fn}
    if cid is not None:
        c["id"] = cid
    return {"choices": [{"index": 0, "delta": {"tool_calls": [c]}, "finish_reason": None}]}


def _usage(pt, ct):
    # usage 是**独立尾块**: choices 为空, 只有 usage(见 llmstream 顶部注释)
    return {"choices": [], "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                                     "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100}}


def _wire(*events, done=True):
    s = "".join("data: %s\n\n" % json.dumps(e, ensure_ascii=False) for e in events)
    return s + ("data: [DONE]\n\n" if done else "")


def _feed(wire, step=1, on_delta=None):
    """跑一遍 complete(), urlopen 换成本地假流。"""
    real = llmstream.urllib.request.urlopen
    llmstream.urllib.request.urlopen = lambda req, timeout=None: FakeBody(wire, step)
    try:
        return llmstream.complete("http://x/y", "k", {}, on_delta=on_delta)
    finally:
        llmstream.urllib.request.urlopen = real


def _feed_err(wire, step=1, on_delta=None):
    """跑一遍, 期望抛 StreamError; 返回异常(没抛就返回 None)。"""
    try:
        _feed(wire, step, on_delta)
        return None
    except llmstream.StreamError as e:
        return e


def _capture(wire, body=None, step=1):
    """跑一遍 complete(), 顺手把**真发出去的请求**留下来 —— 请求头/体对不对, 只有这儿看得见。"""
    box = {}
    real = llmstream.urllib.request.urlopen

    def fake(req, timeout=None):
        box["body"] = json.loads((req.data or b"{}").decode("utf-8"))
        box["accept"] = req.headers.get("Accept")
        return FakeBody(wire, step)

    llmstream.urllib.request.urlopen = fake
    try:
        llmstream.complete("http://x/y", "k", {} if body is None else body)
    finally:
        llmstream.urllib.request.urlopen = real
    return box


# ---------- ① SSE 分帧 ----------
# 逐字节喂: 中文一定要跨 read 边界, 增量解码器没写对这里就抛 UnicodeDecodeError。
wire = _wire(_txt("你好，世界"), _txt("！"))
r = _feed(wire, step=1)
chk("①-1 逐字节喂(中文跨 read 边界)也能拼回正文",
    r["choices"][0]["message"]["content"] == "你好，世界！", r["choices"][0]["message"]["content"])

# CRLF + 注释行(保活心跳) + 无空格的 data:
wire = ("data: " + json.dumps(_txt("A")) + "\r\n\r\n"
        ": ping\r\n"
        "data:" + json.dumps(_txt("B")) + "\r\n\r\n"
        "data: [DONE]\r\n\r\n")
r = _feed(wire, step=3)
chk("①-2 CRLF / 注释行 / 无空格的 data: 都认",
    r["choices"][0]["message"]["content"] == "AB", r["choices"][0]["message"]["content"])

# 多行 data: 按规范用换行拼起来(不能当成两条事件)
body = FakeBody('data: {"a":\ndata: 1}\n\n', step=2)
chk("①-3 多行 data: 用换行拼成一条载荷",
    list(llmstream._iter_sse(body)) == ['{"a":\n1}'], list(llmstream._iter_sse(FakeBody('data: {"a":\ndata: 1}\n\n'))))

# 一行都没有(纯心跳) -> 一条都不该吐
chk("①-4 只有注释心跳不算数据",
    list(llmstream._iter_sse(FakeBody(": ping\n: pong\n"))) == [], "")

# ---------- ② tool_calls 跨分片拼接 ----------
# 两个调用**交错**到达 + 后续分片带空占位: 按到达顺序 append 会丢参数, 覆盖写法会擦掉工具名。
wire = _wire(
    _call(0, cid="call_a", name="crawl_nga", args='{"game":'),
    _call(1, cid="call_b", name="crawl_bili", args='{"game":'),
    _call(0, cid="", name="", args='"崩铁",'),          # 空占位: 必须当"没变"
    _call(1, cid="", name="", args='"崩铁",'),
    _call(0, args='"query":"深渊"}'),
    _call(1, args='"query":"深渊"}'),
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
)
r = _feed(wire, step=1)
tcs = r["choices"][0]["message"]["tool_calls"]
chk("②-1 交错分片的两个 tool_call 各拼各的(arguments 字符串相加)",
    [t["function"]["arguments"] for t in tcs] == ['{"game":"崩铁","query":"深渊"}'] * 2,
    [t["function"]["arguments"] for t in tcs])
chk("②-2 空占位不覆盖首值(id/name 保住)",
    [t["function"]["name"] for t in tcs] == ["crawl_nga", "crawl_bili"]
    and [t["id"] for t in tcs] == ["call_a", "call_b"],
    [(t["id"], t["function"]["name"]) for t in tcs])
chk("②-3 拼出来的 arguments 是真 JSON(下游 json.loads 能直接用)",
    all(json.loads(t["function"]["arguments"]) == {"game": "崩铁", "query": "深渊"} for t in tcs), "")
chk("②-4 正文与工具调用同轮时不串味", r["choices"][0]["message"]["content"] is None,
    r["choices"][0]["message"]["content"])

# 输出顺序按**首见**顺序, 不是 index 字典序(并行调用时两者会不一样)
wire = _wire(_call(1, cid="b", name="crawl_bili", args="{}"),
             _call(0, cid="a", name="crawl_nga", args="{}"))
r = _feed(wire, step=1)
chk("②-5 回填顺序按首见顺序(index=1 先到就排前面)",
    [t["id"] for t in r["choices"][0]["message"]["tool_calls"]] == ["b", "a"],
    [t["id"] for t in r["choices"][0]["message"]["tool_calls"]])

# 完全没给 id 的流: 得补一个非空 id, 不然回灌历史时 tool_call_id 会对不上
wire = _wire(_call(0, name="crawl_nga", args="{}"))
r = _feed(wire, step=1)
chk("②-6 流里没给 id 时补一个非空 id(回灌历史要用)",
    bool(r["choices"][0]["message"]["tool_calls"][0]["id"]), "")

# ---------- ③ 收尾认 [DONE] ----------
chk("③-1 没等到 [DONE] 的流判截断(不许当完整回答)",
    _feed_err(_wire(_txt("半截话"), done=False)) is not None, "")
# 截断的那半截**不能**被当成功果吐出去 —— 上面已在 complete 里抛错, 这里确认错误文案点到点子
e = _feed_err(_wire(_txt("半截话"), done=False))
chk("③-2 截断错误说清是截断", e is not None and "截断" in str(e), e)

# ---------- ④ 畸形一律失败 ----------
chk("④-1 分片不是合法 JSON -> StreamError",
    _feed_err("data: {oops\n\ndata: [DONE]\n\n") is not None, "")
chk("④-2 有 tool_call 但没拼出工具名 -> StreamError(绝不递空工具名给下游)",
    _feed_err(_wire(_call(0, cid="x", args="{}"))) is not None, "")

# ---------- ⑤ usage 与返回体形状 ----------
on = []
wire = _wire(_txt("一"), _txt("二"), _usage(1234, 56))
r = _feed(wire, step=1, on_delta=on.append)
chk("⑤-1 正文增量逐段回调(前端边收边画靠它)", on == ["一", "二"], on)
chk("⑤-2 usage 从尾块取回(含缓存命中/未命中拆分, 计费要用)",
    r["usage"].get("prompt_tokens") == 1234 and r["usage"].get("completion_tokens") == 56
    and r["usage"].get("prompt_cache_hit_tokens") == 900, r.get("usage"))
chk("⑤-3 返回体形状与非流式一致(主循环只认这个形状)",
    set(r) >= {"choices", "usage"} and set(r["choices"][0]) >= {"message", "finish_reason"}
    and set(r["choices"][0]["message"]) >= {"role", "content"}, sorted(r))
# 服务商不支持 include_usage 时: 不能编一个 usage 出来, 回空 dict 就好
r = _feed(_wire(_txt("x")), step=1)
chk("⑤-4 拿不到 usage 时回空 dict(宁可显示 0, 不编数)", r["usage"] == {}, r["usage"])

# ---------- ⑥ 发出去的请求: stream + stream_options 必须一起带 ----------
# 这一条是**网上真踩出来的**: 曾经只有解析层, 没人往请求体里塞 stream —— 服务端老老实实按非流式
# 回整段 JSON, 这边一条 SSE 都拆不出来, 一律判"截断"。本地假流永远测不出来(假流是手写的),
# 只有钉住"真发出去的那一份", 才拦得住这种"看着全对、一上线就全挂"的坏法。
sent = {"model": "m", "temperature": 0.3, "messages": []}
box = _capture(_wire(_txt("x")), sent)
chk("⑥-1 请求体带 stream: true(不带的话服务端按非流式回, 这边一律判截断)",
    box["body"].get("stream") is True, box["body"])
chk("⑥-2 请求体带 stream_options.include_usage(不写这条, 流式下拿不到 usage)",
    (box["body"].get("stream_options") or {}).get("include_usage") is True, box["body"])
chk("⑥-3 不就地改调用方那份 body(引擎那份下一轮还要接着用)",
    "stream" not in sent and "stream_options" not in sent, sent)
chk("⑥-4 请求头声明收 SSE(中间层据此不缓冲)", box["accept"] == "text/event-stream", box["accept"])

print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !!", f)
sys.exit(1 if fails else 0)
