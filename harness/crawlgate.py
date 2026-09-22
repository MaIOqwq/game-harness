# -*- coding: utf-8 -*-
"""跨会话的爬取闸门 —— 同一平台同一时刻只放一个人进去爬, 且两次之间要守最小间距。

**为什么不能只靠爬虫服务自己那把单飞锁**:
两个爬虫服务确实是单飞的(各自一个浏览器、一个 context), 两个人同时爬同一个平台会被 429 挡回;
可驱动侧对 429 只重试 3 次、每次隔 12 秒 —— 撑不过一次正常爬取(几十秒到几分钟), 最后回一句
"busy 重试 3 次仍 429"。对 bilibili 来说这句里带 "429", 会被 `live._raise_if_err` 判成**风控事件**:
于是**排队没抢到的那个人**被自己的会话冷却 5 分钟(还逐次翻倍) —— 明明只是手慢了一步。
一个人用的时候这条路走不到; 一旦当公用工具给人看, 它天天走。

所以把"抢单飞"改成"在门口排队": 后来的人等, 等到才进去, 不把服务端的 429 读成平台风控。

**为什么还要管间距**: 光有互斥是不够的。甲爬完一松手, 等在门口的乙立刻就爬 —— 同一账号在
几秒内连打两次, 正是风控要抓的形状。间距和互斥其实是同一件事的两面(都在描述"这个共用账号
现在能不能再被打一次"), 所以放在同一个地方管, 免得两边各算各的。

**粒度按平台各一把**: NGA 与 bilibili 是两个进程、两个浏览器、互不相干, 不该互相挡
(引擎那边也是"跨平台并发、同平台串行"的调度)。

**锁是进程内的**: 本机只跑一个 webapp 进程, 够用。哪天要多进程部署, 这里得换成跨进程的
锁(文件锁, 或干脆让爬虫服务自己排队)—— 换的时候改这一个文件就够。
"""
import threading
import time

# 引擎里那几个平台名(大写 N 的 "NGA")与这里的键是同一套 —— 两边都从注册表透传, 不另造词汇。
PLATS = ("NGA", "bilibili")


class CrawlGate:
    def __init__(self):
        self._cv = threading.Condition()
        self._held = {}     # plat -> {"scope": 谁, "since": 什么时候进去的}
        self._q = {}        # plat -> [scope, ...] 排队顺序(先进先出: 否则后到的会一直插队, 先到的饿死)
        self._last = {}     # plat -> 上一次**开始爬**的时刻(算间距用; 键在进去那一刻, 与引擎同尺)

    # ---------- 进门 / 出门 ----------
    def acquire(self, plat, scope, gap=0.0, cancel=None, pause=None):
        """在门口等到能进去为止。返回 True=进去了(**调用方必须 release**); False=等的时候被叫停。

        两件事一起等: (1) 前一位出门; (2) 距上一次开始爬已经过了 gap 秒。
        cancel/pause 都不传就是死等(CLI / 标定那条路没有那两个按钮)。

        两个信号都收: 「停止」与「暂停」在"别等了"这一步上是一回事(用户不打算让它再往下打平台了),
        区别只在下场 —— 停止落一份短答案、暂停一个字都不落 —— 那是引擎的事, 闸门只管让人等不下去。
        少收一个的后果很具体: 甲的 bili 还在爬, 乙点了「暂停」却要等在门口排队轮到它才生效,
        界面上就是"按了没反应, 像卡死"(按「停止」倒是当场就停, 于是更像坏了)。
        """
        with self._cv:
            q = self._q.setdefault(plat, [])
            mine_turn = (not self._held.get(plat)) and not q
            if mine_turn and self._gap_left(plat, gap) <= 0:
                self._enter(plat, scope)
                return True
            q.append(scope)
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        return False
                    if pause is not None and pause.is_set():
                        return False
                    if not self._held.get(plat) and q and q[0] == scope and self._gap_left(plat, gap) <= 0:
                        self._enter(plat, scope)
                        return True
                    # 0.4 秒醒一次: 既要能立刻察觉「停止」, 也要能等完最后那点间距。
                    # 间距最长 120 秒, 这点唤醒开销可以忽略 —— 这条路上本来就在等。
                    self._cv.wait(0.4)
            finally:
                try:
                    q.remove(scope)
                except ValueError:
                    pass

    def _enter(self, plat, scope):
        """记账并进去(调用方须持 self._cv)。"""
        self._held[plat] = {"scope": scope, "since": time.time()}
        self._last[plat] = time.time()

    def _gap_left(self, plat, gap):
        """离能下次开爬还差几秒(调用方须持 self._cv)。"""
        if not gap:
            return 0.0
        return max(0.0, (self._last.get(plat) or 0.0) + gap - time.time())

    def release(self, plat):
        """出门并叫醒下一位。**务必放 finally** —— 漏一次就把这个平台锁死了, 后面的人全卡在门口。"""
        with self._cv:
            self._held.pop(plat, None)
            self._cv.notify_all()

    # ---------- 给状态页看 ----------
    def view(self, plat, scope, gap=0.0):
        """**这个特定会话**眼里的这个平台: 我是不是正握着 / 我排在第几 / 还要等多久。

        刻意不把别人的会话名透出去: 页面只该知道"我排在第几", 谁在爬是别人的事。
        返回里的 gap_left 是**这台机器**上这个平台距上次开爬还剩几秒 —— 与引擎真会等的是同一个数,
        所以页面上那个倒计时不是另拍的, 是引擎真会走的时间。"""
        with self._cv:
            h = self._held.get(plat)
            q = list(self._q.get(plat) or [])
            left = self._gap_left(plat, gap)
            return {"mine": bool(h) and h.get("scope") == scope,   # 正在爬的就是我
                    "busy": bool(h),                               # 这个平台现在有人占着
                    "waiting": scope in q,                         # 我在门口排着
                    "ahead": q.index(scope) if scope in q else 0,
                    # 进一位: 倒计时显示 0 时它就该真的能爬了(与 webapp._cool_secs 同一处理)
                    "gap_left": int(left) + 1 if left > 0 else 0}

    def snapshot(self):
        """整台机器的视角(排查用, 不直接给页面)。"""
        with self._cv:
            return {p: {"held": bool(self._held.get(p)),
                        "last": self._last.get(p),
                        "queue": len(self._q.get(p) or [])} for p in PLATS}
