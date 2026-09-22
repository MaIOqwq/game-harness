# -*- coding: utf-8 -*-
"""证据池: 会话隔离 与 "查过的还能查回来" 的分界线就画在这里。

一个 scope 键原先同时管两件事 —— (1) 对话记忆(状态卡/结论/检查点/流水) (2) 爬到的原始证据
(observation / crawl_run)。这两件事要的隔离粒度本来就不同:

  * 对话之间**必须不串**: 开了新对话就该是一张白纸, 上一轮聊到哪儿、得出过什么结论, 不许漏过来;
  * 但**同一台机器上先前爬到的料还得能查回来**: 否则同一款游戏隔天再问一遍就是从零再爬,
    既慢、又平白多打一次平台风控。

所以只把"证据"这一半按池折算, "记忆"那一半仍按 scope 原样隔离:

  * `exp:*`                         -> 自成一池, 与外界互不可见
    (标定/实验数据不许污染真实证据, 真实证据也不许被实验读到 —— 两边都要干净);
  * 其余(prod / web:xxx / api:xxx) -> 本机的真实证据池, 同一台机器上互相可读可引。

写入时 scope 列**原样记**(留出处, 复盘时看得出这条是谁爬的); 只有"读"这条路径按池折算。
"""

_EXP = "exp:"


def pool_of(scope):
    """scope -> 证据池名。'exp:*' 各自成池; 其余归 'local' 一池。"""
    s = (scope or "prod").strip() or "prod"
    return s if s.startswith(_EXP) else "local"


def pool_clause(scope, col="scope"):
    """返回 (SQL 条件片段, 参数) —— 描述"这一池里的证据", 供直接拼进 WHERE。

    只用参数占位符拼, 不把 scope 文本拼进 SQL。"""
    p = pool_of(scope)
    if p == "local":
        return "%s NOT LIKE ?" % col, [_EXP + "%"]
    return "%s = ?" % col, [p]
