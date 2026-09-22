# -*- coding: utf-8 -*-
"""L1 上下文装配: 每轮把 系统词 + 状态卡 + 当轮grounding + 近端raw(2轮) + 按需回捞
拼成送进模型的一段。与探针 l1_budget_probe.py 的 budgeted 策略同构——
原始证据不进上下文, 只在 archive / session_log 里可回捞。"""


def assemble(system, card_render, grounding="", near_raw=(), loaded=""):
    parts = [system, card_render]
    if grounding:
        parts.append("【当轮证据】\n" + grounding)
    if near_raw:
        parts.append("【近端原文】\n" + "\n".join(near_raw[-2:]))
    if loaded:
        parts.append("【按需回捞】\n" + loaded)
    return "\n".join(parts)


def token_count(text):
    """cl100k 近似 DeepSeek tokenizer; 引擎计量用, 相对比可信。"""
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return max(1, int(len(text) / 1.7))
