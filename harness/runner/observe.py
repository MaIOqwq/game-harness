# -*- coding: utf-8 -*-
"""calibrate 跑批中间态观察器(§10.8 复盘工具, 只读): 在 big run 进行中盯逐题进度,
不碰进程 —— 直接读跑批 DB(每题独立 scope=exp:calib:<tag>:qN), 判该题:
  已收口   session_claim 已有该 scope 结论(那题跑完翻篇);
  采集中   有 obs/run 但还没结论、且最近还在写;
  疑卡?   有 obs/run 却没结论、且静默超阈值(炸题/风控长停的判据, 静默阈值=interval+900s);
  未起步   连 scope 都没有(还没 set_scope 到它) —— 观察器只列已出现的 scope。

用法:
  python -m harness.runner.observe --db <sqlite> [--tag <run_tag>] [--loop <秒>]
  --loop 重复快照直到 Ctrl-C(盯 live 用); 缺省单次快照。只读, 打开即连即退。"""
import argparse
import datetime
import os
import re
import sqlite3
import sys
import time


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _plat(p):
    """库内平台名不统一(NGA/bilibili/nga/bili 等), 归一成 nga/bili/other 三桶。"""
    pl = (p or "").strip().lower()
    if pl in ("nga",):
        return "nga"
    if pl in ("bili", "bilibili"):
        return "bili"
    return pl or "other"


def _iso(v):
    """观察时间/crawled_at 是 ISO 字符串; session_claim.moved_at 是 epoch int。统一成 localtime datetime。"""
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.datetime.fromtimestamp(float(v))
    s = str(v)
    if re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s):
        try:
            return datetime.datetime.fromisoformat(s[:19])
        except Exception:
            return None
    return None


def _fresh(now):
    return now.strftime("%H:%M:%S")


def collect(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    scopes = set(r[0] for r in conn.execute("SELECT DISTINCT scope FROM observation"))
    scopes |= set(r[0] for r in conn.execute("SELECT DISTINCT scope FROM crawl_run"))
    scopes |= set(r[0] for r in conn.execute("SELECT DISTINCT scope FROM session_claim"))
    out = {}
    for scope in scopes:
        m = re.match(r"^exp:calib:(?P<tag>[^:]+):q(?P<n>\d+)$", scope or "")
        if not m:
            continue
        plat_obs = {}
        for r in conn.execute("SELECT platform, COUNT(*) c FROM observation WHERE scope=? GROUP BY platform", (scope,)):
            plat_obs[_plat(r["platform"])] = plat_obs.get(_plat(r["platform"]), 0) + r["c"]
        run_p = {_plat(r["platform"]): r["c"] for r in conn.execute(
            "SELECT platform, COUNT(*) c FROM crawl_run WHERE scope=? GROUP BY platform", (scope,))}
        claims = conn.execute(
            "SELECT concl, moved_at FROM session_claim WHERE scope=? ORDER BY moved_at", (scope,)).fetchall()
        last_cl = dict(claims[-1]) if claims else None
        last_act = _iso(conn.execute(
            "SELECT MAX(crawled_at) v FROM observation WHERE scope=?", (scope,)).fetchone()["v"])
        fa = _iso(conn.execute(
            "SELECT MAX(finished_at) v FROM crawl_run WHERE scope=?", (scope,)).fetchone()["v"])
        if fa and (not last_act or fa > last_act):
            last_act = fa
        mt = _iso(last_cl["moved_at"]) if last_cl else None
        if mt and (not last_act or mt > last_act):
            last_act = mt
        out.setdefault(m["tag"], []).append({
            "n": int(m["n"]), "scope": scope, "plat_obs": plat_obs, "run_p": run_p,
            "n_obs": sum(plat_obs.values()), "n_run": sum(run_p.values()),
            "concl": (last_cl or {}).get("concl") or "", "last_act": last_act})
    conn.close()
    for tag in out:
        out[tag].sort(key=lambda x: x["n"])
    return out


def show(path, tag, interval):
    now = datetime.datetime.now()
    groups = collect(path)
    if tag:
        groups = {tag: g for t, g in groups.items() if t == tag}
    if not groups:
        print("%s (db=%s) 无 exp:calib scope" % (_fresh(now), path))
        return
    for t, items in sorted(groups.items()):
        print("== tag=%s  n=%d  快照=%s ==" % (t, len(items), _fresh(now)))
        print("%-4s %-6s %-36s %6s %5s %5s %5s %5s %-7s %s" % (
            "#", "state", "scope", "obs", "run", "nga", "bili", "oth", "last_act", "concl"))
        for it in items:
            age = (now - it["last_act"]).total_seconds() if it["last_act"] else None
            if it["concl"]:
                st = "收口"
            elif age is None:
                st = "起步?"
            elif age < interval + 900:
                st = "采集中"
            else:
                st = "疑卡!"
            la = it["last_act"].strftime("%H:%M") if it["last_act"] else "-"
            oth = it["n_obs"] - it["plat_obs"].get("nga", 0) - it["plat_obs"].get("bili", 0)
            concl = it["concl"].strip().replace("\n", " ") or "(未收口)"
            print("%-4s %-6s %-36s %6d %5d %5d %5d %5d %-7s %s" % (
                "q%d" % it["n"], st, it["scope"], it["n_obs"], it["n_run"],
                it["plat_obs"].get("nga", 0), it["plat_obs"].get("bili", 0), oth,
                la, concl[:60]))


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(description="calibrate 中间态观察器(只读)")
    ap.add_argument("--db", default=os.path.join("harness", "data", "harness_dev.db"))
    ap.add_argument("--tag", default=None, help="只看某 run tag")
    ap.add_argument("--interval", type=int, default=300, help="题间间隔秒(判静默阈值, 默认300)")
    ap.add_argument("--loop", type=int, default=0, help="重复快照间隔秒(0=单次)")
    args = ap.parse_args(argv)
    while True:
        show(args.db, args.tag, args.interval)
        if args.loop <= 0:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
