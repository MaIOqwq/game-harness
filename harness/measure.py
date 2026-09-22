# -*- coding: utf-8 -*-
"""本地度量: senti+sarcasm 走 nlp-tool(LoRA structbert-lora-gold002), topics 走 keyword。
可疑反讽(模型 sarcasm_prob >= NLP_SUSP_TH=0.4) 攒批带标题交大模型 judge 覆写(sarcasm 最终 0/1);
nlp-tool / 大模型密钥 / token 任一不可用 -> 自动降级原 keyword 启发式(存档不炸, 不因度量丢一次爬)。
结构(senti_arg/sarcasm/topic_tags)不变。
env:
  NLP_TOOL_URL         默认 http://127.0.0.1:8772 (与打分服务同机时直连; 异地部署自行指向该服务)
  NLP_TOOL_TOKEN       优先; 否则读 NLP_TOOL_TOKEN_FILE(默认 /path/to/nlp-tool/.tool_token)
  NLP_SUSP_TH          可疑导流线 0.4(>= 判进 judge)
  NLP_MODEL_TH         judge 未答时的模型标签线 0.6
  NLP_JUDGE_CAP        每批导流给 judge 上限 40(防超时)
  NLP_JUDGE=0          关 judge(可疑样本一律用模型标签)
"""
import json
import os
import re
import sys
import urllib.request

# ---------- keyword 启发式(降级 + topics 用) ----------
MARK = ["岁月史书", "孝", "经典", "赢", "绷", "谢", "回旋镖", "典", "尽孝", "贴金", "结晶孝子", "韭菜", "捂嘴", "洗地"]
NEG = ["烂", "答辩", "恶心", "退坑", "拉胯", "坑", "垃圾", "失望", "离谱", "踩死"]
POS = ["真香", "顶", "强", "爽", "好玩", "推荐", "优秀", "好评"]
STRONG = ["膨胀", "数值", "强度", "保值", "破格", "抬", "退役"]


def topics(text):
    tags = []
    if any(w in text for w in STRONG):
        tags.append("强度")
    if any(m in text for m in MARK):
        tags.append("阵营/反讽")
    return ",".join(tags)


def _senti_kw(text):
    neg = sum(text.count(w) for w in NEG)
    pos = sum(text.count(w) for w in POS)
    if neg > pos:
        return 0
    if pos > neg:
        return 2
    return 1


def _sarcasm_kw(text):
    return 1 if any(m in text for m in MARK) else 0


# ---------- config ----------
def _env(name, default):
    v = os.environ.get(name)
    return default if v in (None, "") else v


_URL = _env("NLP_TOOL_URL", "http://127.0.0.1:8772")
_JUDGE_ON = os.environ.get("NLP_JUDGE", "1") != "0"
_SUSP_TH = float(_env("NLP_SUSP_TH", "0.4"))
_MODEL_TH = float(_env("NLP_MODEL_TH", "0.6"))
_JUDGE_CAP = int(_env("NLP_JUDGE_CAP", "40"))
_TEXT_CAP = 150       # nlp-tool 单 POST 条数(服务端 MAX 200, 留余量)
_JUDGE_BATCH = 8      # 一次判定的条数

_warned = {}


def _warn(k, msg):
    if not _warned.get(k):
        _warned[k] = True
        print("[measure] %s" % msg, file=sys.stderr)


def _tool_token():
    tok = _env("NLP_TOOL_TOKEN", None)
    if tok:
        return tok
    path = _env("NLP_TOOL_TOKEN_FILE", "/path/to/nlp-tool/.tool_token")
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    _warn("tool", "nlp-tool token 不可用(设 NLP_TOOL_TOKEN 或文件 %s): 度量降级 keyword" % path)
    return None


def _nlp_measure(texts):
    """texts -> 对齐 [{senti_arg, sarcasm_prob}] 或 None(不可用/失败, 走 keyword 降级)。"""
    tok = _tool_token()
    if not tok:
        return None
    out = []
    for i in range(0, len(texts), _TEXT_CAP):
        chunk = texts[i:i + _TEXT_CAP]
        body = json.dumps({"texts": chunk}).encode("utf-8")
        req = urllib.request.Request(_URL + "/measure", data=body,
                                     headers={"Content-Type": "application/json", "X-Token": tok})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                items = json.loads(r.read().decode("utf-8")).get("items") or []
        except Exception as e:
            _warn("tool", "nlp-tool 请求失败 %r: 度量降级 keyword" % (e,))
            return None
        out.extend(items)
    if len(out) != len(texts):
        _warn("tool", "nlp-tool 返回条数不符(%d/%d): 度量降级 keyword" % (len(out), len(texts)))
        return None
    return out


def _parse_json(s):
    if not s:
        return None
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    a, b = s.find("{"), s.rfind("}")
    if 0 <= a < b:
        try:
            obj = json.loads(s[a:b + 1])
            if isinstance(obj, dict):
                return {str(k): v for k, v in obj.items()}
        except Exception:
            pass
    m = re.findall(r'"(\d+)"\s*:\s*([01])', s)
    if m:
        return {k: int(v) for k, v in m}
    return None


def _judge_sarc(items):
    """items: [(全局下标, title, text)] -> {下标: 0/1}。key 缺失/失败/解析不出 -> {}。"""
    if not _JUDGE_ON:
        return {}
    from .settings import llm_cfg          # 每次现读: 页面上换完服务商当场生效
    dss_url, dss_model, key = llm_cfg()
    if not key:
        _warn("judge", "大模型密钥未设: 可疑样本用模型标签(不导流 judge)")
        return {}
    results = {}
    for s in range(0, len(items), _JUDGE_BATCH):
        chunk = items[s:s + _JUDGE_BATCH]
        blk = "\n".join("[%d] 标题: %s | 评论: %s" % (
            j, (t or "").replace("\n", " ")[:100], (x or "").replace("\n", " ")[:300]) for j, t, x in chunk)
        msgs = [
            {"role": "system",
             "content": "你是中文游戏社区反讽检测器。反讽 = 正话反说 / 阴阳怪气 / 恶意反夸: 表面夸赞、实际在贬低或嘲笑。判断只看文本是否真是反讽, 别脑补。\n"
                        "示例(直接对照学尺度):\n"
                        "- 评论: 新卡池太良心了, 全保底, 策划真懂玩家 -> 反讽=1 (表面夸实际骂)\n"
                        "- 评论: 这皮肤质量不错, 我买了 -> 反讽=0 (正常夸)\n"
                        "- 评论: 孝子又来岁月史书了? 经典洗地 -> 反讽=1 (阴阳怪气)\n"
                        "- 评论: 服务器又炸了, 排队一小时, 无语 -> 反讽=0 (直接吐槽)\n"
                        "直接批评、正常吐槽、玩梗但不带贬损都不算反讽。"},
            {"role": "user",
             "content": "逐条判定下面每条评论是否反讽。只输出 JSON: {\"给定的序号\": 0或1}(0=不反讽, 1=反讽), 不要解释、不要其他文字。\n" + blk},
        ]
        body = {"model": dss_model, "temperature": 0, "messages": msgs}
        req = urllib.request.Request(dss_url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        parsed = None
        for _ in range(2):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    content = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
                if os.environ.get("NLP_JUDGE_DEBUG") == "1":
                    print("[measure][judge-debug] %s" % content[:400], file=sys.stderr)
                parsed = _parse_json(content)
                if parsed is not None:
                    break
            except Exception:
                parsed = None
        if parsed:
            for j, _t, _x in chunk:
                if str(j) in parsed and parsed[str(j)] in (0, 1):
                    results[j] = parsed[str(j)]
    return results


def score_records(records):
    """records: [{title, text}] -> 对齐 [{senti_arg, sarcasm, topic_tags}]。
    nlp-tool 不可用整体降级 keyword; 可疑反讽(prob>=SUSP_TH) 带标题交 judge 覆写。"""
    texts = [(r.get("text") or "")[:2000] for r in records]
    if not texts:
        return []
    kw = [{"senti_arg": _senti_kw(t), "sarcasm": _sarcasm_kw(t), "topic_tags": topics(t)} for t in texts]
    mod = _nlp_measure(texts)
    if mod is None:
        return kw
    finals = [None] * len(texts)
    need_judge = []
    for i, (rec, m) in enumerate(zip(records, mod)):
        try:
            p = float(m.get("sarcasm_prob", 0.0))
            senti_arg = int(m.get("senti_arg", kw[i]["senti_arg"]))
        except (TypeError, ValueError):
            finals[i] = kw[i]
            continue
        finals[i] = {"p": p, "senti_arg": senti_arg,
                     "sarcasm": 1 if p >= _MODEL_TH else 0,
                     "topic_tags": topics(texts[i])}
        if p >= _SUSP_TH:
            need_judge.append((i, rec.get("title") or "", texts[i]))
    if need_judge:
        judge_in = need_judge[:_JUDGE_CAP]
        if len(need_judge) > _JUDGE_CAP:
            _warn("cap", "可疑 %d 条超导流上限 %d, 多余用模型标签" % (len(need_judge), _JUDGE_CAP))
        for i, _x in _judge_sarc(judge_in).items():
            if finals[i] is not None:
                finals[i]["sarcasm"] = _x
    return [{"senti_arg": f["senti_arg"], "sarcasm": f["sarcasm"], "topic_tags": f["topic_tags"]} for f in finals]
