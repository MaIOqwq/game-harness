#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「删除一个对话」的自检(纯离线: 临时库 + 临时目录, 不碰产品库、不起服务)。

口径(2026-09-16 用户定「可以 7 天恢复，在设置加个垃圾桶」): 删 ≠ 立刻抹掉, 分两步 ——
  ① 删的当下: 整段**搬进垃圾桶**, 活的那一侧(checkpoint/session_claim/流水)当场清空;
  ② 过了 7 天: 垃圾桶里那行才由 sweep 真删。中间反悔可以整段搬回去。

所以这里盯三头:
  A. 搬走了没有 —— 活的那侧一样不留(留一样, 下次进这个会话就"又活过来");
  B. 搬回来的东西一样不少 —— 状态卡原文、结论日志、流水全文, 逐样比对;
  C. 不该动的**一行都没动** —— 证据池(crawl_run/observation)与 L4 注册表(alias_resolution),
     以及别的会话。多删一下, 就把别人的答案、别人的 [id=N] 一起毁了。

删会话记忆是全项目**唯一**允许出现删除原语的地方(guard.py 逐名放行), 故最后一节专门盯这道口子。

用法: python _syscheck/verify_session_forget.py   退出码 0=全过, 1=有失败。
"""
import json
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HARNESS = os.path.join(ROOT, "harness")
sys.path.insert(0, ROOT)

os.environ["HARNESS_FLOW"] = "1"          # 离线闸可能把流水关了; 这一份要真落盘才验得了"搬走了没"

from harness import db, flow, forget, guard   # noqa: E402

fails = []
total = ok = 0

DAY = 86400
CARD = '{"turns": 3, "claims": ["M1"], "note": "这张卡是原文，恢复后必须一字不差"}'


def chk(name, cond, got=""):
    global total, ok
    total += 1
    print(("PASS" if cond else "FAIL"), name)
    if cond:
        ok += 1
    else:
        fails.append("%s -> %r" % (name, got))


def one(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


def seed(conn, sc):
    """给一个会话造一份完整记忆: 一张状态卡 + 两条结论 + 一段流水。"""
    conn.execute("INSERT INTO checkpoint(scope,card,saved_at) VALUES(?,?,?)", (sc, CARD, 1))
    conn.execute("INSERT INTO session_claim(cid,q,concl,ev,moved_at,topic,scope) "
                 "VALUES(?,?,?,?,?,?,?)", ("%s::M1" % sc, "问一", "答一", "[id=7]", 11, "话题甲", sc))
    conn.execute("INSERT INTO session_claim(cid,q,concl,ev,moved_at,topic,scope) "
                 "VALUES(?,?,?,?,?,?,?)", ("%s::M2" % sc, "问二", "答二", "", 12, "话题乙", sc))
    conn.commit()
    flow.append(sc, {"question": "鸣潮最近社区里最大的节奏是什么？", "answer": "……[id=7]……"})


tmp = tempfile.mkdtemp(prefix="hs_forget_")
fdir = os.path.join(tmp, "flow")
_orig_flowdir = flow._FLOW_DIR
try:
    flow._FLOW_DIR = fdir
    conn = db.connect(os.path.join(tmp, "t.db"))
    db.init(conn)

    # 两个会话各自的记忆 + 一份这个会话爬来的证据(证据挂在 web:aaa 名下, 但池子是共享的)
    seed(conn, "web:aaa")
    seed(conn, "web:bbb")
    conn.execute("INSERT INTO crawl_run(query,game_key,platform,row_count,scope) VALUES(?,?,?,?,?)",
                 ("鸣潮", "鸣潮", "nga", 1, "web:aaa"))
    rid = one(conn, "SELECT last_insert_rowid()")
    conn.execute("INSERT INTO observation(crawl_id,game_key,platform,text,dedup_key,scope) "
                 "VALUES(?,?,?,?,?,?)", (rid, "鸣潮", "nga", "原帖正文", "pool|1", "web:aaa"))
    conn.execute("INSERT INTO alias_resolution(alias,game_id,valid_from) VALUES(?,?,?)",
                 ("mc", 1, "2026-01-01"))
    conn.commit()
    aaa_flow, bbb_flow = flow.fpath("web:aaa", fdir), flow.fpath("web:bbb", fdir)
    bbb_flow_text = open(bbb_flow, encoding="utf-8").read()
    aaa_flow_text = open(aaa_flow, encoding="utf-8").read()

    # ---------------- ① 删 = 整段搬进垃圾桶 ----------------
    now = int(__import__("time").time())
    st = forget.trash_move(conn, "web:aaa", title="鸣潮最大的节奏", flow_dir=fdir, now=now)

    chk("①-1 如实报出各层搬走多少",
        st["cleared"] == {"checkpoint": 1, "session_claim": 2, "flow_file": 1}, st)
    chk("①-2 过期时刻 = 删的时刻 + 7 天(前台的承诺就是这个数)",
        st["expire_at"] == now + 7 * DAY and forget.TRASH_DAYS == 7, st)
    chk("①-3 检查点搬走了(不是存空行那种软删)",
        one(conn, "SELECT COUNT(*) FROM checkpoint WHERE scope=?", ("web:aaa",)) == 0, "")
    chk("①-4 结论日志搬走了(按 scope 列 / cid 前缀都查不到)",
        one(conn, "SELECT COUNT(*) FROM session_claim WHERE scope=?", ("web:aaa",)) == 0
        and one(conn, "SELECT COUNT(*) FROM session_claim WHERE cid LIKE ?", ("web:aaa::%",)) == 0, "")
    chk("①-5 流水文件搬走了(留在原地的话, 恢复时会把新的和旧的搅在一起)",
        not os.path.isfile(aaa_flow), aaa_flow)
    chk("①-6 别的会话一行没动(检查点/结论/流水都还在, 流水逐字没变)",
        one(conn, "SELECT COUNT(*) FROM checkpoint WHERE scope=?", ("web:bbb",)) == 1
        and one(conn, "SELECT COUNT(*) FROM session_claim WHERE scope=?", ("web:bbb",)) == 2
        and os.path.isfile(bbb_flow)
        and open(bbb_flow, encoding="utf-8").read() == bbb_flow_text, "")
    chk("①-7 **证据池一行没动**(连这个会话自己爬的那些也在): 历史答案的 [id=N] 还得核得回原帖",
        one(conn, "SELECT COUNT(*) FROM crawl_run") == 1
        and one(conn, "SELECT COUNT(*) FROM observation") == 1
        and one(conn, "SELECT COUNT(*) FROM observation WHERE scope=?", ("web:aaa",)) == 1, "")
    chk("①-8 L4 注册表一行没动(那是全机共享的黑话账本, 不归某个会话)",
        one(conn, "SELECT COUNT(*) FROM alias_resolution") == 1, "")

    tr = forget.trash_list(conn, now=now)
    chk("①-9 垃圾桶里正好这一条, 标题认得出, 还剩 7 天",
        len(tr) == 1 and tr[0]["scope"] == "web:aaa"
        and tr[0]["title"] == "鸣潮最大的节奏" and tr[0]["days_left"] == 7, tr)
    blob = conn.execute("SELECT card,claims,flow FROM trash WHERE scope=?", ("web:aaa",)).fetchone()
    chk("①-10 捡得回来的东西一样不少: 状态卡原文 + 两条结论 + 流水全文",
        blob["card"] == CARD and len(json.loads(blob["claims"])) == 2
        and blob["flow"] == aaa_flow_text, (blob["card"], blob["flow"] and len(blob["flow"])))

    empty_scope_raised = False
    try:
        forget.trash_move(conn, "   ")
    except ValueError:
        empty_scope_raised = True
    chk("①-11 空 scope 直接抛错(不许拿空串当成'全删')", empty_scope_raised, "")

    st2 = forget.trash_move(conn, "web:never-existed", flow_dir=fdir, now=now)
    chk("①-12 本来就没东西的会话: 一个 0, 且不往垃圾桶塞空条目(不然垃圾桶里全是没印象的行)",
        st2["cleared"] == {"checkpoint": 0, "session_claim": 0, "flow_file": 0}
        and st2["expire_at"] is None and len(forget.trash_list(conn, now=now)) == 1, st2)

    bare = db.connect(os.path.join(tmp, "bare.db"))    # 连表都还没建(未 init)的库
    st3 = forget.trash_move(bare, "web:aaa", flow_dir=fdir, now=now)
    chk("①-13 库还没建表: 全 0, 不炸主链",
        st3["cleared"]["checkpoint"] == 0 and st3["expire_at"] is None, st3)
    bare.close()

    # ---------------- ② 7 天内捡回来 ----------------
    back = forget.trash_restore(conn, "web:aaa", flow_dir=fdir)
    chk("②-1 恢复如实报出各层放回多少, 标题也带回来(侧栏那行得认得出来)",
        back["checkpoint"] == 1 and back["session_claim"] == 2 and back["flow_file"] == 1
        and back["title"] == "鸣潮最大的节奏", back)
    chk("②-2 状态卡**原文**回来了(不是重新生成一张空的)",
        one(conn, "SELECT card FROM checkpoint WHERE scope=?", ("web:aaa",)) == CARD, "")
    rows = conn.execute("SELECT cid,q,concl,topic,scope FROM session_claim WHERE scope=? ORDER BY cid",
                        ("web:aaa",)).fetchall()
    chk("②-3 两条结论都回来了, 内容与 scope 列都对",
        len(rows) == 2 and [r["cid"] for r in rows] == ["web:aaa::M1", "web:aaa::M2"]
        and rows[0]["concl"] == "答一" and rows[1]["topic"] == "话题乙"
        and all(r["scope"] == "web:aaa" for r in rows), [dict(r) for r in rows])
    chk("②-4 流水**逐字**回来了(答案全文还在, 不是只剩个壳)",
        os.path.isfile(aaa_flow) and open(aaa_flow, encoding="utf-8").read() == aaa_flow_text, "")
    chk("②-5 垃圾桶里那条没了(搬回去就不该再占着)",
        len(forget.trash_list(conn, now=now)) == 0, forget.trash_list(conn, now=now))
    chk("②-6 恢复不动别的会话、不动证据池",
        one(conn, "SELECT COUNT(*) FROM checkpoint WHERE scope=?", ("web:bbb",)) == 1
        and one(conn, "SELECT COUNT(*) FROM observation") == 1, "")
    chk("②-7 恢复不在垃圾桶里的会话 -> None(前端据此回 404, 而不是假装成功)",
        forget.trash_restore(conn, "web:never-existed", flow_dir=fdir) is None, "")

    # ---------------- ③ 过了 7 天才真删 ----------------
    t0 = 1700000000
    forget.trash_move(conn, "web:bbb", title="会被扫掉的", flow_dir=fdir, now=t0)
    chk("③-1 没到期: sweep 一条都不碰",
        forget.sweep(conn, now=t0 + 7 * DAY - 60) == 0
        and len(forget.trash_list(conn, now=t0 + 7 * DAY - 60)) == 1, "")
    chk("③-2 还剩 2 小时也报「1 天」(向上取整; 报 0 天会让人以为还能躺一天)",
        forget.trash_list(conn, now=t0 + 7 * DAY - 7200)[0]["days_left"] == 1,
        forget.trash_list(conn, now=t0 + 7 * DAY - 7200))
    chk("③-3 到点: sweep 扫掉并如实报几条",
        forget.sweep(conn, now=t0 + 7 * DAY + 1) == 1, "")
    chk("③-4 扫掉之后**垃圾桶里彻底没有这一行**(过期=物理清除, 到这一步才找不回来)",
        one(conn, "SELECT COUNT(*) FROM trash WHERE scope=?", ("web:bbb",)) == 0
        and forget.trash_restore(conn, "web:bbb", flow_dir=fdir) is None, "")
    seed(conn, "web:bbb")                  # 上一轮已经把 bbb 搬空扫掉了, 重新造一份再删一次
    chk("③-5 扫的那一下只动过期的那条(没过期的照留)",
        forget.trash_move(conn, "web:bbb", title="留着的", flow_dir=fdir, now=t0 + 6 * DAY)["expire_at"]
        == t0 + 13 * DAY
        and forget.sweep(conn, now=t0 + 7 * DAY + 1) == 0
        and len(forget.trash_list(conn, now=t0 + 7 * DAY + 1)) == 1, "")
    chk("③-6 删除时刻新的排前面(垃圾桶是个列表, 顺序得是最近删的在最上)",
        [x["scope"] for x in forget.trash_list(conn, now=t0 + 7 * DAY + 1)] == ["web:bbb"], "")

    # ---------------- ④ 守卫: 只放行 forget.py 一个 ----------------
    chk("④-1 放行名单就是 forget.py 这一个(没被悄悄扩大)",
        tuple(guard._PURGE_ALLOWED) == ("forget.py",), guard._PURGE_ALLOWED)
    live = guard.check_harness(HARNESS)
    chk("④-2 真产品树扫一遍零违规(放行确实生效了, 不是靠漏扫)", live == [], live[:3])
    _src = 'cur.execute("DELETE FROM observation WHERE id=1")\n'
    chk("④-3 守卫本身没被改瞎(删除原语照样认得出来)", len(guard.scan_source(_src)) == 1, guard.scan_source(_src))
    d2 = tempfile.mkdtemp(prefix="hs_guard_")
    for _fn in ("forget.py", "other.py"):
        with open(os.path.join(d2, _fn), "w", encoding="utf-8") as f:
            f.write(_src)
    got = guard.check_harness(d2)
    chk("④-4 放行是**认文件名**不是认内容: 同一段删除代码, 叫 forget.py 放行、叫别的名字照拦",
        len(got) == 1 and "other.py" in got[0], got)

    # ---------------- ⑤ 网页后端 /api/forget + /api/trash + /api/restore 的接线 ----------------
    import harness.webapp as W                                       # noqa: E402

    class FakeConn:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeDB:
        def __init__(self):
            self.made = []

        def connect(self, *a, **k):
            c = FakeConn()
            self.made.append(c)
            return c

    class FakeForget:
        TRASH_DAYS = 7

        def __init__(self):
            self.calls = []
            self.restored = []

        def trash_move(self, conn, scope, title=None, flow_dir=None):
            self.calls.append((conn, scope, title))
            return {"cleared": {"checkpoint": 1, "session_claim": 2, "flow_file": 1},
                    "expire_at": 1234}

        def trash_restore(self, conn, scope, flow_dir=None):
            self.restored.append((conn, scope))
            if scope.endswith("gone"):
                return None
            return {"checkpoint": 1, "session_claim": 2, "flow_file": 1, "title": "旧标题"}

    class FakeSess:
        def __init__(self):
            self.conn = FakeConn()

    class FakeH:
        def __init__(self):
            self.code = None
            self.obj = None

        def _json(self, code, obj):
            self.code = code
            self.obj = obj

    _wa = open(os.path.join(HARNESS, "webapp.py"), encoding="utf-8").read()
    chk("⑤-1 三条路由都在(删/看垃圾桶/恢复)",
        'u.path == "/api/forget"' in _wa and 'u.path == "/api/trash"' in _wa
        and 'u.path == "/api/restore"' in _wa and "def _trash" in _wa and "def _restore" in _wa, "")
    chk("⑤-2 删的时候把标题一起送进去(垃圾桶那行要靠它认人)",
        '_forget(b.get("session") or "anon", b.get("title"))' in _wa, "")
    chk("⑤-3 webapp 自己不写删除语句(删除只在 forget.py 那一处)",
        guard.scan_source(_wa, path="webapp.py") == [], guard.scan_source(_wa, path="webapp.py")[:3])
    chk("⑤-4 起服务时先扫一遍过期的(不然过期的永远躺在垃圾桶里)",
        "_startup_sweep()" in _wa and "forget.sweep(conn)" in _wa, "")
    chk("⑤-5 列垃圾桶之前也扫一遍(服务连着开好几天, 光靠启动那一下不够)",
        'u.path == "/api/trash"' in _wa
        and __import__("re").search(r"def _trash[\s\S]{0,400}forget\.sweep\(conn\)", _wa) is not None, "")

    W.db, W.forget = FakeDB(), FakeForget()
    W._SESSIONS.clear()
    W._LOCKS.clear()
    busy = FakeSess()
    W._SESSIONS["web:busy"] = busy
    lk = W._session_lock("web:busy")
    lk.acquire()
    try:
        h = FakeH()
        W.Handler._forget(h, "busy", "标题")
        chk("⑤-6 有题正在跑 -> 409 拒删, 且**一行没搬**、缓存里那个会话也没被抽走",
            h.code == 409 and W.forget.calls == [] and W._SESSIONS.get("web:busy") is busy,
            (h.code, W.forget.calls, list(W._SESSIONS)))
    finally:
        lk.release()

    h = FakeH()
    W.Handler._forget(h, "busy", "这个对话的标题")
    chk("⑤-7 不在跑了 -> 删成功, 各层搬走多少如实回给前台, 并带上保留天数",
        h.code == 200 and h.obj["ok"] is True and h.obj["cleared"]["session_claim"] == 2
        and h.obj["days"] == 7 and h.obj["expire_at"] == 1234, h.obj)
    chk("⑤-8 标题原样传给了搬库那一层",
        W.forget.calls[-1][2] == "这个对话的标题", W.forget.calls[-1])
    chk("⑤-9 内存里缓存的那个 Session 被丢掉了(不丢的话下次进来还接着旧卡答, 库里搬空了也白搬)",
        "web:busy" not in W._SESSIONS, list(W._SESSIONS))
    chk("⑤-10 借的是那个会话自己的连接, 清完关掉(不留一条漏掉的连接)",
        W.forget.calls[-1][0] is busy.conn and busy.conn.closed, "")
    chk("⑤-11 传给清库的是 web:<sid> 这个 scope, 不是裸 sid",
        W.forget.calls[-1][1] == "web:busy", W.forget.calls[-1][1])
    chk("⑤-12 清完这个 scope 的锁放开了(否则这个对话永远不能再问)",
        lk.acquire(blocking=False) is True, "")
    lk.release()

    W.forget.calls = []
    before = len(W.db.made)
    h = FakeH()
    W.Handler._forget(h, "never-seen", None)
    chk("⑤-13 服务端从没见过的对话: 现开一条连接照搬(不因为没缓存就跳过)",
        h.code == 200 and len(W.db.made) == before + 1 and W.db.made[-1].closed,
        (h.code, len(W.db.made)))
    chk("⑤-14 而且没把这条临时连接塞进会话缓存(空 scope 不该被写进记忆库)",
        "web:never-seen" not in W._SESSIONS, list(W._SESSIONS))

    # ---- 恢复那条链: 最容易漏的是"恢复完不丢缓存" —— 那不叫恢复, 只是库里多了几行 ----
    W.forget.restored = []
    W._SESSIONS.clear()
    live2 = FakeSess()
    W._SESSIONS["web:comeback"] = live2                 # 开会话时建的**空卡**就挂在这上面
    h = FakeH()
    W.Handler._restore(h, "comeback")
    chk("⑤-15 恢复成功: 各层放回多少如实回给前台",
        h.code == 200 and h.obj["ok"] is True and h.obj["restored"]["flow_file"] == 1, h.obj)
    chk("⑤-16 **恢复后把缓存里的 Session 也丢了** —— 不丢的话下次进来接着那张空卡答, 恢复等于白做",
        "web:comeback" not in W._SESSIONS, list(W._SESSIONS))
    chk("⑤-17 恢复借的也是那个会话自己的连接, 用完关掉",
        W.forget.restored[-1][0] is live2.conn and live2.conn.closed, "")
    chk("⑤-18 恢复传的同样是 web:<sid>",
        W.forget.restored[-1][1] == "web:comeback", W.forget.restored[-1][1])

    lk2 = W._session_lock("web:comeback")
    lk2.acquire()
    try:
        h = FakeH()
        W.Handler._restore(h, "comeback")
        chk("⑤-19 有题正在跑时不许恢复(否则它收口落的卡会跟搬回来那张打架)",
            h.code == 409 and h.obj["ok"] is False, h.obj)
    finally:
        lk2.release()

    bystander = FakeSess()
    W._SESSIONS["web:bystander"] = bystander
    h = FakeH()
    W.Handler._restore(h, "gone")          # 垃圾桶里没有它(过期被清掉了 / 从没删过)
    chk("⑤-20 垃圾桶里没有这个对话 -> 404, 不假装恢复成功",
        h.code == 404 and h.obj["ok"] is False, h.obj)
    chk("⑤-21 恢复只碰自己那一个会话, 别家的缓存原封不动",
        W._SESSIONS.get("web:bystander") is bystander and "web:gone" not in W._SESSIONS,
        list(W._SESSIONS))
finally:
    flow._FLOW_DIR = _orig_flowdir
    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass

print("PASS=%d FAIL=%d" % (ok, len(fails)))
for f in fails:
    print("  !!", f)
sys.exit(1 if fails else 0)
