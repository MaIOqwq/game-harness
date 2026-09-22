# -*- coding: utf-8 -*-
"""流式补全的解析层: 把 SSE 增量流拼回一份**与非流式同形状**的响应体。

为什么单起一个文件: engine 主循环只认 `resp["choices"][0]["message"]` 与 `resp["usage"]`。
把流式拼成同一个形状, 主循环一行都不用改, 协议上的风险全圈在这里(见 harness/test.py 的离线用例)。

三条不能想当然的协议细节(照 deepseek-ai/deepseek-harness 的 translate.ts 核实过, 不是猜的):
  ① `stream: true` 必须搭 `stream_options: {"include_usage": true}` —— 不写这个, 流式下**拿不到 usage**,
     连 token 用量都没有(非流式才是默认带)。
  ② tool_calls 是**跨分片**的: 按 `index` 分桶累积; `id`/`function.name` 只在**非空**时赋值
     (后续分片带 "" 表示"没变", 若写成"有值就覆盖"会被空占位擦掉首个有效值 -> 组装出空工具名);
     `function.arguments` 是字符串直接相加。按到达顺序 append 会在**并行调用**时丢参数。
  ③ 收尾认 `data: [DONE]`, **不认** finish_reason(finish_reason 只是记一下)。没等到 [DONE] 就 EOF
     = 截断, 一律当失败 —— 当成功的话, 半截的 arguments 会被 json.loads 硬解, 炸在工具层,
     比在模型层炸难查得多(这是流式最容易埋的一个雷)。
"""
import codecs
import json
import urllib.request

DONE = "[DONE]"


class StreamError(Exception):
    """流式链路上的失败(截断/畸形/空工具名)。调用方按"这次调用失败"处理, 可重试。"""


def _lines(resp):
    """把响应体切成一串**完整行**。增量解码是必须的: 一次 read 可能停在 UTF-8 多字节字符中间,
    直接 decode 会抛 UnicodeDecodeError(而且崩在中文答案上, 最难复现)。"""
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    buf = ""
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buf += dec.decode(chunk)
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            yield line.rstrip("\r")
    buf += dec.decode(b"", True)             # 收尾: 冲掉解码器里剩的字节
    if buf:
        yield buf.rstrip("\r")


def _iter_sse(resp):
    """按 SSE 规范拆帧, 只吐 data 载荷。

    空行才 dispatch(多行 `data:` 用换行拼); `:` 开头是注释行 —— 那是中间层的心跳, 不是数据,
    当数据解会立刻炸成"不是合法 JSON"。"""
    data = []
    for line in _lines(resp):
        if line == "":
            if data:
                yield "\n".join(data)
                data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield "\n".join(data)


class _Accum:
    """一轮流式响应的累积器: 正文 + 按 wire index 分桶的 tool_calls。"""

    def __init__(self):
        self.content = []
        self.calls = {}      # wire index -> {"id","name","arguments"}
        self.order = []      # 首次见到该 index 的顺序(回填时按它排, 不按字典序)
        self.usage = None
        self.finish = None

    def feed(self, payload, on_delta):
        obj = json.loads(payload)
        if isinstance(obj.get("usage"), dict):
            # usage 可能挂在 finish 那个分片上, 也可能是一个只有 usage 的尾块 -> 取最后一个
            self.usage = obj["usage"]
        for ch in obj.get("choices") or []:
            if ch.get("finish_reason"):
                self.finish = ch["finish_reason"]
            d = ch.get("delta") or {}
            if d.get("content"):
                self.content.append(d["content"])
                if on_delta:
                    on_delta(d["content"])
            # reasoning_content(思维链)有意不取: 我们不作答用它, 而且它的首个分片常是空串,
            # 拿空串开块会凭空多出一段空内容。
            for c in d.get("tool_calls") or []:
                idx = c.get("index")
                if idx is None:
                    idx = 0
                b = self.calls.get(idx)
                if b is None:
                    b = {"id": "", "name": "", "arguments": ""}
                    self.calls[idx] = b
                    self.order.append(idx)
                fn = c.get("function") or {}
                if isinstance(c.get("id"), str) and c["id"]:
                    b["id"] = c["id"]
                if isinstance(fn.get("name"), str) and fn["name"]:
                    b["name"] = fn["name"]
                b["arguments"] += fn.get("arguments") or ""

    def message(self):
        m = {"role": "assistant", "content": "".join(self.content) or None}
        if not self.order:
            return m
        calls = []
        for n, i in enumerate(self.order):
            b = self.calls[i]
            if not b["name"]:
                # 真到这儿说明拼接规则被改坏了(空占位擦了首个有效值)。宁可这一炮失败,
                # 也不能把空工具名递给下游 —— 那会变成"unknown tool """ 这种查无可查的错。
                raise StreamError("第 %d 个 tool_call 没拼出工具名(分片拼接出错)" % (n + 1))
            calls.append({"id": b["id"] or "call_%d" % n, "type": "function",
                          "function": {"name": b["name"], "arguments": b["arguments"]}})
        m["tool_calls"] = calls
        return m


def complete(url, key, body, on_delta=None, timeout=120):
    """发一次流式请求, 返回**与非流式同形状**的响应体: {"choices":[{"message":...,"finish_reason":...}], "usage":{}}。

    on_delta(text) 每收到一段正文就回调一次(前端据此边收边画)。失败抛 StreamError。
    timeout 是**单次读**的超时(socket 级), 等于一个空闲看门狗: 上游卡住超过这个秒数就断,
    不会挂着不动 —— 爬虫那几分钟是模型流已关闭之后的等待, 不在这条路上。

    usage 拿不到时回空 dict(有些服务商不支持 include_usage)。那时 token 用量会显示成 0,
    这是"取不到"而不是"没花钱" —— 别把它当真实花费。"""
    # ① 这两个开关**只能在这里一起加**: 少了 stream, 服务端按非流式回(整段 JSON), 这边一条
    # SSE 都拆不出来, 一律判截断; 少了 stream_options, 流是流了但拿不到 usage。
    # 复制一份再改: 引擎那份 body 下一轮还要接着用。
    body = dict(body)
    body["stream"] = True
    body.setdefault("stream_options", {"include_usage": True})
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                 "Accept": "text/event-stream"})
    acc = _Accum()
    seen_done = False
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for payload in _iter_sse(r):
            if payload == DONE:
                seen_done = True
                break
            try:
                acc.feed(payload, on_delta)
            except StreamError:
                raise
            except Exception as e:
                raise StreamError("流里有一段不是合法 JSON: %s" % e)
    if not seen_done:
        raise StreamError("流在 [DONE] 之前就断了(截断): 不能当完整回答用")
    return {"choices": [{"message": acc.message(), "finish_reason": acc.finish or "stop"}],
            "usage": acc.usage or {}}
