#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前台接线自检(静态, 不启浏览器) —— 分发包这一版。

底子是开发期那份 `_tmp/verify_web_wiring.py`, 改动只有三处:
  · 认的是分发包的目录, 且**不读 demo_data.js**(演示数据不进分发包, 平台键改从 webapp.py 的 CRAWLERS 读);
  · 「左上角图标位预留(#logoSlot)」这条反过来 —— 产品**不带 logo**, 得查它真没了;
  · 补上本轮新加的两块: 首屏居中页(.hero / body.empty)与多对话列表(登记簿/SID 切换)。

只做静态核对: 不启浏览器(铁律)、不装东西。捉的是改版后最常见的坏法 ——
删了节点却把 JS 里的 $("#id") 留着、类名 toggle 了 CSS 里没定义、id 改了没人跟。
"""
import json
import os
import re
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HTML = os.path.join(ROOT, "web", "index.html")

fails = []
total = ok = 0


def chk(name, cond, got=""):
    global total, ok
    total += 1
    print(("PASS" if cond else "FAIL"), name)
    if cond:
        ok += 1
    else:
        fails.append(name + " -> " + repr(got)[:200])


html = open(HTML, encoding="utf-8").read()
css = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
js = "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S))
markup = re.sub(r"<style>.*?</style>|<script.*?</script>", "", html, flags=re.S)
# 去注释后的脚本, 专给"这几步得挨着调"那类结构检查用 —— 中间夹一行 // 注释不该算脱钩。
# 负向环视保住 URL 里的 ://(别的 // 在 JS 里只可能是注释)。
jsx = re.sub(r"(?<!:)//[^\n]*", "", js)

# ---- ① 顶栏已撤, 而且不是"藏起来"而是真没了 ----
chk("1.1 无 <header> 元素(顶栏整条撤掉)", "<header" not in markup, "")
chk("1.2 顶栏三件套不再挂在页面上(无 #lights / .crawler / .knob 节点)",
    not re.search(r'id="lights"|class="[^"]*\bcrawler\b|class="[^"]*\bknob\b', markup), "")

# ---- ② 侧栏: 品牌 + 「开启新对话」 + 多对话列表 ----
chk("2.1 产品不带 logo(既无 #logoSlot 节点, 也无 .logo 样式)",
    'id="logoSlot"' not in markup and not re.search(r"\.logo\s*\{", css), "")
chk("2.2 「开启新对话」按钮存在且不再是空壳(接到 newSidStart)",
    'id="newSess"' in markup and "开启新对话" in markup
    and re.search(r'\$\("#newSess"\)\s*\.onclick\s*=\s*newSidStart', js) is not None, "")
chk("2.3 对话列表按时间分组(7 天内 / 30 天内)",
    "7 天内" in js and "30 天内" in js, "")
chk("2.4 分组逻辑挂在渲染里(groupOf/daysAgo)",
    "function groupOf" in js and "function daysAgo" in js, "")
chk("2.5 历史条目单行标题, 无副行",
    "li.row" in css and ".sess li small" not in css, "")
chk("2.6 不抄参考图底部的账号行", "EZ2lie" not in html
    and not re.search(r'class="[^"]*\buserrow\b', css), "")

# ---- ②b 多对话: 登记簿在 localStorage, 每个对话一个 SID(后端一个 scope) ----
chk("2b.1 对话登记簿存在(ha_sids)且读写成对",
    "ha_sids" in js and "localStorage.setItem" in js and "localStorage.getItem" in js, "")
chk("2b.2 当前对话记在 ha_sid, 切/新建都会回写",
    re.search(r'localStorage\.setItem\(k,\s*v\)', js) is not None
    and re.search(r'\$\("#newSess"\)\.onclick\s*=\s*newSidStart', js) is not None
    and "function switchSid" in js, "")
chk("2b.3 列表列出的是**对话**不是题(渲染用 SIDS, 不再是 D.asks 反查)",
    re.search(r"function renderSessions\(\)\{[\s\S]*?SIDS\.forEach", js) is not None
    and "function sessions()" not in js, "")
chk("2b.4 切对话会把上一个的题清干净 + 重新拉账(不串)",
    re.search(r"function switchSid[\s\S]*?D\.asks=\[\]", js) is not None
    and re.search(r"function switchSid[\s\S]*?refreshHistory\(\)", js) is not None, "")
chk("2b.5 切换/新建都带题在跑的闸门(asking)", len(re.findall(r'if\(asking\)\s*return toast', js)) >= 2, "")
chk("2b.6 对话标题取自最早的问句(不编标题)",
    re.search(r"function sidRemember[\s\S]*?D\.asks\[0\]", js) is not None, "")
chk("2b.7 一次问答后回写登记簿(sidRemember 紧跟着 renderSessions)",
    re.search(r"sidRemember\(\);\s*renderSessions\(\)", jsx) is not None, "")

# ---- ②d 每个对话的管理: 「⋯」→ 置顶 / 删除(09-16 补) ----
chk("2d.1 每个对话行右边有「⋯」管理键(不再是只有一个可点的整行)",
    'class="rowmore"' in js and 'data-more="' in js and 'class="t"' in js
    and re.search(r"\.sess li\.row \.rowmore\s*\{", css) is not None, "")
chk("2d.2 「⋯」常显(不靠悬停才冒出来: 触屏没有悬停这一步), 悬停/展开时提亮",
    re.search(r"\.sess li\.row \.rowmore\s*\{[^}]*opacity:\s*\.6", css) is not None
    and re.search(r"\.rowmore:hover[^{]*\{[^}]*opacity:\s*1", css) is not None, "")
chk("2d.3 菜单三项齐全: 置顶/取消置顶 + 导出为 PDF + 删除这个对话",
    'id="sidMenu"' in markup and 'data-act="pin"' in markup and 'data-act="del"' in markup
    and 'data-act="pdf"' in markup and 'e.pin ? "取消置顶" : "置顶"' in js, "")
_pin_i, _grp_i = js.find('class="grp">置顶'), js.find("if(!buckets[g].length) return;")
chk("2d.4 置顶的另起一组, 且排在「7 天内/30 天内/更早」**前面**(不埋在老对话堆里)",
    _pin_i > 0 and _grp_i > _pin_i, (_pin_i, _grp_i))
chk("2d.5 置顶状态进登记簿(不是只活在内存里, 刷新就没了), 且排序把置顶放前面"
    "(不放前面的话, 老对话会被 registryPut 的 80 条截断悄悄切掉)",
    re.search(r"function sidPin[\s\S]*?e\.pin=!e\.pin[\s\S]*?registryPut\(\)", js) is not None
    and re.search(r"function sidOrder[\s\S]*?a\.pin\s*\?\s*-1\s*:\s*1", js) is not None, "")
chk("2d.6 「删除」打的是真接口 /api/forget(不是只从侧栏拿掉)",
    re.search(r'api\("/api/forget"', js) is not None and "function sidAskDelete" in js, "")
chk("2d.7 删前要手打确认词, 打对之前按钮是禁用的",
    'id="delInput"' in markup and re.search(r'id="delGo"[^>]*disabled', markup) is not None
    and '.trim()!=="删除这个对话"' in js, "")
chk("2d.8 删的正是当前这个时自动切走(不留在一个已经删掉的对话里)",
    re.search(r"if\(id!==SID\)\{\s*renderSessions\(\);\s*return;\s*\}", js) is not None
    and re.search(r"if\(id!==SID\)[\s\S]{0,300}sidUse\(", js) is not None, "")
chk("2d.9 删除窗如实写明口径: 连后端记忆一起清 / 7 天内可在垃圾桶恢复 / 证据库与 [id=N] 不受影响",
    re.search(r'id="mDel"[\s\S]*?全部记忆[\s\S]*?垃圾桶[\s\S]*?可以恢复[\s\S]*?证据库不受影响', markup) is not None
    and re.search(r'id="mDel"[\s\S]*?\[id=N\]', markup) is not None, "")
chk("2d.10 点「⋯」不会顺带把这一行也切过去(stopPropagation)",
    re.search(r'querySelectorAll\("\[data-more\]"\)[\s\S]{0,200}stopPropagation', jsx) is not None, "")
chk("2d.11 点别处/滚列表/改窗口都收掉菜单(不留一个飘着的浮层)",
    re.search(r'addEventListener\("click",\s*function\(e\)\{\s*if\(\$\("#sidMenu"\)', js) is not None
    and '"scroll", sidMenuClose' in js and '"resize", sidMenuClose' in js, "")

# ---- ②e 垃圾桶: 设置面板最后一栏, 删掉的对话能捡回来(09-16 补) ----
_tsec_i, _ttool_i = js.find("function trashSection"), js.find("function loadSettings")
chk("2e.1 设置里真有「垃圾桶」这一栏, 且在工具卡片之后",
    'data-sec="' in js and "垃圾桶" in js
    and re.search(r'cardGrid\(g,tp\)[\s\S]{0,400}trashSection\(tr\)', js) is not None, "")
chk("2e.2 顶上那排多了一个「垃圾桶」页签(不是藏在别处找不到)",
    re.search(r'h\+=\'<button type="button" data-sec="\'\+gs\.length\+\'">垃圾桶</button>\'', js) is not None, "")
chk("2e.3 列表数据来自后端 /api/trash(不在前端自己算还剩几天)",
    re.search(r'api\("/api/trash"\)', js) is not None
    and re.search(r"function trashSection[\s\S]*?it\.days_left", js) is not None, "")
chk("2e.4 「恢复」打的是真接口 /api/restore, 并带上会话号",
    re.search(r'api\("/api/restore"', js) is not None
    and re.search(r"function trashRestore[\s\S]*?session:sid", js) is not None, "")
chk("2e.5 恢复成功后把那一行加回侧栏(不这么做, 恢复出来的对话在侧栏里看不见)",
    re.search(r"function trashRestore[\s\S]*?sidFind\(sid\)[\s\S]*?SIDS\.unshift[\s\S]*?renderSessions\(\)", js) is not None, "")
chk("2e.6 恢复后重画这一栏(恢复掉的那条不该还留在垃圾桶里)",
    re.search(r"function trashRestore[\s\S]*?loadSettings\(\)", js) is not None, "")
chk("2e.7 删掉的那一刻 toast 要说清「去哪儿捡」, 而不是一句「没了」",
    re.search(r'toast\("已删除这个对话[\s\S]{0,200}垃圾桶', js) is not None, "")
chk("2e.8 删除时把标题一起送过去(不送的话垃圾桶里那行认不出是哪个对话)",
    re.search(r'api\("/api/forget",\{method:"POST",body:JSON\.stringify\(\{session:id,title:DEL_TITLE\}\)\}\)', js) is not None, "")

# ---- ②c 首屏: 居中一竖列, 只在空对话时出现 ----
chk("2c.1 首屏节点在对话区里、且在 #wrap 之前",
    re.search(r'id="stream"[\s\S]*?class="hero"[\s\S]*?id="wrap"', markup) is not None, "")
# 首屏**只要标题 + 示例问句位** —— 中间那句说明(「现爬社区原帖 → …」)已按用户要求撤掉,
# 这段就不再要求 hero 里必须有个 <p> 了; 反过来查一句, 别哪天又被加回去。
chk("2c.2 首屏只有标题 + 示例问句位(中间那段说明已撤, 且不许再长回来)",
    re.search(r'class="hero"[\s\S]*?<h1>[\s\S]*?id="egBox"', markup) is not None
    and re.search(r'class="hero"[\s\S]*?<p>', markup) is None, "")
chk("2c.3 空对话才显示(CSS 挂在 body.empty 上, 不是一直摆着)",
    re.search(r"body\.empty\s+\.hero\s*\{[^}]*display:\s*flex", css) is not None
    and re.search(r"body\.empty\s+#wrap\s*\{[^}]*display:\s*none", css) is not None, "")
chk("2c.4 空对话时标题与输入框**一起**居中(居中的是整摞, 不是只吊标题)",
    re.search(r"body\.empty\s+main\s*\{[^}]*justify-content:\s*center", css) is not None
    and re.search(r"body\.empty\s+\.stream\s*\{[^}]*flex:\s*0\s+0\s+auto", css) is not None, "")
chk("2c.5 这个类由代码切(paintEmpty 的条件里带 asking), 且题一发出就重画",
    "function paintEmpty" in jsx and re.search(r"classList\.toggle\(\"empty\",\s*!\(D\.asks", jsx) is not None
    and re.search(r"asking\s*&&\s*!asking|!\(D\.asks[\s\S]{0,80}asking", jsx) is not None
    # 「题一发出就重画」这半条: 置 asking 与重画要挨着 —— 中间隔太多行的话, 发题到占位卡
    # 出现在屏幕之间会慢一拍。09-16 起置位走 setAsking(它同时管发送键与停止键), 两种写法都认。
    and re.search(r"asking=true;|setAsking\(true\);[\s\S]{0,200}(paintEmpty|renderAll)\(\)",
                  jsx) is not None, "")
chk("2c.6 首屏在启动时就摆好(不等接口回来); 侧栏同理 —— 本地那本先画, 后端那份随后补",
    re.search(r"paintEmpty\(\);[\s\S]{0,90}renderSessions\(\);[\s\S]{0,90}refreshSessions\(\);"
              r"[\s\S]{0,90}refreshStatus", jsx) is not None, "")
chk("2c.7 示例问句点一下只填进输入框, 不直接发(一题要爬几分钟)",
    re.search(r"EG\.forEach", js) is not None
    and re.search(r'\$\("#q"\)\.value=b\.textContent', js) is not None, "")

# ---- ③ 输入区 = 一个盒, 盒内只剩档位 + 平台比例(原地展开的滑动按钮) ----
chk("3.1 输入区是单个 .composer .box", 'class="box"' in markup and ".composer .box" in css, "")
chk("3.2 档位 chip 组在盒内(#modeSeg + data-mode)",
    'id="modeSeg"' in markup and 'data-mode="快速"' in markup and 'data-mode="精准"' in markup, "")

_as = markup.index("<aside>"), markup.index("</aside>")
aside_html = markup[_as[0]:_as[1]]
_ci = markup.index('<div class="composer">'), markup.index("</main>")
composer_html = markup[_ci[0]:_ci[1]]

chk("3.3 平台比例是**原地展开的按钮**(#ratioBtn → #ratioMorph), 不是弹窗",
    'id="ratioBtn"' in composer_html and 'id="ratioMorph"' in composer_html
    and 'id="mRatio"' not in markup, "")
chk("3.4 「圆 → 胶囊」动效在位(.morph 收起窄 + .morph.open 变宽 + width 过渡)",
    re.search(r"\.morph\s*\{[^}]*transition:[^}]*width", css) is not None
    and re.search(r"\.morph\.open\s*\{[^}]*width", css) is not None, "")
chk("3.5 滑杆在胶囊内(#ratioRange + #ratioVal)",
    'id="ratioRange"' in composer_html and 'id="ratioVal"' in composer_html, "")
chk("3.6 爬虫状态落在**侧栏底**(.sbot 内两行: NGA / bilibili)",
    'class="sbot"' in aside_html
    and re.search(r'class="sbot"[\s\S]*?id="stNga"', aside_html) is not None
    and re.search(r'class="sbot"[\s\S]*?id="stBili"', aside_html) is not None, "")
chk("3.7 「各项设置」也在侧栏底(.sbot 内 #settingsBtn + 窗 #mSettings)",
    re.search(r'class="sbot"[\s\S]*?id="settingsBtn"', aside_html) is not None
    and 'id="mSettings"' in markup, "")
chk("3.8 爬虫状态/设置不再挂在输入盒里",
    not any(k in composer_html for k in ('id="stNga"', 'id="stBili"', 'id="settingsBtn"')), "")
chk("3.9 状态详情窗保住原来的两盏灯(服务/登录态)",
    'id="mStatus"' in markup and 'id="statusBody"' in markup
    and "服务" in js and "登录态" in js, "")
chk("3.10 清除对话记忆并进了设置这个预留位",
    re.search(r'id="mSettings"[\s\S]*?id="clearMem"', markup) is not None, "")
chk("3.11 问句气泡与答案卡同一条右边线(.q 列向 flex + align-items:flex-end)",
    re.search(r"\.q\s*\{[^}]*flex-direction:\s*column", css) is not None
    and re.search(r"\.q\s*\{[^}]*align-items:\s*flex-end", css) is not None, "")

# ---- ④ 口径: 数字不编 ----
chk("4.1 前端不再出现 evidence_cap(该字段随上限一起撤了)", "evidence_cap" not in html, "")
chk("4.2 trace 明说证据全量进上下文", "全部</b>进上下文" in js, "")
chk("4.3 清除记忆窗不再写死对话数(改成按登记簿算)",
    re.search(r'id="clearCnt"', markup) is not None
    and re.search(r'id="clearCnt"[\s\S]{0,40}…', markup) is not None
    and re.search(r'\$\("#clearCnt"\)\.textContent=SIDS\.length', js) is not None
    and not re.search(r"\d+\s*个会话\s*/\s*\d+\s*条消息", html), "")

# ---- ④b 动效: 克制、只挂在状态真变的地方, 且不该让整屏反复重放 ----
chk("4b.1 入场动画只给**新出现**的那条(有 .msg.enter + 记账本, 不是给所有 .msg 挂)",
    re.search(r"\.msg\.enter\s*\{[^}]*animation", css) is not None
    and "function markFresh" in jsx and re.search(r"PAINTED\[k\]", jsx) is not None
    and re.search(r"markFresh\(\)", jsx) is not None, "")
chk("4b.2 题在跑时有占位卡(别让爬的那几分钟页面看着像死了)",
    "function renderPending" in jsx and re.search(r"h\+renderPending\(\)", jsx) is not None
    and re.search(r"\.card\.pending\s*\{", css) is not None
    and re.search(r"\.typing\s+i\s*\{[^}]*animation", css) is not None, "")
chk("4b.3 发题时立刻摆占位、答案回来撤掉(pending 设了也要清)",
    re.search(r"pending=\{q:", jsx) is not None and re.search(r"pending=null", jsx) is not None, "")
chk("4b.4 系统设了「减少动态效果」就把动画全关掉(无障碍, 不是可选项)",
    re.search(r"prefers-reduced-motion[\s\S]{0,300}animation:none", css) is not None, "")
# 时长分三档量, 因为三种动效的"合理"标准本来就不一样: 交互过渡、一次性入场、循环呼吸/跳动。
# (.morph 那条 0.36s 是"圆→胶囊"的布局变化, 有意比别处慢半拍, 见 3.4 —— 所以过渡这档放到 0.4s。)
_tr = []
for _m in re.findall(r"transition:([^;}]*)", css):
    _tr += [float(x) for x in re.findall(r"(\d*\.\d+)s", _m)]
_one = [float(x) for x in re.findall(r"animation:(?:riseIn|fadeIn|popIn)\s+(\d*\.\d+)s", css)]
_loop = [float(x) for x in re.findall(r"animation:(?:busyPulse|dotJump)\s+(\d*\.\d+)s", css)]
chk("4b.5 交互过渡都在 0.1-0.4s(不拖沓, 也不至于快得看不见)",
    bool(_tr) and all(0.1 <= x <= 0.4 for x in _tr), _tr)
chk("4b.6 入场动画在 0.15-0.35s 档(一次性的, 别慢到挡着操作)",
    bool(_one) and all(0.15 <= x <= 0.35 for x in _one), _one)
chk("4b.7 循环动画是「呼吸/跳动」不是入场: 0.8-2s 且都标了 infinite",
    bool(_loop) and all(0.8 <= x <= 2.0 for x in _loop)
    and len(re.findall(r"animation:\w+\s+\d*\.\d+s[^;}]*infinite", css)) >= len(_loop), _loop)

# ---- 接线完整性: JS 里点到的 id 必须真在 HTML 里(改版最常见的坏法) ----
ids_html = set(re.findall(r'\bid="([^"]+)"', markup))
ids_js = set(re.findall(r'\$\("#([A-Za-z0-9_\-]+)"\)', js))
ids_js |= set(re.findall(r'open\("#([A-Za-z0-9_\-]+)"\)', js))
# 这几个是**运行时才生出来的**节点(二维码图由 qrShow/ngaQrShow 现写 innerHTML, 「加一个」由 settingsBody
# 现拼, 爬取实时画面那几块由 renderPending 现拼), 取用处都带
# if($("#x")) 兜底 —— 它们不在静态页面里是应当的, 不算漏接。
RUNTIME_IDS = {"qrImg", "ngaQrImg", "mcpAdd",
               "pendSteps", "pendLive", "pendDraft", "mdlRow", "mdlProbe"}
missing = sorted(i for i in ids_js if i not in ids_html and i not in RUNTIME_IDS)
chk("5.1 JS 引用的 %d 个 #id 全部存在于页面" % len(ids_js), not missing, missing)

# 动态填的爬虫状态行: "st" + key 首字母大写 + 余下 —— 平台键改了这里就会撞。
# 分发包里没有 demo_data.js(演示数据不进包), 键从后端 webapp.py 的 CRAWLERS 读 —— 那才是真源。
_webapp = open(os.path.join(ROOT, "harness", "webapp.py"), encoding="utf-8").read()
_m = re.search(r"^CRAWLERS\s*=\s*\((.*?)\)\s*$", _webapp, re.S | re.M)
keys = set(re.findall(r'"([a-z]+)"\s*,\s*"[^"]*"\s*,\s*"[^"]*"', _m.group(1) if _m else ""))
chip_ids = set(re.findall(r'id="(st[A-Za-z]+)"', markup))
want = {"st" + k[:1].upper() + k[1:] for k in keys}
chk("5.2 爬虫状态行 id 与后端 CRAWLERS 的平台键对得上(%s)" % ",".join(sorted(keys)),
    bool(keys) and want.issubset(chip_ids), (sorted(want), sorted(chip_ids)))

# ---- 类名: JS 里 toggle 的类必须在 CSS 里有定义, 否则样式切换是哑的 ----
cls_js = set(re.findall(r'classList\.(?:add|remove|toggle|contains)\("([^"]+)"', js))
cls_js |= set(re.findall(r'className="([^"]+)"', js))
undef = sorted(c for c in cls_js if not re.search(r"[.\s]" + re.escape(c) + r"[\s,{.:\[]", css))
chk("5.3 JS 切状态的类都在 CSS 里有定义", not undef, undef)

# ---- 语法: 能跑 node 就顺手 --check 一次; 没有 node 不算失败(本文件仍是零依赖) ----
node = shutil.which("node")
if node:
    tmp = os.path.join(HERE, "_web_inline_check.js")
    open(tmp, "w", encoding="utf-8").write(js)
    r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
    os.remove(tmp)
    chk("6.1 内联脚本语法通过(node --check)", r.returncode == 0, r.stderr[:400])
else:
    print("SKIP 6.1 node 不在 PATH, 跳过语法检查(不判失败)")

# ---- ⑦ 事件时间轴: 把 index.html 里真那份窗口函数抠出来, 喂假数据跑 ----
# 用户 2026-09-17 的原话:「你这时间轴你自己看了没 这是人看的吗」。坏在哪很具体: 老帖能翻到七年前,
# 而样本几乎全堆在最近半个月 —— 整幅铺开时右边挤成一条线、左边一大片空白, 181 条看着像十几个点。
# 所以两件事必须钉死: ① 默认窗口落在样本密的那一段, 且窗口外还剩多少条要如实报出来(不许闷着截断);
# ② 样本本来就摊得均匀时不许乱放大(那会把一条均匀的曲线谎报成"最近才爆发")。
if node:
    def _grab(name):
        at = js.find("function " + name + "(")
        if at < 0:
            return ""
        i = js.index("{", at)
        d = 0
        for j in range(i, len(js)):
            if js[j] == "{":
                d += 1
            elif js[j] == "}":
                d -= 1
                if d == 0:
                    return js[at:j + 1]
        return ""

    _src = _grab("tlWindow")
    _day = _grab("tlDay")
    chk("7.1 时间轴按天并点(同一天几十条重叠在一起等于没画): 有 tlDay, 桶键走它",
        bool(_day) and "k=tlDay(e.t)" in js, (_day[:40], "k=tlDay(e.t)" in js))
    chk("7.2 默认窗口按「装得下多少条」挑, 不是拍脑袋写死天数",
        bool(_src) and "spans=[7,14,30,60,90,180,365]" in _src
        and re.search(r"c\s*/\s*ts\.length\s*>=\s*0\.55", _src) is not None, _src[:200])
    if not (_src and _day):
        chk("7.3 抠得出 tlWindow/tlDay 才跑得动这一段", False, "抓不到函数")
    else:
        # ① 七年跨度、九成样本堆在最近两周(真数据就长这样) ② 均匀摊三年
        _js7 = """
const TL_DAY=86400000;
"""+_day+"""
"""+_src+"""
const DAY=86400000, now=Date.parse("2026-09-15T12:00:00");
var skew=[], i;
for(i=0;i<181;i++) skew.push(now - (i<109 ? i*0.12 : 400+i*12)*DAY);  // 109 条挤在最近两周, 其余散到七年前
var flat=[]; for(i=0;i<181;i++) flat.push(now - i*6*DAY);             // 三年摊匀
function cnt(ts,w){ return ts.filter(function(t){ return t>=w.s&&t<=w.e; }).length; }
var a=tlWindow(skew), b=tlWindow(flat), sa=cnt(skew,a), sb=cnt(flat,b);
console.log(JSON.stringify({
  skewDays:a.days, skewOut:a.out, skewIn:sa, skewPct:Math.round(sa*100/skew.length),
  flatDays:b.days, flatOut:b.out, flatPct:Math.round(sb*100/flat.length)}));
"""
        _t7 = os.path.join(HERE, "_web_tl_check.js")
        open(_t7, "w", encoding="utf-8").write(_js7)
        _r7 = subprocess.run([node, _t7], capture_output=True, text=True, encoding="utf-8")
        os.remove(_t7)
        if _r7.returncode != 0:
            chk("7.3 这一段跑得起来", False, (_r7.stderr or "")[-300:])
        else:
            import json as _json
            _o7 = _json.loads(_r7.stdout.strip().splitlines()[-1])
            chk("7.3 一边倒的数据: 窗口收到「最近的密段」, 且把窗口外那 72 条如实数出来",
                _o7["skewDays"] in (7, 14, 30) and _o7["skewPct"] >= 55
                and _o7["skewOut"] == 181 - _o7["skewIn"] and _o7["skewOut"] > 0, _o7)
            chk("7.4 摊匀的数据: 整幅铺开(不许乱放大 —— 那会把均匀的曲线谎报成'最近才爆发')",
                _o7["flatDays"] == 0 and _o7["flatOut"] == 0 and _o7["flatPct"] >= 99, _o7)

# ---- ⑧ 时间轴**真画一遍**(node 里用 echarts 的服务端渲染, 不启浏览器) ----
# ⑦ 那条只看窗口算得对不对; 这条看"到底画出来了没有" —— 用户上回的原话是「你这时间轴你自己看了没」,
# 所以这里不让它停在"代码看着对": 把真那份 timelinePlot 抠出来, 喂一撮构造好的样本, 让 echarts
# 当场算坐标, 再看它要画几根条、条上有没有字、颜色对不对。条宽/字号这类只有眼睛能看的东西另说。
if node:
    _need = ["timelinePlot", "tsOf", "laneOf", "tlDay", "tlWindow"]
    _parts = {}
    for _n in _need:
        _parts[_n] = _grab(_n)
    _lv = js.find("var LANE_ORDER=")
    if not all(_parts.values()) or _lv < 0:
        chk("8.0 抠得出画图那几个函数", False, [k for k, v in _parts.items() if not v])
    else:
        _lvend = js.find("function tsOf(")
        _tlconst = "".join(re.findall(r"var TL_[A-Z_]+=[^;]+;", js))
        _src8 = (js[_lv:_lvend] + _tlconst + "\n" + "\n".join(_parts[_n] for _n in _need))
        # 7 条样本: 2 条老帖(把窗口拉到最近那段) + 5 条挤在最后两天, 其中 1 条被引用
        _ask8 = {
            "id": "t8", "question": "画得出来吗", "collected_at": "2026-09-15T12:00:00",
            "cites": [{"id": 9, "kind": "reply", "platform": "bilibili", "title": "被引的那条",
                       "published_at": "2026-09-14 10:00:00", "like_count": 5, "reply_count": 1,
                       "text": "这条的正文该出现在气泡里"}],
            "uncited": [
                {"id": 1, "kind": "post", "platform": "NGA", "title": "老帖甲",
                 "published_at": "2019-01-01 09:00:00"},
                {"id": 2, "kind": "post", "platform": "NGA", "title": "老帖乙",
                 "published_at": "2020-02-02 09:00:00"},
                {"id": 3, "kind": "reply", "platform": "bilibili", "title": "评论甲",
                 "published_at": "2026-09-14 11:00:00", "like_count": 9, "text": "评论甲说了句啥"},
                {"id": 4, "kind": "reply", "platform": "bilibili", "title": "评论乙",
                 "published_at": "2026-09-14 12:00:00"},
                # 正文是个横杠 —— B 站视频没写简介时爬虫存的就是这个(全库 97 行都长这样)。
                # 它不该被当成内容写进气泡(用户原话「为啥有这种东西？？？」), 该退回写标题。
                {"id": 5, "kind": "reply", "platform": "bilibili", "title": "评论丙",
                 "published_at": "2026-09-15 08:00:00", "like_count": 3, "text": "-"},
                {"id": 6, "kind": "post", "platform": "NGA", "title": "新帖",
                 "published_at": "2026-09-14 08:00:00"},
            ],
        }
        _js8 = """const echarts=require(process.argv[2]);
var AX={axisLine:"#e3e8ef",axisLabel:"#98a2b3",label:"#667085",splitLine:"#f0f2f5"};
function esc(s){return String(s==null?"":s);}
function hasEcharts(){return true;}
function $(sel){ return null; }
function plotFallback(el,msg){ console.log("FALLBACK "+msg); }
var CAP=null, DRAWN=[];
function mkChart(el){ return { setOption:function(o){ CAP=o; } }; }
var ASK=__ASK__;
__SRC__
var FAKE={style:{}};
timelinePlot(FAKE, ASK);
if(!CAP){ console.log("NO_OPTION"); } else {
  CAP.animation=false;
  var orig=CAP.series[0].renderItem;
  CAP.series[0].renderItem=function(p, api){
    var r=orig(p, api), g=r.children[0];
    DRAWN.push({lane:String(api.value(2)), w:Math.round(g.shape.width), h:Math.round(g.shape.height),
                cy:Math.round(g.shape.y+g.shape.height/2),
                fill:g.style.fill, op:(g.style.opacity==null?1:g.style.opacity),
                txt:(r.children[1]? String(r.children[1].style.text) : "")});
    return r;
  };
  var ch=echarts.init(null,null,{renderer:"svg",ssr:true,width:900,
                                 height:parseInt(FAKE.style.height||"270",10)});
  ch.setOption(CAP);
  var svg=ch.renderToSVGString();
  /* 点不看配置、只看**画出来的图元**: 报"配置里有 N 个点"是骗人的, 上回就是配错了维数
     (写成 [时刻, 时刻, 行]) 结果一个点都没落下来, 配置里却明明有。 */
  var dots=[];
  var re=/transform="matrix\(([\d.]+),0,0,[\d.]+,([\d.]+),([\d.]+)\)"[^>]*fill="(#[0-9a-fA-F]{6})"[^>]*ecmeta_series_index="1"/g, m;
  while((m=re.exec(svg))) dots.push({r:parseFloat(m[1]), x:Math.round(parseFloat(m[2])),
                                     y:Math.round(parseFloat(m[3])), fill:m[4]});
  /* 气泡里的话也真调一遍 formatter 看 —— 用户点着气泡提过两次要求(「不要把点赞和回复合计了」
     「这块爬到了啥内容直接写出来, 不是让你 13 5」)。这种"配置看着对、写出来是别的字"的东西,
     只有把函数叫出来看才作数。每根条、每个点都叫一遍。 */
  var _fmt=CAP.tooltip.formatter;
  var _btips=CAP.series[0].data.map(function(d){ return _fmt({seriesName:"活跃期", data:d}); });
  var _dtips=CAP.series[1].data.map(function(d){ return _fmt({seriesName:"样本", data:d}); });
  console.log(JSON.stringify({bars:DRAWN, dots:dots, zoom:CAP.dataZoom.map(function(z){return [z.start,z.end];}),
                              svgLen:svg.length, hasMark:(svg.indexOf("本次采集")>=0),
                              nDots:CAP.series[1].data.length,
                              btips:_btips, dtips:_dtips,
                              animUpd:CAP.animationDurationUpdate,
                              dotSeries:!!(CAP.series[1]&&CAP.series[1].type==="scatter")}));
}
process.exit(0);
"""
        _js8 = _js8.replace("__ASK__", json.dumps(_ask8, ensure_ascii=False)).replace("__SRC__", _src8)
        _f8 = os.path.join(HERE, "_web_tl_draw.js")
        open(_f8, "w", encoding="utf-8").write(_js8)
        _r8 = subprocess.run([node, _f8, os.path.join(ROOT, "web", "vendor", "echarts.min.js")],
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
        os.remove(_f8)
        if _r8.returncode != 0 or not (_r8.stdout or "").strip():
            chk("8.0 这一遍真画得出来(node 里 echarts 服务端渲染)", False, (_r8.stderr or "")[-3000:])
        elif (_r8.stdout or "").strip().startswith("NO_OPTION"):
            chk("8.0 时间轴把配置交给了 echarts", False, "setOption 没被调到")
        else:
            import json as _json2
            _o8 = _json2.loads(_r8.stdout.strip().splitlines()[-1])
            _bars, _dots = _o8["bars"], _o8["dots"]
            _txt = sorted(b["txt"] for b in _bars if b["txt"])
            # 窗口里: NGA 只有 09-14 一条 → 一根条(1); B站 09-14 三条 + 09-15 一条, 这两天挨着
            # → 接成一根长条(4)。最后那天最容易掉: 窗口切在采集那一刻(中午)时它骑在边上会被整根吞掉。
            chk("8.1 邻近的接成一根长条: 窗口里画出 2 根, 宽度都 > 0(不是退化成空白)",
                len(_bars) == 2 and all(b["w"] > 0 for b in _bars), _bars)
            chk("8.2 条上写着这段共几条(1 / 4 —— 接起来的是这两天之和)", _txt == ["1", "4"], _txt)
            chk("8.3 长条是泳道色、半透(当「一段」的底), 不是被引用的那条变深",
                sorted(set(b["fill"] for b in _bars)) == ["#c084fc", "#e0a83a"]
                and sorted(set(b["op"] for b in _bars)) == [0.32], _bars)
            # 点: 真从画出来的图元里数(不是看配置)。三个点 —— NGA 09-14 一个, B站 09-14 一个, 09-15 一个。
            _cy_by_color = {}
            for _b in _bars:
                _cy_by_color[_b["fill"]] = _b["cy"]
            # 数据里共 5 个「那天·那个来源」, 2019/2020 那两个在窗外 —— 窗口里该落下 3 个点。
            chk("8.4 条上面的点真画出来了: 窗口里 3 个点(不是 0 个 —— 维数配错时就是 0 个, 配置里还看不出来)",
                len(_dots) == 3 and _o8["dotSeries"], (_dots, _o8["nDots"]))
            chk("8.5 每个点都抬在自己那根长条上面(按颜色对上号比), 颜色是泳道色",
                all(d["y"] < _cy_by_color.get(d["fill"], 0) for d in _dots)
                and sorted(set(d["fill"] for d in _dots)) == ["#c084fc", "#e0a83a"], _dots)
            chk("8.6 「本次采集」那条虚线在", _o8["hasMark"] is True, _o8["hasMark"])
            chk("8.7 默认视窗不是整幅(收在最近那段), 也不是零宽",
                all(0 <= v <= 100 for z in _o8["zoom"] for v in z)
                and _o8["zoom"][0][1] - _o8["zoom"][0][0] > 0
                and _o8["zoom"][0][1] - _o8["zoom"][0][0] < 60, _o8["zoom"])
            # 8.8~8.13: 气泡里的话(用户点着气泡提过两回: 「不要把点赞和回复合计了」、
            # 「这块爬到了啥内容直接写出来展示」「不是让你 13 5, 让你写内容」)
            _bt, _dt = _o8["btips"], _o8["dtips"]
            _allb, _alld = "".join(_bt), "".join(_dt)
            _d14 = next((t for t in _dt if "评论甲" in t), "")
            _d15 = next((t for t in _dt if "评论丙" in t), "")
            _dng = next((t for t in _dt if "新帖" in t), "")
            # 正文优先(官号那类行的标题是爬回来时拼的元信息, 正文才是内容), 没正文才退回标题
            chk("8.8 条的气泡把这段**爬到了啥内容**列出来(有正文写正文, 没正文写标题), 不是只报几个数",
                all(k in _allb for k in ("评论甲说了句啥", "评论乙", "评论丙", "新帖")), _bt)
            chk("8.9 被答案引用过的那条在气泡里标出来, 尾巴上如实说共几条",
                ("[引用]" in _allb) and ("这条的正文该出现在气泡里" in _allb)
                and ("共 4 条，其中 1 条被答案引用" in _allb), _bt)
            chk("8.10 点的气泡只列**这一天这一个来源**的(09-14 那个点不含 09-15 的评论丙)",
                ("评论丙" not in _d14) and ("评论甲" in _d14) and ("评论乙" in _d14), _d14)
            chk("8.11 点赞与回复分开写, 不再合成一个数",
                ("点赞 14" in _d14) and ("回复 1" in _d14) and ("合计" not in _alld), _d14)
            chk("8.12 没有的那个指标不硬写(回复 0 的不写回复; 论坛帖两个都没有就不写这行)",
                ("点赞 3" in _d15) and ("回复" not in _d15)
                and ("点赞" not in _dng) and ("回复" not in _dng), (_d15, _dng))
            chk("8.13 整份页面里不再有「点赞+回复」这种合计数", "点赞+回复" not in js, "")
            # 用户原话:「滑动的时候动画不流畅啊，点追不上条」。长条是自定义图形(重画即当场到位),
            # 散点是普通系列(更新默认补间 300ms) —— 两边不同步就是"点追不上条"。这里钉住"更新不补间"。
            chk("8.14 拖滑块/滚轮缩放时不做补间(两边才同步; 关掉的是**更新**动画, 首次画出来那下照旧)",
                _o8.get("animUpd") == 0, _o8.get("animUpd"))
            # 用户原话:「为啥有这种东西？？？」—— 截图里那条点开只有一行「08/21 [引用] -」。
            # 那个横杠是爬虫在视频没写简介时填的占位符, 不是内容: 得当空, 退回写标题。
            import re as _re8
            _d15_lines = [l.strip() for l in _re8.sub(r"<[^>]*>", "", _d15).split("\n")]
            chk("8.15 正文是「-」这种占位符时不写进气泡(退回写标题), 气泡里不出现光秃秃一条横杠",
                ("评论丙" in _d15) and not any(l.endswith("-") for l in _d15_lines), _d15_lines)

print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !!", f)
sys.exit(1 if fails else 0)
