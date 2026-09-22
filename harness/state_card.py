# -*- coding: utf-8 -*-
"""L1 状态卡: anchor + active claims + corrections + shown。
materialize-then-prune 收口: 结论先落卡(active), 翻篇时 close() 移入会话日志。
引擎规则(在循环层执行): 一轮原文只有在该轮结论已 materialize、
且证据已写 archive 之后, 才允许被 prune; 证据/结论不在可裁剪区。"""
import time


class StateCard:
    def __init__(self, anchor=None):
        self.anchor = anchor or {"game": None, "platform": None, "as_of": None}
        self.active = {}        # cid -> {q, concl, ev, at}
        self.corrections = []   # {cid, orig, corr, at}
        self.shown = []         # 已展示过的 key, 防重复呈现

    def materialize(self, cid, q, concl, ev=""):
        self.active[cid] = {"q": q, "concl": concl, "ev": ev, "at": int(time.time())}

    def correct(self, cid, orig_concl, corr):
        # 用户纠正: 保留留痕 + supersede(原结论仍在 corrections, 不覆盖丢失)
        self.corrections.append({"cid": cid, "orig": orig_concl, "corr": corr, "at": int(time.time())})
        if cid in self.active:
            self.active[cid]["concl"] = corr

    def close(self, cid):
        """翻篇: 移出 active, 返回 payload 由调用方写入会话日志。"""
        return self.active.pop(cid, None)

    def snapshot(self):
        """L2 检查点用: 整卡深拷贝为 JSON 可序列化 dict(不引用活对象)。"""
        return {
            "anchor": dict(self.anchor),
            "active": {str(cid): dict(c) for cid, c in self.active.items()},
            "corrections": [dict(c) for c in self.corrections],
            "shown": list(self.shown),
        }

    def restore(self, snap):
        """L2 续接: 用 snapshot 的 dict 恢复整卡; 缺字段回缺省, 不炸。"""
        snap = snap or {}
        self.anchor = dict(snap.get("anchor") or {"game": None, "platform": None, "as_of": None})
        self.active = {str(cid): dict(c) for cid, c in (snap.get("active") or {}).items()}
        self.corrections = [dict(c) for c in (snap.get("corrections") or [])]
        self.shown = list(snap.get("shown") or [])

    def render(self):
        lines = ["anchor: %s" % self.anchor]
        for cid in sorted(self.active, key=lambda x: self.active[x]["at"]):
            c = self.active[cid]
            lines.append("claim %s: %s | %s | %s" % (cid, c["q"], c["concl"], c["ev"]))
        for cr in self.corrections:
            lines.append("corr %s: %s -> %s" % (cr["cid"], cr["orig"], cr["corr"]))
        return "\n".join(lines) if lines else "(空卡)"
