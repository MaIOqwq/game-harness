# -*- coding: utf-8 -*-
"""会话层: 每会话持住一个 Engine 跨轮, 让 engine.ask() 对话化。
"用户"的一轮 = 一次 ask_turn(文本)。因 Engine 跨轮保留, 平台状态机(冷却/爬距)/状态卡/会话日志
在同一会话内连续 —— 这是 runner/编排(对话壳)的最小形态, 供 CLI demo、live 标定脚本、未来前台共用。
缺省 mock provider(engine 内部), 无 LLM/断网也能验证流程; 真爬由 --live / HARNESS_LIVE 开关。"""
import os

from .. import checkpoint, db, registry
from ..engine import Engine, GAME_HINTS, DEFAULT_NGA_RATIO, clamp_ratio

MODES = ("快速", "精准")


class Session:
    """一个对话会话 = 一个 Engine。跨轮对话、换话题翻篇、按档位/游戏上下文提问都走它。"""

    def __init__(self, conn=None, provider=None, mode="快速", game_hint=None, db_path=None, scope="prod",
                 nga_ratio=None, gate=None):
        self.conn = conn or db.connect(db_path or db.DEFAULT_DB)
        db.init(self.conn)
        registry.seed_builtin(self.conn)    # L4 内置黑话幂等播种(建库/换库后各跑一次, 同 alias 最新行已是则跳过)
        self.mode = mode if mode in MODES else "快速"
        self.game_hint = game_hint          # 用户钉死的游戏上下文, 优先级高于自动探测
        self.scope = scope
        self.nga_ratio = clamp_ratio(DEFAULT_NGA_RATIO if nga_ratio is None else nga_ratio)  # 本会话默认 NGA 占比%
        # gate 只有网页后端会传(跨会话共用一个实例); CLI/标定不传 = 不排队, 行为与从前一样。
        self.engine = Engine(self.conn, provider=provider, scope=scope, gate=gate)
        self._restore_checkpoint()          # L2: 续接本 scope 上次进程留下的状态卡(若有)

    def set_scope(self, scope):
        """切记忆归属(逐题隔离标定用): 透传给引擎, 卡/编号/日志/ctx 一并切换, 平台冷却保留。
        L2: 旧 scope 卡翻篇进旧日志后清其检查点, 新 scope 若留过检查点则续接。"""
        if scope != self.scope:
            checkpoint.clear(self.conn, self.scope)   # 旧 scope 内容已翻篇(在 session_claim), 检查点作废
            self.scope = scope
            self.engine.set_scope(scope)
            self._restore_checkpoint()

    def flush(self):
        """把当前 scope 仍活跃的结论翻篇进会话日志(标定收尾调用, 否则最后一题结论留内存不进库)。"""
        self.engine._flush_active()
        checkpoint.clear(self.conn, self.scope)       # 卡已空、内容已入日志, 检查点清掉防重复续接

    # ---------- 上下文解析 ----------
    def detect_game(self, text):
        """从问句里探测游戏(GAME_HINTS 已按长词优先排)。探测不到返回 None。"""
        for g in GAME_HINTS:
            if g and g in (text or ""):
                return g
        return None

    def resolve(self, text, mode=None, game=None):
        """档位: 入参 > 会话档; 游戏: 入参 > 钉死上下文 > 自动探测 > 本会话锚定的游戏。

        最后那一档是给**追问**留的: 用户接着问「那它最近呢」时题面里根本不出现游戏名,
        前两档都落空 —— 原先就退化成"未知游戏", 检索词与归档过滤全失去约束, 答案开始飘。
        锚定值来自状态卡(上一轮真爬过哪个游戏就锚哪个), 排在自动探测**之后**:
        题面点名了新游戏就听题面的, 只有题面什么都没说时才拿会话上下文兜底。"""
        m = mode if mode in MODES else self.mode
        g = game or self.game_hint or self.detect_game(text) or self.engine.card.anchor.get("game")
        return m, g

    # ---------- L2 检查点 ----------
    def _restore_checkpoint(self):
        """若本 scope 留过检查点: 恢复状态卡 + 续上结论编号(进程重启后接着上次卡走)。"""
        snap = checkpoint.load(self.conn, self.scope)
        if not snap:
            return
        card = snap.get("card")
        if card:
            self.engine.card.restore(card)
        self.engine._n = max(self.engine._n, int(snap.get("engine_n", 0)))

    def _save_checkpoint(self):
        checkpoint.save(self.conn, self.scope, {"card": self.engine.card.snapshot(),
                                                "engine_n": self.engine._n})

    # ---------- 一轮 ----------
    def ask_turn(self, text, mode=None, game=None, nga_ratio=None, cancel=None, emit=None,
                 pause=None, resume_from=None):
        """cancel = threading.Event(可选, 网页那个「停止」用的): 置位后引擎在下一个安全点收口,
        用**已经取到的证据**作答并照常落库/落流水 —— 停题不等于把这一轮白跑, 也不丢已爬到的样本。

        pause = threading.Event(可选, 网页那个「暂停」用的): 安全点同 cancel, 但它**不收口作答** ——
        这题在记忆里留成"还没答", 已爬到的样本留在库里, 用户点「继续」时从那儿接着爬(见 engine.ask)。

        resume_from = 被暂停那条流水记录(dict, 可选): 传了就是"接着上次那道题跑"。

        emit = 实时事件的回调(可选, 只有网页那条传): 传了引擎就走流式, 边跑边往外推
        轮次/工具起止/排队/正文增量, 前端据此边收边画。不传 = 一切照旧。"""
        m, g = self.resolve(text, mode, game)
        nr = self.nga_ratio if nga_ratio is None else clamp_ratio(nga_ratio)  # 单题可覆盖, 缺省=会话默认
        res = self.engine.ask(text or "", game_hint=g, mode=m, nga_ratio=nr, cancel=cancel, emit=emit,
                              pause=pause, resume_from=resume_from)
        if (res or {}).get("paused"):
            return res                              # 暂停这一跑没收口: 没答案可存, 状态卡也不动
        self._save_checkpoint()                     # L2: 每轮收口后落检查点(崩溃可续)
        res["mode"] = m
        res["game_hint"] = g
        res["claim_cid"] = "M%d" % self.engine._n   # 本轮物化结论的卡 id(供 /correct)
        return res

    # ---------- 用户纠正(反馈回路 v1: supersede-not-overwrite) ----------
    def correct(self, cid, new_concl):
        """纠正某条**仍活跃**的结论: 状态卡留痕(原文进 corrections, 活跃结论 supersede)。
        后续同线程渲染会把 corr 行带给模型。返回 (ok, msg)。"""
        card = self.engine.card
        cur = card.active.get(cid)
        if cur is None:
            return False, "cid %s 不在活跃卡(可能已翻篇/不存在), 活跃: %s" % (
                cid, ", ".join(sorted(card.active)) or "(无)")
        corr = (new_concl or "").strip()
        old = cur["concl"]      # 先取值: card.correct 是就地改写, 之后再读 cur 就是新值了
        card.correct(cid, old, corr)
        return True, "已纠正 %s: %s -> %s" % (cid, (old or "")[:24], (corr or "")[:60])

    # ---------- 状态查看(demo / 自检用) ----------
    def card_text(self):
        """L1 状态卡渲染(未翻篇结论)。"""
        return self.engine.card.render()

    def log_count(self):
        """已翻篇结论数(会话日志)."""
        return self.engine.log.count()
