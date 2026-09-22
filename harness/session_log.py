# -*- coding: utf-8 -*-
"""L3/L1 会话侧结论日志: 翻篇结论从状态卡移入。离上下文——默认不渲染,
只有当前问句命中旧线程话题时才 query() 按需回捞。
(回捞触发在真引擎里是 planner/读层判定, 此处用话题词命中作 stand-in。)
scope = 记忆归属; cid 主键值落库前缀 scope+'::' => 跨 scope 永不撞主键; query 按 scope 过滤并剥前缀回键。"""
import sqlite3


class SessionLog:
    def __init__(self, conn, scope="prod"):
        self.conn = conn
        self.scope = scope

    def append(self, cid, claim, topic=""):
        self.conn.execute(
            "INSERT OR REPLACE INTO session_claim(cid,q,concl,ev,moved_at,topic,scope) "
            "VALUES(?,?,?,?,?,?,?)",
            (self._key(cid), claim.get("q", ""), claim.get("concl", ""),
             claim.get("ev", ""), claim.get("at", 0), topic, self.scope))
        self.conn.commit()

    def query(self, topics):
        """按话题词回捞已翻篇结论(仅本 scope)。命中 = 话题词出现在 q/concl 文本, 或等于该条归档时的 topic 线程标签。"""
        out = {}
        rows = self.conn.execute(
            "SELECT cid,q,concl,ev,topic FROM session_claim WHERE scope=?", (self.scope,)).fetchall()
        for r in rows:
            hay = (r[1] or "") + (r[2] or "") + " " + (r[4] or "")
            if any(t and t in hay for t in topics):
                out[self._unkey(r[0])] = {"q": r[1], "concl": r[2], "ev": r[3]}
        return out

    def count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM session_claim WHERE scope=?", (self.scope,)).fetchone()[0]

    def _key(self, cid):
        return "%s::%s" % (self.scope, cid)

    @staticmethod
    def _unkey(stored):
        return stored.split("::", 1)[1] if "::" in stored else stored
