# -*- coding: utf-8 -*-
"""爬虫闸门(crawlgate): 同平台互斥 / 排队先进先出 / 爬距 / 「停止·暂停」能当场把人从队里叫走。

为什么这门必须单独验: 它是**跨会话**那个"公用"保证的落点(见 crawlgate.py 开头)。引擎侧的同轮
调度自检覆盖过了, 闸门本身在这之前**一条断言都没有** —— 它要是悄悄坏了(比如 release 漏了叫醒、
被叫停的人没从队里除名), 表现是"某个平台从此谁都爬不了"或"排队的人凭空消失", 而且**只在两个
人同时用时才现形**, 一个人自己用永远撞不到。这里全是线程和时序, 不碰网络、不碰爬虫服务。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness import crawlgate                                            # noqa: E402

fails = []
n_chk = 0


def chk(name, cond, extra=""):
    global n_chk
    n_chk += 1
    print("  %s %s" % ("ok " if cond else "XX ", name))
    if not cond:
        fails.append("%s | %s" % (name, extra))


print("=== 1. 互斥 + 先进先出 ===")
g = crawlgate.CrawlGate()
inside, order = [], []


def worker(tag, hold):
    if not g.acquire("bilibili", tag, gap=0.0):
        return
    inside.append(tag)
    order.append(tag)
    time.sleep(hold)
    inside.pop()
    g.release("bilibili")


t1 = threading.Thread(target=worker, args=("a", 0.6))
t1.start()
time.sleep(0.15)                      # 让 a 先进去
t2 = threading.Thread(target=worker, args=("b", 0.0))
t2.start()
time.sleep(0.25)
chk("1.1 第二个人在门口等着, 没有同时进去", inside == ["a"], inside)
t1.join(5)
t2.join(5)
chk("1.2 先到的先进、后到的后进", order == ["a", "b"], order)
chk("1.3 都出门了, 平台空出来", g.snapshot()["bilibili"]["queue"] == 0
    and not g.snapshot()["bilibili"]["held"], g.snapshot())

print("=== 2. 爬距: 前一位出门之后也不是立刻就能进 ===")
g = crawlgate.CrawlGate()
g.acquire("bilibili", "a", gap=0.0)
g.release("bilibili")
t0 = time.time()
g.acquire("bilibili", "b", gap=0.5)
dt = time.time() - t0
chk("2.1 真等满了间距才进去", dt >= 0.45, "等了 %.2fs" % dt)
g.release("bilibili")
t0 = time.time()
g.acquire("NGA", "c", gap=0.0)
chk("2.2 NGA 没有爬距这回事 -> 等了就进", time.time() - t0 < 0.3, "")
g.release("NGA")

print("=== 3. 跨平台互不挡 ===")
g = crawlgate.CrawlGate()
g.acquire("NGA", "a", gap=0.0)
t0 = time.time()
got = g.acquire("bilibili", "a", gap=0.0)
chk("3.1 NGA 占着, bilibili 照进(两个进程两个浏览器)", got and time.time() - t0 < 0.3,
    "%.2fs" % (time.time() - t0))
g.release("bilibili")
g.release("NGA")

print("=== 4. 「停止」把排队的人当场叫走 ===")
g = crawlgate.CrawlGate()
g.acquire("bilibili", "holder", gap=0.0)          # 占着不放
ev = threading.Event()
res = {}


def waiter_stop():
    res["r"] = g.acquire("bilibili", "me", gap=0.0, cancel=ev)


t = threading.Thread(target=waiter_stop)
t.start()
time.sleep(0.25)
chk("4.1 排队的人这会儿还在等", "r" not in res, res)
ev.set()
t.join(3)
chk("4.2 「停止」一置旗, 他不再等 -> 返回 False(这次算没发出去)", res.get("r") is False, res)
chk("4.3 被叫走的人从队里除名了(不会烂在队里挡住后面的人)",
    g.snapshot()["bilibili"]["queue"] == 0, g.snapshot())
g.release("bilibili")

print("=== 5. 「暂停」同样能把排队的人叫走 ===")
# 这条是这次补的洞: 从前闸门只认「停止」, 前面那个人还在爬时按「暂停」要等自己进门才生效 ——
# 界面表现就是"按了没反应, 像卡死"(按「停止」倒是当场停, 于是更像坏了)。
g = crawlgate.CrawlGate()
g.acquire("bilibili", "holder", gap=0.0)
pv = threading.Event()
res2 = {}


def waiter_pause():
    res2["r"] = g.acquire("bilibili", "me", gap=0.0, cancel=None, pause=pv)


t = threading.Thread(target=waiter_pause)
t.start()
time.sleep(0.25)
chk("5.1 排队的人这会儿还在等", "r" not in res2, res2)
pv.set()
t.join(3)
chk("5.2 「暂停」一置旗, 他也不再等 -> 返回 False", res2.get("r") is False, res2)
chk("5.3 同样从队里除名", g.snapshot()["bilibili"]["queue"] == 0, g.snapshot())
g.release("bilibili")

print("=== 6. 出门叫醒下一位(漏了这一步 = 平台被锁死) ===")
g = crawlgate.CrawlGate()
g.acquire("bilibili", "holder", gap=0.0)
woke = []


def waiter_next():
    if g.acquire("bilibili", "me", gap=0.0):
        woke.append(time.time())


t = threading.Thread(target=waiter_next)
t.start()
time.sleep(0.25)
chk("6.1 前一位没出门时, 他进不来", not woke, woke)
g.release("bilibili")
t.join(3)
chk("6.2 前一位一出门就把他叫醒", bool(woke), woke)
g.release("bilibili")

print("=== 7. 状态视图: 页面那盏「排队中/正在使用」的灯 ===")
g = crawlgate.CrawlGate()
g.acquire("bilibili", "a", gap=0.0)
va = g.view("bilibili", "a", gap=0.0)
chk("7.1 正在爬的人: mine + busy 都真", va["mine"] is True and va["busy"] is True, va)
vb = g.view("bilibili", "b", gap=0.0)
chk("7.2 别人看: busy 但 mine=false", vb["busy"] is True and vb["mine"] is False, vb)
chk("7.3 没在队里就不该说自己排着", vb["waiting"] is False, vb)
g.release("bilibili")
v3 = g.view("bilibili", "a", gap=0.0)
chk("7.4 出门之后不忙", v3["busy"] is False, v3)

print("-" * 56)
print("chk=%d PASS=%d FAIL=%d" % (n_chk, n_chk - len(fails), len(fails)))
for f in fails:
    print("  FAIL:", f)
sys.exit(1 if fails else 0)
