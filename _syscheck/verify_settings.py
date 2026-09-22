#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置页自检: 大模型那栏长什么样、工具卡片怎么分、模型名是**问出来的**不是用户填的。

为什么这支值得存在: 设置页是用户唯一能自己动的地方, 而它错起来不报错 —— 少一项、多一段说明、
把「记忆库位置」这种装机时定好的东西摆在页面上让用户改, 都要等人真去点了才发现。
所以这几条钉死:

  ① 该删的删了: 大模型那三行没有说明文字; 「模型名」这一项整条没了; 「记忆库位置」不在页面上;
  ② 工具 = 卡片: 那一组的 layout 是 cards, 每项都带 card 名, 一件工具的参数都在同一张卡里;
     本机没有打分服务时那张卡只留开关(六个旋钮调了也不起作用, 摆出来只会误导);
  ③ 模型名是问服务端要的: 打 /models 挑一个能聊天的(排掉向量/语音/画图那类), 一个地址只问一次;
     设了 LLM_MODEL 就听它的(那是口子); 问不到就如实说原因, 并退回 deepseek-chat 保证答题不断;
  ④ 存盘: 换了密钥/地址当场再问一次, 把「现在用哪个模型」带回给页面。

**只碰临时目录**: 这里会调 save(), 所以 PATH/SECRETS_PATH 先指到 temp 下, 绝不写产品的
settings.json / secrets.json。除了自己起的那个假服务, 不触网。

用法: python _syscheck/verify_settings.py   退出码 0=全过, 1=有失败。
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from harness import settings                                              # noqa: E402

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


def _card_of(pl, name):
    """payload 里某张工具卡上的项(找不到返回 None)。"""
    for g in pl["groups"]:
        if g.get("layout") == "cards":
            got = [it for it in g["items"] if it.get("card") == name]
            if got:
                return got
    return None


# ---------- 假的大模型服务: 只回 /models, 记下被打了几次 ----------
HITS = {"models": 0}


class _H(BaseHTTPRequestHandler):
    ids = ["text-embedding-3", "whisper-1", "deepseek-chat", "qwen-plus"]
    code = 200

    def do_GET(self):
        if not self.path.rstrip("/").endswith("/models"):     # 填到 /v1 前缀的也得认
            self.send_error(404)
            return
        HITS["models"] += 1
        if _H.code != 200:
            self.send_error(_H.code)
            return
        body = json.dumps({"data": [{"id": i} for i in _H.ids]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
FAKE = "http://127.0.0.1:%d" % PORT

TMP = tempfile.mkdtemp(prefix="verify_settings_")
settings.PATH = os.path.join(TMP, "settings.json")
settings.SECRETS_PATH = os.path.join(TMP, "secrets.json")
settings.MCP_SERVERS_PATH = os.path.join(TMP, "mcp_servers.json")

for k in ("LLM_MODEL", "LLM_API_KEY", "DEEPSEEK_API_KEY", "NLP_TOOL_TOKEN", "TOOL_NLP"):
    os.environ.pop(k, None)

try:
    # ---------- ① 该删的删了 ----------
    print("=== 1. 页面上不该有的东西 ===")
    keys = {s["key"] for s in settings.REGISTRY}
    chk("①-1 「模型名」这一项整条没了(它是问服务端要的, 不该让用户填)",
        "LLM_MODEL" not in keys, sorted(k for k in keys if k.startswith("LLM")))
    chk("①-2 大模型那三行都没有说明文字",
        all(not settings._BY_KEY[k]["help"] for k in ("DEEPSEEK_API_KEY", "LLM_BASE_URL", "LLM_WINDOW")),
        [settings._BY_KEY[k]["help"] for k in ("DEEPSEEK_API_KEY", "LLM_BASE_URL", "LLM_WINDOW")])
    chk("①-3 工具那几行的说明也清了(开关名本身就说清了它是什么)",
        all(not s["help"] for s in settings.REGISTRY
            if s["key"].startswith("TOOL_")), "")
    chk("①-4 「记忆库位置」在页面上不出现(位置是装机时定好的, 不该让用户改)",
        settings._BY_KEY["HARNESS_DB"]["hide"] is True)
    chk("①-5 「落逐题流水」留着",
        settings._BY_KEY["HARNESS_FLOW"]["group"] == "存档"
        and not settings._BY_KEY["HARNESS_FLOW"]["hide"], "")

    # ---------- ② 工具 = 卡片 ----------
    print("=== 2. 工具那一栏 = 一件工具一张卡 ===")
    pl = settings.payload()
    names = [g["name"] for g in pl["groups"]]
    chk("②-1 分组只剩大模型 / 工具 / 存档(外部工具并进工具, 情感与启停不再各自一类)",
        names == ["大模型", "工具", "存档"], names)
    tool = [g for g in pl["groups"] if g["name"] == "工具"][0]
    chk("②-2 工具那一组的 layout 是 cards", tool.get("layout") == "cards", tool.get("layout"))
    chk("②-3 每一项都带了 card 名(前端按它收进对应的卡)",
        all(it.get("card") for it in tool["items"]), "")
    cards, seen = [], set()
    for it in tool["items"]:
        if it["card"] not in seen:
            seen.add(it["card"]); cards.append(it["card"])
    chk("②-4 卡片齐: 四条爬虫/归档 + 情感打分 + 爬虫服务",
        cards == ["NGA 爬虫", "B站爬虫", "官号探针", "历史归档检索", "情感打分", "爬虫服务"], cards)
    chk("②-5 卡里第一项就是这件工具自己的开关(下面那几项都跟着它走)",
        (_card_of(pl, "情感打分") or [{}])[0].get("key") == "TOOL_NLP",
        [it["key"] for it in (_card_of(pl, "情感打分") or [])])
    chk("②-6 「爬虫服务」卡 = 启停三项 + 两个地址 + 本地模式(共用的挂一张卡, 塞进某条爬虫里就错了)",
        len(_card_of(pl, "爬虫服务") or []) == 6, len(_card_of(pl, "爬虫服务") or []))
    chk("②-7 payload 里带上了「当前模型」这一行(只读, 见③)",
        isinstance(pl.get("model"), dict) and "auto" in pl["model"], pl.get("model"))
    labels = [it["label"] for g in pl["groups"] for it in g["items"]]
    chk("②-8 页面上没有「记忆库位置」也没有「模型名」",
        "记忆库位置" not in labels and "模型名" not in labels, labels)

    print("=== 3. 本机没有打分服务时, 那张卡只留开关 ===")
    os.environ["TOOL_NLP"] = "0"
    got = _card_of(settings.payload(), "情感打分")
    chk("③-1 关着就只显示开关本身(六项调了也不起作用, 不摆出来)",
        len(got) == 1 and got[0]["key"] == "TOOL_NLP", [it["key"] for it in got])
    os.environ["TOOL_NLP"] = "1"
    got = _card_of(settings.payload(), "情感打分")
    chk("③-2 没填令牌(服务不在) -> 还是只有开关", len(got) == 1, [it["key"] for it in got])
    chk("③-3 而且照实写着本机缺什么(不编、也不假装能用)",
        bool(got[0].get("note")) and "打分服务" in got[0]["note"], got[0].get("note"))
    os.environ["NLP_TOOL_TOKEN"] = "test-token"
    got = _card_of(settings.payload(), "情感打分")
    chk("③-4 开着 + 有令牌 -> 七项全出来", len(got) == 7, [it["key"] for it in got])
    os.environ.pop("NLP_TOOL_TOKEN", None)
    os.environ["TOOL_NLP"] = "0"

    # ---------- ④ 模型名是问出来的 ----------
    print("=== 4. 模型名: 问服务端要, 不问用户 ===")
    model, err = settings.detect_model(FAKE, key="k", force=True)
    chk("④-1 从 /models 里挑了一个能聊天的(带 chat 的优先), 不是第一个",
        model == "deepseek-chat" and err is None, (model, err))
    chk("④-2 向量/语音那几类不会被当成聊天模型",
        settings._pick_chat_model(["text-embedding-3", "whisper-1", "dall-e-3"]) is None, "")
    _H.ids = ["text-embedding-3", "whisper-1"]
    m2, e2 = settings.detect_model(FAKE, key="k", force=True)
    chk("④-3 一个能聊的都没有 -> 原因写清楚(不假装探到了)",
        m2 == "deepseek-chat" and e2 and "没有能对话" in e2, (m2, e2))
    before = HITS["models"]
    settings.detect_model(FAKE, key="k")            # 同一地址第二次
    chk("④-4 同一个地址只问一次(答题时不许每次都去打网络)",
        HITS["models"] == before, (before, HITS["models"]))
    _H.code = 404
    m3, e3 = settings.detect_model(FAKE, key="k", force=True)
    chk("④-5 服务端没有这个接口 -> 说清 404 并指出「要指定模型就设 LLM_MODEL」",
        m3 == "deepseek-chat" and e3 and "404" in e3 and "LLM_MODEL" in e3, (m3, e3))
    _H.code = 200
    _H.ids = ["deepseek-chat", "qwen-plus"]

    print("=== 5. 接进引擎的那条路 ===")
    os.environ["LLM_BASE_URL"] = FAKE + "/v1"
    settings.detect_model(FAKE + "/v1", key="k", force=True)
    url, mdl, _key = settings.llm_cfg()
    chk("⑤-1 地址按 OpenAI 协议拼(填到 /v1 就行, 缺的路径自己补)",
        url == FAKE + "/v1/chat/completions", url)
    chk("⑤-2 模型名是探测来的", mdl == "deepseek-chat", mdl)
    os.environ["LLM_MODEL"] = "我指定的模型"
    chk("⑤-3 设了 LLM_MODEL 就听它的(那是明着指定, 不被自动探测盖掉)",
        settings.llm_cfg()[1] == "我指定的模型", settings.llm_cfg()[1])
    chk("⑤-4 这时页面上标「手动指定」",
        settings.peek_model() == {"model": "我指定的模型", "auto": False, "error": None},
        settings.peek_model())
    os.environ.pop("LLM_MODEL")

    # ---------- ⑥ 存盘 ----------
    print("=== 6. 保存时顺手把新模型探测出来 ===")
    r = settings.save({"LLM_BASE_URL": FAKE + "/v1"})
    chk("⑥-1 保存成功", r.get("ok") is True, r)
    chk("⑥-2 换了地址当场再问一次, 结果带回给页面",
        (r.get("model") or {}).get("model") == "deepseek-chat", r.get("model"))
    chk("⑥-3 存的是临时目录那份 settings.json, 产品那份没被动过",
        os.path.exists(settings.PATH)
        and not os.path.exists(os.path.join(ROOT, "settings.json")), settings.PATH)
    r2 = settings.save({"HARNESS_FLOW": "0"})
    chk("⑥-4 没动大模型就不去问网络(保存不该平白多等几秒)",
        r2.get("ok") is True and r2.get("model") is None, r2.get("model"))
finally:
    srv.shutdown()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)

print("")
print("汇总: %d 通过 / %d 失败" % (ok, total - ok))
if fails:
    for f in fails:
        print("  " + f)
    sys.exit(1)
