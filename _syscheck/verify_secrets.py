# -*- coding: utf-8 -*-
"""分发包体检: 这棵树里不许有凭证(cookie / 爬虫服务令牌 / 浏览器登录数据 / 密钥字面量)。

为什么要有这一条: 这个包是要发出去的(放简历、传 GitHub)。爬虫的登录态就落在几个具体文件里 ——
bili 的整个浏览器 profile(`crawlers/bili/browser_data/`)、NGA 的 `config.json`、两个服务自己签的
`.tool_token`、以及可能会顺手写死在源码里的密钥 —— 它们**在开发机上是必需的, 在分发包里就是泄密**。
靠"打包前记得删"是靠不住的(已经漏过一次), 所以固化成一条会红的自检。

只扫数据文件里"键 + 长值"那种形状, 不裸扫关键字: 源码里本来就该出现 SESSDATA 这个词
(bili 的登录模块要读它), 裸扫会把正经代码判成违规, 那样的自检没人会留着。

**它体检的是"要发出去的那棵树", 不是部署机。** 部署机上爬虫不在树内(在 <DEPLOY_ROOT>/nga-demo、
<DEPLOY_ROOT>/bili-demo)、`harness.env` 里就住着密钥 —— 那两条在那儿是**正常的**, 照红只会让人习惯性
忽略红灯。所以这两处按"不适用"跳过并印出理由, 不算通过也不算失败。真正兜住"别把机器自己的
配置打进包"的是打包那一步(make_dist.py 的 DROP_FILES 已含 harness.env/.env/settings.json/
secrets.json/mcp_servers.json)。
"""
import os
import re
import sys

try:                                    # Windows 控制台默认 GBK, 中文会变乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ME = os.path.abspath(__file__)

# 这些路径**不许存在**(相对仓库根)
# 注意 `crawlers/nga/config.json` 不在其列: NGA 服务**没有这个文件会直接退出**(nga_search_server
# 找不到 config 就 SystemExit), 所以它必须随包走 —— 但它必须是**掏空 cookie 的模板**,
# 由下面第 2 段专门盯住这一点。
CRED_PATHS = (
    "crawlers/bili/browser_data",
    "crawlers/nga/browser_data",
    "crawlers/bili/.tool_token",
    "crawlers/nga/.tool_token",
    "harness/data/.bili_token",
    "harness/data/.nga_token",
    "harness/data/.nlp_token",
)

# 数据文件里"cookie 类键 + 一长串值"的形状
_COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4",
                "sessionid", "set-cookie")
_COOKIE_RE = re.compile(
    r"(?:%s)\s*[\"']?\s*[:=]\s*[\"']([^\"']{12,})[\"']" % "|".join(_COOKIE_KEYS))
# 大模型密钥的字面量(拼出来写, 免得本文件自己被自己命中)
_KEY_RE = re.compile("sk" + "-[A-Za-z0-9_-]{20,}")

_DATA_EXT = (".json", ".env", ".ini", ".txt", ".cfg", ".conf", ".log",
             ".tsv", ".csv", ".yaml", ".yml")
_CODE_EXT = (".py", ".bat", ".cmd", ".sh", ".js", ".html")
# 第三方目录一律不进扫描: 依赖包里那些 SPDX 许可串(`sk-linking-...`)长得像密钥,
# 报出来是纯噪音 —— 自检红了却没人该动它, 下次真漏洞就没人看了。
_SKIP_DIRS = ("browser_data", "__pycache__", ".git", "node_modules",
              "venv", ".venv", "site-packages", "dist-info", ".mypy_cache",
              ".pytest_cache", "playwright")
_MAX = 2 * 1024 * 1024                     # 超过 2MB 的当二进制/数据块, 不逐行读
# 这几样是**这台机器自己的**运行时配置(部署机上必须有、密钥就住里头), 不是"包里的文件"。
# 它们在不在包里由 make_dist.py 的 DROP_FILES 管; 这里再逐字扫内容, 只会在部署机上刷出一条
# 与真相无关的红。
_MACHINE_FILES = ("harness.env", ".env", "settings.json", "secrets.json")

fails = []
skips = []
n_chk = 0


def chk(name, cond, extra=""):
    global n_chk
    n_chk += 1
    print("  %s %s" % ("ok " if cond else "XX ", name))
    if not cond:
        fails.append("%s | %s" % (name, extra))


def skip(name, why):
    """这一条在当前的树上**不适用**(不是通过也不是失败) —— 印出理由, 别让它变成噪音红。"""
    print("  -- %s (%s)" % (name, why))
    skips.append(name)


print("=== 1. 凭证文件一个都不许在 ===")
for rel in CRED_PATHS:
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    chk("1. 不存在 %s" % rel, not os.path.exists(p), p)

print("=== 2. NGA 的 config.json: 必须留着(不然服务起不来)、必须掏空 ===")
_cfg = os.path.join(ROOT, "crawlers", "nga", "config.json")
if not os.path.isdir(os.path.join(ROOT, "crawlers")):
    # 部署机上爬虫不在树内(如 <DEPLOY_ROOT>/nga-demo、<DEPLOY_ROOT>/bili-demo) —— 整段不适用。
    skip("2. NGA config.json 掏空", "这棵树里没有 crawlers/: 爬虫不在树内(部署布局)")
elif not os.path.exists(_cfg):
    chk("2. config.json 在(服务没它起不来)", False, _cfg)
else:
    try:
        import json                                     # noqa: E402
        with open(_cfg, encoding="utf-8") as f:
            _d = json.load(f)
    except Exception as ex:
        _d = None
        chk("2. config.json 解得开", False, "%s: %s" % (_cfg, ex))
    if _d is not None:
        _c = _d.get("cookies") or {}
        chk("2. config.json 里 cookies 是空的", not _c,
            "还留着 %d 个 cookie 键" % len(_c))
        chk("2. config.json 里没填登录标志(那是账号名)", not str(_d.get("login_marker") or "").strip(),
            "login_marker 有值 —— 那是上一台机器的账号")

print("=== 3. 全树找 .tool_token / 令牌文件 ===")
hits = []
for base, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
    for fn in files:
        if fn == ".tool_token" or fn.endswith(".tool_token"):
            hits.append(os.path.join(base, fn))
chk("3. 没有 .tool_token", not hits, hits)

print("=== 4. 数据文件里没有 cookie 值 / 代码里没有密钥字面量 ===")
bad = []
for base, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
    for fn in files:
        full = os.path.join(base, fn)
        if os.path.abspath(full) == ME or fn in _MACHINE_FILES:
            continue
        ext = os.path.splitext(fn)[1].lower()
        if ext not in _DATA_EXT and ext not in _CODE_EXT:
            continue
        try:
            if os.path.getsize(full) > _MAX:
                continue
            with open(full, encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        for m in _COOKIE_RE.finditer(src):
            bad.append("%s 有 cookie 值: %s=%.12s..." % (full, m.group(0)[:24], m.group(1)))
        for m in _KEY_RE.finditer(src):
            bad.append("%s 有密钥字面量: %.12s..." % (full, m.group(0)))
chk("4. 干净", not bad, "\n     ".join(bad[:10]))

print("-" * 56)
print("chk=%d PASS=%d FAIL=%d SKIP=%d" % (n_chk, n_chk - len(fails), len(fails), len(skips)))
for f in fails:
    print("  FAIL:", f)
sys.exit(1 if fails else 0)
