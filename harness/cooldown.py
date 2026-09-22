# -*- coding: utf-8 -*-
"""B站风控冷却 —— **全机器一份**, 不是每个对话一份。

为什么必须按机器算: 被平台盯上的是**这台机器的出口**(IP + 登录态 + 请求节奏), 不是某个对话。
按对话记的话, A 刚被抓、界面提示「退 5 分钟」, B 转头照爬不误 —— 这台机器的总打点速率一点没降,
再被抓一次的概率跟没退避时一样。冷却的全部意义就是让**整台机器**安静下来, 所以它得有个
不属于任何会话的落脚处, 就是这个模块。

进程内的全局态(一台机器一个后端进程, 与 crawlgate 是同一个层面的东西);
平台名不分大小写(NGA/nga 同一个键), 免得哪天别的平台也加冷却时静默查不到。
"""
import threading
import time

_LOCK = threading.Lock()
_ST = {}                       # 平台名 -> {"until": 解禁时刻, "streak": 连续被风控次数}


def _key(plat):
    return str(plat or "").strip().lower()


def _st(plat):
    k = _key(plat)
    with _LOCK:
        s = _ST.get(k)
        if s is None:
            s = _ST[k] = {"until": 0.0, "streak": 0}
        return s


def remaining(plat):
    """还要冷却几秒(不在冷却 -> 0, 不倒扣成负数)。"""
    return max(0.0, _st(plat)["until"] - time.time())


def until(plat):
    """解禁时刻(没在冷却 -> 0.0)。"""
    return _st(plat)["until"]


def streak(plat):
    return _st(plat)["streak"]


def mark_fail(plat, start, cap):
    """被风控一次: 连续失败翻倍、封顶 cap, 返回新的解禁时刻。"""
    s = _st(plat)
    with _LOCK:
        s["streak"] += 1
        s["until"] = time.time() + min(start * (2 ** (s["streak"] - 1)), cap)
        return s["until"]


def mark_ok(plat):
    """成功一次 -> 连续失败归零, 冷却当场解掉(这一问证明平台又认我们了)。"""
    s = _st(plat)
    with _LOCK:
        s["streak"] = 0
        s["until"] = 0.0


def reset():
    """清空(自检用)。"""
    with _LOCK:
        _ST.clear()
