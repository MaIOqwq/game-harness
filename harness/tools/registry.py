# -*- coding: utf-8 -*-
"""自带工具的唯一注册表 —— 一处定义, 三处消费。

三个消费者读的是同一份清单, 所以「工具叫什么、收什么参数、开没开」永远不会三处打架:
  ① 引擎(engine._tool_defs)  —— 每 ask 按这份清单 + 开关, 组模型可见的工具面;
  ② MCP 服务端(mcp/server.py) —— 外部客户端 tools/list 拿到的就是这份(按协议换个外壳);
  ③ 设置页「工具」栏         —— 每项一个开关, 显示名/说明/能不能用都从这来。

一条 entry =
  name      工具名(模型与 MCP 都用它)
  label     页面上显示的中文名
  env       受哪个环境开关管(settings.REGISTRY 里的 key)
  optional  True = 默认不开(用户想用才开)
  desc      给模型看的说明(也是 MCP 的 description)
  schema    JSON Schema 入参。**只存一份** —— OpenAI 的 parameters 与 MCP 的 inputSchema
            是同一个东西, 复制两份迟早对不上。
  needs()   本机依赖缺了就返回一句人话(页面照实显示), 齐了就返回 None
  call(ctx, args)  真干活。引擎对爬取类另有并发/闸门处理, 不走这里; 这里给 MCP 服务端用。
"""
import os

from . import crawl_live, search_archive
from .. import measure
from ..sources import live


def _on(env_key, default="1"):
    v = os.environ.get(env_key)
    return (default if v in (None, "") else str(v)) != "0"


def _nlp_needs():
    return None if live.measure_mode() == "model" else \
        "本机没检测到打分服务: 这一项现在用不了。填了「打分服务地址 + 令牌」并起好服务后可用。"


# ---------------------------------------------------------------- 各工具的实现
def _call_crawl(ctx, args):
    return crawl_live.call(ctx, args)


def _call_official(ctx, args):
    return crawl_live.commit(ctx, args, crawl_live.official(ctx, args))


def _call_nlp(ctx, args):
    """给一批文本打情绪/反讽。**不可用时不假装**: 不退回关键词词表(那只有三档、看不出来),
    直说没有软信号, 让调用方自己按原文判。"""
    texts = [str(t) for t in (args.get("texts") or []) if str(t or "").strip()]
    if not texts:
        return {"error": "texts 是空的: 给几条要打分的文本(如社区评论原文)"}
    if live.measure_mode() != "model":
        return {"error": "本机没有可用的打分服务, 这批文本没有软信号 —— 请按原文自行判断, "
                         "别把这次调用当成'已判过情绪'"}
    got = measure._nlp_measure(texts)
    if got is None:
        return {"error": "打分服务请求失败(服务没起/令牌不对/超时), 这批文本没有软信号 —— "
                         "请按原文自行判断, 别当作'已判过情绪'"}
    thr = getattr(measure, "_MODEL_TH", 0.6)
    items = []
    for t in texts:
        items.append({"senti": None, "sarcasm": None, "tags": measure.topics(t)})
    for i, m in enumerate(got[:len(items)]):
        try:
            p = float(m.get("sarcasm_prob", 0.0))
        except (TypeError, ValueError):
            continue
        items[i]["senti"] = {0: "负", 1: "中", 2: "正"}.get(int(m.get("senti_arg", 1)))
        items[i]["sarcasm"] = 1 if p >= thr else 0
    return {"source": "本地小模型打分服务(structbert 情绪 + 反讽概率)",
            "note": "senti=情绪(负/中/正), sarcasm=1 表示判为可疑反讽(正话反说)。"
                    "这是**初筛信号**, 精度有限, 判向最终以原文为准。",
            "items": items}


def _recall_answer(ctx, args):
    """本会话早前某一轮的**原话**回捞(按 scope 读流水)。

    追问接不住, 多半不是因为忘了"结论是什么", 而是给不出"当时具体怎么说的" ——
    纪要只留一行摘要, 而答案全文里才有细节(哪个版本、哪条帖子、怎么措辞的)。
    这里就是那份全文的取回口。**只读本会话**的流水文件: 别的会话的题捞不到。
    """
    from .. import flow
    scope = (ctx or {}).get("scope")
    if not scope:
        return {"error": "没有会话标识, 回捞不到(这是引擎侧才有的东西)"}
    recs = flow.records(scope)
    if not recs:
        return {"error": "本会话还没有答过的题(流水是空的), 没有可回捞的东西"}

    # index = 会话内第几题(从 1 起)。给了它就精确取那一条, 不再按词打分。
    idx = args.get("index")
    if idx not in (None, ""):
        try:
            i = int(idx) - 1
        except (TypeError, ValueError):
            return {"error": "index 得是整数(会话内第几题, 从 1 起)"}
        if i < 0 or i >= len(recs):
            return {"error": "本会话一共 %d 题, 没有第 %s 题" % (len(recs), idx)}
        picked = [(i, recs[i])]
    else:
        q = str(args.get("query") or "").strip()
        if not q:
            return {"error": "给个 query(想找什么)或 index(会话内第几题); 两者都空就不知道你要哪一轮"}
        try:
            k = int(args.get("limit") or 3)
        except (TypeError, ValueError):
            k = 3
        k = max(1, min(k, 8))
        grams = [q[i:i + 2] for i in range(max(len(q) - 1, 1))]
        if len(q) == 1:
            grams = [q]
        scored = []
        for n, r in enumerate(recs):
            head = (r.get("question") or "") + " " + (r.get("concl") or "")
            # 问句/结论命中比正文命中值钱得多: 正文长, 撞两个二字词太容易, 不能靠它排序
            s = 3 * sum(head.count(g) for g in grams) + sum((r.get("answer") or "").count(g) for g in grams)
            if s:
                scored.append((s, n, r))
        if not scored:
            return {"error": "本会话答过的 %d 题里没有跟 %r 沾边的: 换几个词, 或用 index 点名第几题"
                             % (len(recs), q), "questions": [r.get("question") for r in recs]}
        scored.sort(key=lambda x: (-x[0], -x[1]))     # 同分取更近的那一题
        picked = [(n, r) for _s, n, r in scored[:k]]

    items = []
    for n, r in picked:
        ans = (r.get("answer") or "").strip()
        if len(ans) > 6000:
            ans = ans[:6000] + "…(原话更长, 已在 6000 字处截断)"
        items.append({"n": n + 1, "at": r.get("ts") or "",
                      "question": r.get("question") or "",
                      "concl": r.get("concl") or "", "answer": ans})
    return {"total": len(recs), "returned": len(items), "items": items,
            "note": "这是你**当时给出的原话**。里面的 [id=N] 是那一轮的引用号, 本轮未必还成立 —— "
                    "要引用就重新爬/搜归档拿新号, 别照抄这些号; 引用内容(原文)本身可以照用。"}


# ---------------------------------------------------------------- 注册表本体
def _crawl_entries():
    """两条爬虫 entry 从 crawl_live 的注册表长出来 —— 说明文字只写一份, 别在这里重抄。"""
    defs = {d["function"]["name"]: d["function"] for d in crawl_live.crawl_tool_defs()}
    labels = {"crawl_nga": "NGA 爬虫", "crawl_bili": "B站爬虫"}
    envs = {"crawl_nga": "TOOL_NGA", "crawl_bili": "TOOL_BILI"}
    out = []
    for c in crawl_live.CRAWLERS:
        f = defs[c["tool"]]
        out.append({"name": c["tool"], "label": labels[c["tool"]], "env": envs[c["tool"]],
                    "optional": False, "desc": f["description"], "schema": f["parameters"],
                    "needs": lambda: None, "call": _call_crawl})
    return out


def _official_entry():
    f = crawl_live.official_tool_def()["function"]
    return {"name": "crawl_official", "label": "官号探针", "env": "TOOL_OFFICIAL",
            "optional": False, "desc": f["description"], "schema": f["parameters"],
            "needs": lambda: None, "call": _call_official}


def _archive_entry():
    return {
        "name": "search_archive", "label": "历史归档检索", "env": "TOOL_ARCHIVE",
        "optional": False,
        "desc": "只读历史归档(不新爬): 按游戏/平台/时间/词检索已有样本。as_of=只看该时间点之前。",
        "schema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "game": {"type": "string"},
            "platform": {"type": "string", "enum": ["NGA", "bilibili"]},
            "as_of": {"type": "string"},
        }, "required": ["game"]},
        "needs": lambda: None, "call": search_archive.call}


def _nlp_entry():
    return {
        "name": "nlp_score", "label": "情感打分", "env": "TOOL_NLP", "optional": True,
        "desc": "用本机的打分小模型给一批文本判**情绪(负/中/正)+ 反讽**, 返回逐条结果。"
                "适合「想知道这批社区评论的整体风向、但不想逐条让大模型读」的场合。"
                "**这是可选工具**: 本机没装打分服务时它根本不在工具列表里; 万一调失败, "
                "返回里会明说「没有软信号」—— 那时请按原文自行判断, 别当成「已经判过情绪」。",
        "schema": {"type": "object", "properties": {
            "texts": {"type": "array", "items": {"type": "string"},
                      "description": "要打分的文本, 一条一个元素(建议带上下文, 单条 2000 字以内)"},
        }, "required": ["texts"]},
        "needs": _nlp_needs, "call": _call_nlp}


def _recall_entry():
    return {
        "name": "recall_answer", "label": "会话回捞", "env": "TOOL_RECALL",
        "optional": False,
        "desc": "回捞**本会话早前某一轮**答过的题: 给出那一轮的问句、结论行与答案**全文**。"
                "只读本会话(跨会话捞不到)。追问时最常用 —— 「你刚才说的那点具体怎么说的」"
                "「你之前那条引的是哪个帖子」, 先调它把原话取回来照着答, 别凭记忆复述细节。"
                "按 query(想找什么, 给几个词)或 index(会话内第几题, 从 1 起)取, 两者给一个。",
        "schema": {"type": "object", "properties": {
            "query": {"type": "string",
                      "description": "想找什么(如「鸣潮 版本」「官号公告」)。给了 index 就不用它"},
            "index": {"type": "integer",
                      "description": "会话内第几题(从 1 起, 序号见上下文里的纪要)。不确定就用 query"},
            "limit": {"type": "integer", "description": "query 命中时最多取几条(默认 3, 上限 8)"},
        }, "required": []},
        "needs": lambda: None, "call": _recall_answer}


BUILTIN = _crawl_entries() + [_official_entry(), _archive_entry(), _recall_entry(), _nlp_entry()]
BY_NAME = {t["name"]: t for t in BUILTIN}


# ---------------------------------------------------------------- 消费接口
def enabled(name):
    t = BY_NAME.get(name)
    if not t:
        return False
    if not _on(t["env"]):
        return False
    if t["optional"] and t["needs"]():
        return False          # 可选工具: 本机依赖不在 = 不暴露(结构性防线, 不靠提示词劝)
    return True


def defs():
    """模型可见的工具面(OpenAI function-calling 形状), 只含开着的。"""
    out = []
    for t in BUILTIN:
        if not enabled(t["name"]):
            continue
        out.append({"type": "function", "function": {
            "name": t["name"], "description": t["desc"], "parameters": t["schema"]}})
    return out


def mcp_tools():
    """同一份清单的 MCP 形状(tools/list 用)。inputSchema 就是上面那份 schema。"""
    out = []
    for t in BUILTIN:
        if not enabled(t["name"]):
            continue
        out.append({"name": t["name"], "description": t["desc"], "inputSchema": t["schema"]})
    return out


def call(ctx, name, args):
    """按名调一个自带工具(MCP 服务端用)。未知/没开 -> 明确报错, 不猜。"""
    t = BY_NAME.get(name)
    if t is None:
        return {"error": "没有这个工具: %s" % name}
    if not enabled(name):
        return {"error": "工具 %s 现在是关的(在设置页「工具」里打开, 或它依赖的本机服务没起)" % name}
    return t["call"](ctx, args or {})
