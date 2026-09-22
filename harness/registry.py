# -*- coding: utf-8 -*-
"""L4 别名注册表(账本式, supersede-not-overwrite)。

黑话/别称 -> 规范游戏名的版本化注册表。**只 INSERT + SELECT, 不改旧行**:
新增版本 = 追加一条新行, "生效" = 取该 alias 最新(id 最大)行 —— 旧行留在账本里不 overwrite
(与数据层只增契约一致, guard 放行; 不用 UPDATE valid_until 关旧, 语义由 id 序替代)。

表结构 schema.sql: alias_resolution(id PK, alias, game_id, confidence, hits, source, valid_from, valid_until)。
用法:
  seed_builtin(conn)            会话层建库后播种内置黑话(幂等: 同 alias 同 game 同 source 不重复插)
  resolve(conn, alias)          -> {alias, game, confidence, source} | None(取最新行)
  find_in_text(conn, text)      -> 问句里命中的别名列表(长词优先, 去重), 供引擎给模型提示
表不存在(直接 Engine + :memory: 未 init)时一律安全回落空, 不炸主链。
"""
import sqlite3

# 规范游戏维度: 表里只存 game_id, 名在此解析。
# game_id **显式登记在下面的 GAME_IDS 里, 与列表顺序无关**(09-22 修, 来龙去脉见 _game_id 注释)。
# 新增游戏 = 在 GAME_IDS 末尾追加一行、给一个没被占过的号(CANONICAL 自动跟着长)。
GAME_IDS = {
    "明日方舟": 1,
    "明日方舟终末地": 2,
    "崩坏三": 3,
    "崩坏星穹铁道": 4,
    "绝区零": 5,
    "原神": 6,
    "鸣潮": 7,
    "王者荣耀": 8,
    "金铲铲之战": 9,
}
CANONICAL = tuple(GAME_IDS)               # 展示/遍历用; 顺序 = 登记顺序, 改顺序不影响 game_id
_ID_TO_GAME = {gid: name for name, gid in GAME_IDS.items()}

# 同 IP 的兄弟作(本家 vs 衍生作): 检索/归档里最容易互相污染的一类 —— 两边玩家在同一批版块
# 互聊, 关键词命中后样本会被当成"目标游戏的社区声音"。跨 IP 的横向对比(鸣潮 vs 原神)是合法提问,
# 不在此列, 别误伤。
SIBLINGS = {
    "明日方舟": ("明日方舟终末地",),
    "明日方舟终末地": ("明日方舟",),
}

# 内置黑话(源 = 用户口头例: 三蹦子=崩坏三, 终末地=明日方舟终末地; live 常被问 星铁/崩铁=崩坏星穹铁道)
BUILTIN = (
    ("三蹦子", "崩坏三"),
    ("三崩子", "崩坏三"),
    ("崩三", "崩坏三"),
    ("终末地", "明日方舟终末地"),
    ("星铁", "崩坏星穹铁道"),
    ("崩铁", "崩坏星穹铁道"),
)
_BUILTIN_SOURCE = "builtin-20260909"


def _game_id(game):
    """游戏名 -> 稳定 game_id; 不在册返回 None。

    09-22 修: 原为 CANONICAL.index(game) + 1 —— 按**下标**推导 id。往游戏列表中间插一个新游戏时,
    后面所有游戏的下标整体后移, 老库里已存的 game_id 就会**静默指错游戏**(不报错, 只是答案对错游戏)。
    改成查显式表 GAME_IDS: id 一经定下, 再也不随插入/重排变动。
    兼容: GAME_IDS 里的号取的正是修前的 index+1, 所以**老库里已存的行照旧解得对**, 不用改库。"""
    return GAME_IDS.get(game)


def _name(game_id):
    return _ID_TO_GAME.get(game_id)


def _newest(conn, alias):
    try:
        return conn.execute("SELECT id, game_id, source FROM alias_resolution "
                            "WHERE alias=? ORDER BY id DESC LIMIT 1", (alias,)).fetchone()
    except sqlite3.OperationalError:
        return None


def seed(conn, pairs, source=_BUILTIN_SOURCE, now=None):
    """播种 (alias, game) 列表: 同 alias 最新行已是同 game 同 source 则跳过(幂等不刷账本)。"""
    import time
    now = now or time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for alias, game in pairs:
        gid = _game_id(game)
        if not gid:
            continue
        cur = _newest(conn, alias)
        if cur and cur["game_id"] == gid and cur["source"] == source:
            continue
        try:
            conn.execute(
                "INSERT INTO alias_resolution (alias, game_id, confidence, hits, source, valid_from) "
                "VALUES (?,?,?,?,?,?)", (alias, gid, 0.9, 0, source, now))
        except sqlite3.OperationalError:
            return n  # 表不存在(未 init): 本次不插, 下次建表后再来
        n += 1
    conn.commit()
    return n


def seed_builtin(conn):
    return seed(conn, BUILTIN)


def resolve(conn, alias):
    """取该 alias 最新(id 最大)生效行 -> {alias, game, confidence, source} | None。"""
    try:
        row = conn.execute(
            "SELECT alias, game_id, confidence, hits, source FROM alias_resolution "
            "WHERE alias=? ORDER BY id DESC LIMIT 1", (alias,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    game = _name(row["game_id"])
    if not game:
        return None
    return {"alias": row["alias"], "game": game, "confidence": row["confidence"],
            "hits": row["hits"], "source": row["source"]}


# 注(2026-09-17): 曾有 aliases_of(按 hits 排序取某游戏的别名)供 B站 检索词扩词用, 实测后撤掉 ——
# 别名当检索词会撞同名异物(「三蹦子」在 B站 指三轮车, 搜回来一堆车视频), 检索这条线只发本体名。
# 注册表现只服务"认出问句在说哪个游戏"(find_in_text)与兄弟作防火墙(sibling_terms)。


def sibling_terms(conn, game):
    """game 的兄弟作的"外表名"(规范名 + 注册表别名), 供识别检索词/样本是否跑到兄弟作去了。

    关键过滤: 丢掉本身是 game 名字子串的术语 —— 这样钉死"明日方舟终末地"时不会拿"明日方舟"
    去误伤(终末地帖子里提本家是正常的), 而钉死"明日方舟"时"终末地"仍进表(这正是要拦的污染)。
    无兄弟/无表 -> []。返回长词优先, 便于"先匹配更具体的"。"""
    sibs = SIBLINGS.get(game or "")
    if not sibs:
        return []
    terms = set()
    for s in sibs:
        if s and s not in (game or ""):
            terms.add(s)
    try:
        rows = conn.execute("SELECT DISTINCT alias, game_id FROM alias_resolution").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for alias, gid in rows:
        if _name(gid) in sibs and alias and alias not in (game or ""):
            terms.add(alias)
    return sorted(terms, key=lambda t: (-len(t), t))


def find_in_text(conn, text):
    """问句里命中哪些黑话(注册表里全量 alias 扫子串, 长词优先, 去重); 无表/无命中返回 []。"""
    try:
        rows = conn.execute("SELECT DISTINCT alias FROM alias_resolution").fetchall()
    except sqlite3.OperationalError:
        return []
    text = text or ""
    hits, seen = [], set()
    for (alias,) in rows:
        if not alias or alias not in text or alias in seen:
            continue
        r = resolve(conn, alias)
        if r and r["game"] and r["game"] not in seen:
            hits.append(r)
            seen.add(r["game"])
    hits.sort(key=lambda r: (-len(r["alias"]), r["alias"]))
    return hits
