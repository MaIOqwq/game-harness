# -*- coding: utf-8 -*-
"""页面上那套「设置」的后端: 一张配置项清单 + 一份落盘的值 + 运行期生效。

三层来源, 优先级从高到低:
    1. settings.json  —— 用户在页面上改的(本模块写的)
    2. 环境变量       —— config.bat 设的(start.bat 会 call 它)
    3. 默认值         —— 本文件 REGISTRY 里的 default
页面上每一项都标出「当前值从哪来」, 免得用户改了半天不知道被谁盖住。

**运行期能不能立刻生效**分两种, 页面上会分别标出来:
  - 即时: 改完马上生效(见 _LIVE);
  - 重启: 那几个是进程一起来就被读进模块常量的(比如库路径), 只能重启后生效。
没把握的一律标「重启」——宁可让用户多点一次, 也不假装即时生效。

**凭据**: 明文**永不回传前端**(页面只看得到「已配置 / 未配置」)、**永不写进 settings.json**。
大模型密钥可以在页面上填 —— 那种情况写在本机的 secrets.json(和 settings.json 分开放),
方便普通人一键填完就跑; 打分服务令牌仍只读, 那个是服务端自己签发的。

本模块**故意不在顶层 import supervisor/measure**: 那两个模块在 import 时就把环境变量
读成模块常量了, 所以调用方必须先把 apply_to_env() 跑完再 import 它们 (webapp.py 就是这么排的)。

它写的是**配置文件**(settings.json / secrets.json, 临时文件 + os.replace 原子落盘), 不属于
ARCHITECTURE §5.1 那条"数据层只增不改"的契约 —— 故整份豁免 append-only 扫描 (guard: allow-write)。
"""
import json
import os
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(os.path.dirname(HERE), "settings.json")
SECRETS_PATH = os.path.join(os.path.dirname(HERE), "secrets.json")

# 启动时把「环境里本来就有哪些键」记下来, 用来算每一项的来源
_FROM_ENV = {}


def _spec(key, label, group, kind, default, help, restart=False, choices=None,
          lo=None, hi=None, sensitive=False, secret_write=False, card=None, hide=False):
    return {"key": key, "label": label, "group": group, "kind": kind, "default": default,
            "help": help, "restart": restart, "choices": choices,
            "lo": lo, "hi": hi, "sensitive": sensitive, "secret_write": secret_write,
            "card": card, "hide": hide}


# ---------------------------------------------------------------- 配置项清单
_CARD_NLP = "情感打分"
_CARD_SVC = "爬虫服务"

REGISTRY = [
    # ---- 大模型 ----
    # **没有「模型名」这一项**: 接进来一个服务, 用哪个模型是问服务端要的(见 detect_model),
    # 不该让用户去填。真要指定, config.bat 里设 LLM_MODEL 就是那个口子。
    _spec("DEEPSEEK_API_KEY", "密钥", "大模型", "secret", "", "",
          sensitive=True, secret_write=True),
    _spec("LLM_BASE_URL", "接口地址", "大模型", "str", "https://api.deepseek.com", ""),
    _spec("LLM_WINDOW", "上下文窗口", "大模型", "int", "1000000", "",
          lo=8000, hi=10000000),

    # ---- 工具 ----
    # 一件工具一张卡(card 字段): 卡片自己在页面上展开, 这件工具的开关和它自己的参数都在卡片里。
    # 开关是「模型和外部客户端能看到哪些工具」的总闸 —— 关掉就是真从工具面删掉
    # (引擎的 function-calling 清单、MCP 服务端的 tools/list 都读同一份, 见 tools/registry.py)。
    _spec("TOOL_NGA", "NGA 爬虫", "工具", "bool", "1", "", card="NGA 爬虫"),
    _spec("TOOL_BILI", "B站爬虫", "工具", "bool", "1", "", card="B站爬虫"),
    _spec("TOOL_OFFICIAL", "官号探针", "工具", "bool", "1", "", card="官号探针"),
    _spec("TOOL_ARCHIVE", "历史归档检索", "工具", "bool", "1", "", card="历史归档检索"),

    # 情感打分: 一个开关 + 它自己那六项。本机没有打分服务时**只留开关**(关着就没软信号),
    # 那六项调了也不起作用, 所以不显示 —— 摆一堆没用的旋钮只会误导。
    _spec("TOOL_NLP", "情感打分", "工具", "bool", "0", "", card=_CARD_NLP),
    _spec("NLP_TOOL_URL", "打分服务地址", "工具", "str", "http://127.0.0.1:8772",
          "本机小模型打分服务的地址。", card=_CARD_NLP),
    _spec("NLP_TOOL_TOKEN", "打分服务令牌", "工具", "secret", "",
          "打分服务自己签发的令牌。", sensitive=True, card=_CARD_NLP),
    _spec("NLP_JUDGE", "大模型复核", "工具", "bool", "1",
          "可疑样本再让大模型看一眼。", card=_CARD_NLP),
    _spec("NLP_JUDGE_CAP", "单题最多复核几条", "工具", "int", "40", "",
          lo=0, hi=500, card=_CARD_NLP),
    _spec("NLP_SUSP_TH", "反讽可疑门", "工具", "float", "0.4", "", lo=0.0, hi=1.0, card=_CARD_NLP),
    _spec("NLP_MODEL_TH", "模型判定门", "工具", "float", "0.6", "", lo=0.0, hi=1.0, card=_CARD_NLP),

    # 爬虫服务: 这几项是两条爬虫共用的(启停、地址), 所以挂一张自己的卡, 不塞进某一条爬虫里。
    _spec("HARNESS_IDLE_SEC", "闲置多久关（秒）", "工具", "int", "600", "",
          lo=0, hi=86400, card=_CARD_SVC),
    _spec("HARNESS_START_TIMEOUT", "冷启动最多等（秒）", "工具", "int", "180", "",
          lo=10, hi=1200, card=_CARD_SVC),
    _spec("HARNESS_NO_LAZY", "一直开着", "工具", "bool", "0", "", card=_CARD_SVC),
    _spec("NGA_HTTP", "NGA 爬虫地址", "工具", "str", "", "留空 = 本地 127.0.0.1:8770。",
          restart=True, card=_CARD_SVC),
    _spec("BILI_HTTP", "B站爬虫地址", "工具", "str", "", "留空 = 本地 127.0.0.1:8771。",
          restart=True, card=_CARD_SVC),
    _spec("HARNESS_LOCAL", "本地模式", "工具", "bool", "1", "", restart=True, card=_CARD_SVC),

    # ---- 存档 ----
    _spec("HARNESS_FLOW", "落逐题流水", "存档", "bool", "1",
          "每题落一份完整流水, 便于事后核对。"),
    # 记忆库位置(HARNESS_DB): 页面上**不出现**(位置是装机时定好的, 不该让用户改)。
    # 这一条留着只是为了让 config.bat / 老 settings.json 里已经设过的值照旧生效(见 apply_to_env)。
    _spec("HARNESS_DB", "", "存档", "str", "", "", restart=True, hide=True),
]

_BY_KEY = {s["key"]: s for s in REGISTRY}

# 敏感项: 只回报「配没配」, 明文永不回传前端
_SECRETS = {s["key"] for s in REGISTRY if s["sensitive"]}
# 其中这些是「用户能在页面上填」的 —— 存的落点是 secrets.json, 不混进 settings.json
_WRITE_SECRETS = {s["key"] for s in REGISTRY if s["secret_write"]}

# 即时生效: 改完把新值推进对应的模块常量(以及 os.environ —— 有些是每次调用现读的)
_LIVE = {
    "HARNESS_IDLE_SEC": "supervisor:idle",
    "HARNESS_START_TIMEOUT": "supervisor:timeout",
    "HARNESS_NO_LAZY": "supervisor:lazy",
    "NLP_JUDGE": "measure:judge",
    "NLP_JUDGE_CAP": "measure:cap",
    "NLP_SUSP_TH": "measure:susp",
    "NLP_MODEL_TH": "measure:model",
    "NLP_TOOL_URL": "measure:url",
    "HARNESS_FLOW": "env:only",     # flow.py 每次调用都读 os.environ, 不用推
    "LLM_BASE_URL": "env:only",     # 大模型那几样同理 —— 引擎每次调用现读(见 llm_cfg), 换完当场生效
    "LLM_WINDOW": "env:only",
    # 工具开关: 工具面每 ask 现组(engine._tool_defs), MCP 服务端每次 tools/list 现读 —— 也是当场生效
    "TOOL_NGA": "env:only",
    "TOOL_BILI": "env:only",
    "TOOL_OFFICIAL": "env:only",
    "TOOL_ARCHIVE": "env:only",
    "TOOL_NLP": "env:only",
}


# ---------------------------------------------------------------- 读写
def _read_file():
    try:
        with open(PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_file(d):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PATH)          # 原子落盘: 中途断电也不会留半个文件


def _read_secrets():
    try:
        with open(SECRETS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_secrets(d):
    tmp = SECRETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SECRETS_PATH)


# ---------------------------------------------------------------- 外部工具服务
# 用户自己加的 MCP 工具服务(命令或地址)。和 settings.json 分开存: 它是张清单(可增可删),
# 不是"一个键一个值"那种配置项, 塞进 REGISTRY 那套标量读写里只会别扭。
MCP_SERVERS_PATH = os.path.join(os.path.dirname(HERE), "mcp_servers.json")


def _strmap(v):
    """{键: 字符串} 那种小字典, 不是字典就当空 —— 用户手填的 JSON 什么都可能是。"""
    if not isinstance(v, dict):
        return {}
    return {str(k): str(x) for k, x in v.items()}


def mcp_servers():
    """外部工具服务清单。**坏行单独丢掉, 不整体作废** —— 一条填错不该把别的也带走。"""
    try:
        with open(MCP_SERVERS_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return []
    if not isinstance(d, list):
        return []
    out = []
    for it in d:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        transport = "http" if str(it.get("transport") or "").lower() == "http" else "stdio"
        row = {"name": name, "transport": transport,
               "enabled": str(it.get("enabled", "1")) != "0",
               "env": _strmap(it.get("env")), "headers": _strmap(it.get("headers"))}
        if transport == "http":
            row["url"] = str(it.get("url") or "").strip()
        else:
            cmd = it.get("command")
            if isinstance(cmd, str):
                cmd = cmd.split()
            row["command"] = [str(c) for c in (cmd or []) if str(c).strip()]
            row["cwd"] = str(it.get("cwd") or "").strip()
        out.append(row)
    return out


def save_mcp_servers(items):
    tmp = MCP_SERVERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(items or []), f, ensure_ascii=False, indent=2)
    os.replace(tmp, MCP_SERVERS_PATH)


def capture_env():
    """记下"环境里本来就有哪些键"(进程一起来调一次), 用于页面上标来源。"""
    _FROM_ENV.clear()
    for s in REGISTRY:
        if os.environ.get(s["key"]) not in (None, ""):
            _FROM_ENV[s["key"]] = True


def apply_to_env():
    """把落盘的值灌进环境变量。**必须在 import supervisor/measure 之前调**。"""
    capture_env()
    for k, v in _read_file().items():
        # 空值不灌: 下游几个读法都是 os.environ.get(k, 默认), 空串会被当成真值用、顶掉默认
        if k in _BY_KEY and k not in _SECRETS and str(v) != "":
            os.environ[k] = str(v)
    for k, v in _read_secrets().items():   # 页面上填的密钥盖过 config.bat 里的同名环境变量
        if k in _WRITE_SECRETS and str(v) != "":
            os.environ[k] = str(v)


def effective(key):
    """按 页面设置 > 环境变量 > 默认 的顺序算当前生效值。"""
    if key in _SECRETS:
        return ""
    saved = _read_file()
    if key in saved and saved[key] not in (None, ""):
        return saved[key]
    if os.environ.get(key) not in (None, ""):
        return os.environ[key]
    return _BY_KEY[key]["default"]


def _source(key):
    """这一项的值是从哪来的。**只看 _FROM_ENV, 不看当前的 os.environ** ——
    apply_to_env() 会把页面存的值也灌进 os.environ, 拿它当依据的话, 页面设过的项会被
    错标成「环境变量(config.bat)」。"""
    if key in _SECRETS:
        if key in _WRITE_SECRETS and _read_secrets().get(key):
            return "页面设置"
        return "环境变量" if _FROM_ENV.get(key) else "未配置"
    if key in _read_file() and _read_file().get(key) not in (None, ""):
        return "页面设置"
    if _FROM_ENV.get(key):
        return "环境变量(config.bat)"
    return "默认值"


def _base_url():
    """用户填的接口地址(可能是 /v1 前缀, 也可能是完整的 chat/completions 地址)。"""
    return (os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/") or "https://api.deepseek.com"


def _api_root(base):
    """从接口地址里剥出服务根地址 —— /models 要打在根上, 不是打在 /chat/completions 上。"""
    b = (base or "").strip().rstrip("/")
    if b.endswith("/chat/completions"):
        b = b[:-len("/chat/completions")].rstrip("/")
    return b


def _llm_key():
    return os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")


# 不能对话的模型(向量/语音/画图/审核那几类)。挑模型时先按名字排掉, 免得把 embedding 当聊天模型发过去。
_NON_CHAT = ("embedding", "embed", "rerank", "tts", "whisper", "audio", "speech", "voice",
             "moderation", "image", "dall-e", "stable-diffusion", "sd-", "video", "ocr", "asr")

_MODELS_CACHE = {}          # 服务根地址 -> (模型名, 探测失败时的原因)


def _pick_chat_model(ids):
    """清单里挑一个能聊天的: 名字带 chat 的先上, 否则取第一个非专用模型。挑不出就 None。"""
    good = [i for i in ids if not any(bad in str(i).lower() for bad in _NON_CHAT)]
    for i in good:
        if "chat" in str(i).lower():
            return i
    return good[0] if good else None


def _probe_err(e):
    """探测失败时说人话, 并且指出下一步 —— 页面会原样把这句显示给用户。"""
    code = getattr(e, "code", None)
    if code in (401, 403):
        return "密钥不对或还没填（HTTP %s）" % code
    if code == 404:
        return "这个地址没有模型清单接口（HTTP 404）—— 要指定模型请设 LLM_MODEL"
    if code:
        return "服务端回了 HTTP %s" % code
    return "%s（连不上这台服务？）" % (type(e).__name__)


def detect_model(base=None, key=None, force=False, timeout=6):
    """问服务端「你有哪些模型」, 挑一个能聊天的当默认 —— 用户不用自己填模型名。

    返回 (模型名, 原因)。**探测失败不抛也不留空**: 答题还得继续(退回 deepseek-chat),
    原因由页面照实显示出来。同一个地址探一次就记住(force=True 才再探一次)。
    """
    root = _api_root(base if base is not None else _base_url())
    if not force and root in _MODELS_CACHE:
        return _MODELS_CACHE[root]
    key = _llm_key() if key is None else key
    model, err = None, None
    try:
        req = urllib.request.Request(root + "/models",
                                     headers={"Authorization": "Bearer " + (key or "")})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        rows = d.get("data") if isinstance(d, dict) else None
        if not isinstance(rows, list):
            rows = d.get("models") if isinstance(d, dict) else None
        ids = []
        for it in (rows or []):
            mid = str(it.get("id") or it.get("name") or "").strip() if isinstance(it, dict) \
                else str(it or "").strip()
            if mid:
                ids.append(mid)
        model = _pick_chat_model(ids)
        if not model:
            err = "服务端没给出模型清单" if not ids else "清单里没有能对话的模型"
    except Exception as e:                       # 网络/解析/HTTP 一律当"没探到", 不往上抛
        err = _probe_err(e)
    if not model:
        model = "deepseek-chat"
    _MODELS_CACHE[root] = (model, err)
    return model, err


def peek_model():
    """**不联网**地看一眼现在用哪个模型(设置页打开时调, 不许卡在网络上)。"""
    forced = (os.environ.get("LLM_MODEL") or "").strip()
    if forced:
        return {"model": forced, "auto": False, "error": None}
    hit = _MODELS_CACHE.get(_api_root(_base_url()))
    if hit:
        return {"model": hit[0], "auto": True, "error": hit[1]}
    return {"model": "", "auto": True, "error": None}


def llm_cfg():
    """大模型怎么接: (完整的 chat/completions 地址, 模型名, 密钥)。

    **每次调用现读**, 不是启动时定死的常量 —— 页面上换完服务商要当场生效。
    地址按 OpenAI 兼容协议拼: 填到 /v1 这种前缀就行, 缺的 /chat/completions 这里补上;
    已经把完整地址填进来的也认。模型名不填时**问服务端要**(detect_model, 一个地址只问一次)。
    """
    base = _base_url()
    if not base.endswith("/chat/completions"):
        base += "/chat/completions"
    model = (os.environ.get("LLM_MODEL") or "").strip()   # 设了就是明着指定, 不被自动探测盖掉
    if not model:
        model, _err = detect_model()
    return base, model, _llm_key()


def llm_window():
    """所配模型的上下文窗口(token)。护栏按它折算, 换小窗口的模型时会等比例收紧。"""
    try:
        return max(8000, int(os.environ.get("LLM_WINDOW") or 1000000))
    except Exception:
        return 1000000


def _tool_note(key):
    """工具那一栏的「能不能用」: 可选工具依赖的本机东西不在时, 照实说一句(不编)。"""
    try:
        from .tools import registry
        for t in registry.BUILTIN:
            if t["env"] == key:
                return t["needs"]()
    except Exception:
        pass
    return None


def nlp_available():
    """情感打分这一套现在到底能不能起作用 —— 两个条件都满足才算:

      1. 那把开关(TOOL_NLP)是开着的;
      2. 本机检测得到打分服务(判据与服务端一致: 只看令牌在不在, 不真去连)。

    缺就把「情感打分」卡片里那六项(地址/令牌/两道门槛/复核开关/复核上限)藏掉 ——
    服务不在时 measure.py 直接返回「没有软信号」, 连大模型复核那一支都走不到,
    摆一堆调了也不起作用的旋钮只会误导。**开关本身留着**: 想用得先把它打开。
    """
    if os.environ.get("TOOL_NLP", "0") in ("", "0"):
        return False
    try:
        from .sources import live
        return live.measure_mode() != "keyword"
    except Exception:
        return False


def payload():
    """给前端的清单: 分组 + 每项的当前值/来源/是否敏感/是否要重启。

    「工具」那一组的 layout 是 cards: 每项带一个 card 名字, 前端按它把**一件工具的参数
    收进同一张卡片**(点开那张卡才看得到), 见 web/index.html 的 cardGrid()。
    """
    hide_nlp = not nlp_available()
    groups = []
    for s in REGISTRY:
        if s["hide"]:
            continue
        if hide_nlp and s["card"] == _CARD_NLP and s["key"] != "TOOL_NLP":
            continue
        if not groups or groups[-1]["name"] != s["group"]:
            groups.append({"name": s["group"], "items": [], "layout": "list"})
        if s["sensitive"]:
            # 明文不外传: 只告诉前端「配没配」+「这个能不能在页面上填」
            item = dict(s, value="", configured=bool(_FROM_ENV.get(s["key"]) or
                                                     _read_secrets().get(s["key"])),
                        writable=bool(s["secret_write"]))
        else:
            item = dict(s, value=effective(s["key"]), configured=None, writable=False)
        item["source"] = _source(s["key"])
        # 可选工具依赖的本机东西不在时, 把一句实话挂在它的卡片上(不是报错, 是条件不满足)
        item["note"] = _tool_note(s["key"]) if s["card"] else None
        item.pop("default", None)
        item.pop("secret_write", None)
        item.pop("hide", None)
        groups[-1]["items"].append(item)
    for g in groups:
        if any(it.get("card") for it in g["items"]):
            g["layout"] = "cards"
    return {"groups": groups, "path": PATH, "model": peek_model()}


# ---------------------------------------------------------------- 存 + 生效
def _coerce(spec, raw):
    """把前端来的值转成该有的类型。返回 (值, 错误串)。"""
    kind = spec["kind"]
    if kind == "bool":
        return ("1" if str(raw).strip() not in ("0", "", "false", "False") else "0"), None
    if kind == "int":
        try:
            v = int(str(raw).strip())
        except Exception:
            return None, "要填整数"
        if spec["lo"] is not None and v < spec["lo"]:
            return None, "不能小于 %s" % spec["lo"]
        if spec["hi"] is not None and v > spec["hi"]:
            return None, "不能大于 %s" % spec["hi"]
        return str(v), None
    if kind == "float":
        try:
            v = float(str(raw).strip())
        except Exception:
            return None, "要填数字"
        if spec["lo"] is not None and v < spec["lo"]:
            return None, "不能小于 %s" % spec["lo"]
        if spec["hi"] is not None and v > spec["hi"]:
            return None, "不能大于 %s" % spec["hi"]
        return str(v), None
    return str(raw or "").strip(), None


def _push_live(key, val):
    """即时生效那几项, 把新值推进对应模块。"""
    what = _LIVE.get(key)
    if not what:
        return False
    if what == "env:only":
        return True
    from . import measure, supervisor
    if what == "supervisor:idle":
        supervisor.configure(idle_sec=int(val))
    elif what == "supervisor:timeout":
        supervisor.configure(start_timeout=int(val))
    elif what == "supervisor:lazy":
        supervisor.configure(lazy=(val != "1"))
    elif what == "measure:judge":
        measure._JUDGE_ON = (val != "0")
    elif what == "measure:cap":
        measure._JUDGE_CAP = int(val)
    elif what == "measure:susp":
        measure._SUSP_TH = float(val)
    elif what == "measure:model":
        measure._MODEL_TH = float(val)
    elif what == "measure:url":
        measure._URL = val
    return True


def save(values):
    """按前端提交的键值存盘 + 尽量即时生效。

    返回 {"ok", "applied": [...], "restart": [...], "errors": {...}, "groups": ...}
    applied  = 已经当场生效的
    restart  = 存进去了、但要重启进程才生效的
    """
    values = values or {}
    errors, applied, restart, saved = {}, [], [], _read_file()
    sec = _read_secrets()

    for key, raw in values.items():
        spec = _BY_KEY.get(key)
        if spec is None:
            errors[key] = "不认识的配置项"
            continue
        if spec["sensitive"] and not spec["secret_write"]:
            errors[key] = "凭据只能在 config.bat 里改, 页面上不写盘"
            continue
        if spec["secret_write"]:
            # 填了新的 = 换; 提交空串 = 把页面上存的那份撤掉(回到 config.bat / 未配置)。
            # 前端「没动过的空框」根本不会提交, 见 index.html 的 setSave —— 否则打开一次面板
            # 点个保存, 就能把 config.bat 里配好的密钥顺手抹掉。
            val = str(raw or "").strip()
            if val:
                sec[key] = val
                os.environ[key] = val
            else:
                sec.pop(key, None)
                os.environ.pop(key, None)
            applied.append(key)
            continue
        val, err = _coerce(spec, raw)
        if err:
            errors[key] = err
            continue
        if val == "":
            # 留空 = 撤掉这层的覆盖, 回到「环境变量 / 默认值」。
            # **不能把空串当值存下来**: 下游读法是 os.environ.get(k, 默认), 空串会被当成真值用
            # (NGA_HTTP 那种一空、爬虫地址就变成空串, 整条链直接连不上)。
            saved.pop(key, None)
            os.environ.pop(key, None)
            if _push_live(key, str(effective(key))):
                applied.append(key)
            else:
                restart.append(key)
            continue
        saved[key] = val
        os.environ[key] = val              # 每次调用现读的那些立即看到新值
        if _push_live(key, val):
            applied.append(key)
        else:
            restart.append(key)

    if errors:
        return {"ok": False, "errors": errors, "applied": [], "restart": [],
                "groups": payload()["groups"], "model": None}
    _write_file(saved)
    _write_secrets(sec)
    # 动了密钥/地址就当场再问一次模型清单 —— 换服务商后页面上要立刻看得到「现在用哪个模型」
    model = None
    if any(k in ("DEEPSEEK_API_KEY", "LLM_BASE_URL") for k in applied):
        m, err = detect_model(force=True)
        model = {"model": m, "auto": True, "error": err}
    return {"ok": True, "errors": {}, "applied": applied, "restart": restart,
            "groups": payload()["groups"], "model": model}
