#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页后端「按会话缓存 Session」的自检(纯离线, 不起服务、不起浏览器、不碰产品库)。

盯的是这几条 —— 每条都对应一个"改错了会静默出错"的坏法:
  ① sid -> scope 的折算: 没带 session 的请求落到一个固定的匿名 scope(不是各建各的);
  ② 同一个 sid 反复取到**同一个** Session 对象(否则每轮都新开一张状态卡, 追问就断了);
  ③ 超出上限按"最久没用"回收, 最近用过的活着;
  ④ **正在跑题的会话不许回收** —— 那一轮的卡还在内存里, 抽走 = 答到一半的上下文丢了;
  ⑤ 查状态(每几秒一次)不许顺手把 Session 建出来(那会把空 scope 写进记忆库)。

Session 用假的替身(不落盘、不开库) —— 这里测的是缓存与回收的规矩, 不是引擎。

用法: python _syscheck/verify_session_lru.py   退出码 0=全过, 1=有失败。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

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


from harness import webapp as W                                   # noqa: E402


class FakeSession:
    """替身: 只记 scope, 不建库不读配置。"""
    made = []

    def __init__(self, conn=None, provider=None, mode="快速", game_hint=None,
                 db_path=None, scope="prod", nga_ratio=None, gate=None):
        self.scope = scope
        FakeSession.made.append(scope)


W.Session = FakeSession

# ---------- ① sid -> scope ----------
chk("①-1 带 sid 落到 web:<sid>", W._scope("abc") == "web:abc", W._scope("abc"))
chk("①-2 没带 session 用同一个匿名 scope(不是各建各的)",
    W._scope(None) == W._scope("") == W._scope("anon") == "web:anon", W._scope(None))

# ---------- ② 同一个 sid 反复取到同一个对象 ----------
W._SESSIONS.clear()
FakeSession.made = []
a1 = W._session("s1")
a2 = W._session("s1")
chk("②-1 同一个 sid 取到同一个 Session", a1 is a2, (id(a1), id(a2)))
chk("②-2 而且只建了一次", FakeSession.made.count("web:s1") == 1, FakeSession.made)
chk("②-3 不同 sid 拿到不同对象", W._session("s2") is not a1, "")

# ---------- ③ 超上限按最久没用回收 ----------
W._SESSIONS.clear()
W._LOCKS.clear()
FakeSession.made = []
first = None
for i in range(W.SESSION_CAP):
    s = W._session("k%d" % i)          # k0 最老, k11 最新
    if i == 0:
        first = s
W._session("k0")                        # 回头用一下 k0 -> 它变成"最近用过"
newest = W._session("extra")            # 这一下超上限, 该踢掉"最久没过"的那个(k1)
chk("③-1 还留在上限内", len(W._SESSIONS) <= W.SESSION_CAP, len(W._SESSIONS))
chk("③-2 最近用过的 k0 没被踢", W._session("k0") is first, "")
chk("③-3 最久没用的 k1 被踢了(取它是新建的, 不是原来那个)",
    "web:k1" not in W._SESSIONS or FakeSession.made.count("web:k1") > 1, list(W._SESSIONS))
chk("③-4 新来的没被顺手踢掉", W._session("extra") is newest, "")

# ---------- ④ 正在跑题的会话不许回收 ----------
W._SESSIONS.clear()
W._LOCKS.clear()
FakeSession.made = []
for i in range(W.SESSION_CAP):
    W._session("d%d" % i)
busy = W._session("d0")                 # 最老的那一个
lock = W._session_lock("web:d0")
lock.acquire()
try:
    W._session("another")               # 触发回收: d0 最老, 但它锁着 -> 必须跳过
    chk("④-1 锁着的会话没被回收", W._session("d0") is busy, list(W._SESSIONS))
    chk("④-2 锁着的会话还在表里", "web:d0" in W._SESSIONS, list(W._SESSIONS))
    chk("④-3 别的会话照常被回收(不是干脆不回收了)", len(W._SESSIONS) <= W.SESSION_CAP,
        len(W._SESSIONS))
finally:
    lock.release()

# ---------- ⑤ 查状态只读不建 ----------
W._SESSIONS.clear()
FakeSession.made = []
n = W._cool_secs("never-seen", "nga")
chk("⑤-1 冷却查询对没见过的会话回 0 而不是抛错", n == 0, n)
chk("⑤-2 而且没顺手建出 Session 来", not FakeSession.made and not W._SESSIONS,
    (FakeSession.made, list(W._SESSIONS)))
W._cool_left("never-seen")
chk("⑤-3 整个冷却面板查一遍也不建(页面几秒刷一次)", not FakeSession.made, FakeSession.made)

# ---------- ⑥ 锁: 同 scope 同一把, 跨 scope 各一把 ----------
chk("⑥-1 同 scope 拿到同一把锁", W._session_lock("web:x") is W._session_lock("web:x"), "")
chk("⑥-2 不同 scope 不是同一把", W._session_lock("web:x") is not W._session_lock("web:y"), "")

print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !!", f)
sys.exit(1 if fails else 0)
