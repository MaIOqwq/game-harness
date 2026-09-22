# -*- coding: utf-8 -*-
"""L2 检查点: 会话状态卡跨进程续接。

会话层(可软删, ARCHITECTURE §5.1): 每 scope 存一份"当前进度"(状态卡 + 引擎结论编号)的 JSON,
Engine/进程重启后 Session 恢复即接着上次的卡继续, 不丢未翻篇结论。
只增契约下 save 与 clear 都用 INSERT OR REPLACE 覆盖同 scope 一行 —— 不留破坏性 SQL(guard 放行),
物理清除(真删行)是前台"删除会话记忆"按钮的独立任务, 不在这。

用法(Session 壳调用, 不在 engine 内):
  save(conn, scope, {"card": card.snapshot(), "engine_n": n})   每轮 ask_turn 后
  load(conn, scope)   -> dict | None(无检查点 / 已清 / 表未建)
  clear(conn, scope)  翻篇/flush/切 scope 后: 存空行, 下次 load 即 None
表不存在(未 init)时安全回落, 不炸主链。
"""
import json
import time


def save(conn, scope, snap):
    """落/覆写本 scope 检查点(INSERT OR REPLACE, 每 scope 一行)。"""
    conn.execute(
        "INSERT OR REPLACE INTO checkpoint (scope, card, saved_at) VALUES (?,?,?)",
        (scope, json.dumps(snap or {}, ensure_ascii=False), int(time.time())))
    conn.commit()


def load(conn, scope):
    """取本 scope 检查点 -> dict | None。空行/表未建/坏 JSON 都回落 None。"""
    try:
        row = conn.execute(
            "SELECT card FROM checkpoint WHERE scope=?", (scope,)).fetchone()
    except Exception:
        return None
    if not row or not row["card"]:
        return None
    try:
        return json.loads(row["card"])
    except (ValueError, TypeError):
        return None


def clear(conn, scope):
    """清本 scope 检查点(软删语义): 存空行 -> 下次 load 返回 None, 行留着无妨。"""
    conn.execute(
        "INSERT OR REPLACE INTO checkpoint (scope, card, saved_at) VALUES (?,?,?)",
        (scope, "", int(time.time())))
    conn.commit()
