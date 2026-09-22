# -*- coding: utf-8 -*-
"""live 标定脚本: 复用会话层(Session)驱动 N 题真爬, 验门控/护栏/冷却/平台占比在真实网络成立。
这是 runner/会话层的第一个真实消费方, 也验证"题与题间隔"对 B站风控的保护。

用法:
  python -m harness.runner.calibrate [--interval 360] [--max-q N] [--start i]
                                     [--qsfile 题单.tsv] [--db path] [--report 报告]
                                     [--tag 运行标] [--mock] [--nga-ratio 50]
  --interval 两题间隔秒(默认 360; live 建议 300-600, 别低于引擎设计的 BILI_GAP 语义)
  --qsfile  题单 TSV: game<TAB>mode<TAB>question(支持 # 注释); 缺省用内置小题单
  --tag     本次运行的 scope tag(默认时间戳); 每题独立 scope = exp:calib:<tag>:q<N> => 逐题记忆隔离
  --mock    用 mock provider 只验流程(无需隧道); 缺省 = 真爬, 需本地隧道就绪
  --nga-ratio  NGA 占比% 0-100(默认 50=双平台均等≈现状); 100=只 NGA, 0=只 bili。
              语义 A: 样本证据配比。端点 0/100 硬压另一边, 内部 1..99 软引导(±RATIO_TOL)。
              单进程必跑(双进程并发同隧道曾触发 NGA 429)。

隧道守卫: 起跑前探 18770(NGA)/18771(bili) /health, 全挂则拒绝开跑(避免静默打到假后端)。
报告: 逐题追加(结论行/用时/rounds/token/平台新增/阻塞/翻篇数), 复跑可续。"""
import os
import sys
import time


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


DEFAULT_QS = [
    ("鸣潮", "快速", "最近鸣潮社区在吵什么强度问题"),
    ("王者荣耀", "快速", "孙膑现在还能打中路吗 社区怎么说"),
    ("明日方舟终末地", "精准", "终末地最近的玩法讨论和评价怎么样"),
    ("原神", "精准", "原神这个版本值不值得回坑"),
]


def _health(port, tokf):
    import json
    import urllib.request
    try:
        tok = open(tokf, encoding="utf-8").read().strip()
        req = urllib.request.Request("http://127.0.0.1:%d/health" % port,
                                     headers={"X-Token": tok})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def _load_qs(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            parts = ln.split("\t")
            if len(parts) < 3:
                continue
            game, mode, q = parts[0].strip(), parts[1].strip(), "\t".join(parts[2:]).strip()
            out.append((game, mode if mode in ("快速", "精准") else "快速", q))
    return out


def main(argv=None):
    _utf8()
    import argparse
    ap = argparse.ArgumentParser(description="游戏社区问答 Agent — live 标定(复用会话层)")
    ap.add_argument("--db", default=None, help="SQLite 路径(默认 harness/data/harness_dev.db)")
    ap.add_argument("--qsfile", default=None, help="题单 TSV: game<TAB>mode<TAB>question")
    ap.add_argument("--interval", type=int, default=360, help="两题间隔秒(默认360; live建议300-600)")
    ap.add_argument("--max-q", type=int, default=0, help="最多跑前 N 题(0=全部)")
    ap.add_argument("--start", type=int, default=0, help="从第几题开始(0基, 续跑用)")
    ap.add_argument("--report", default=os.path.join("_tmp", "calib_report.txt"))
    ap.add_argument("--tag", default=None, help="本次 scope tag(默认时间戳): 每题 exp:calib:<tag>:q<N>")
    ap.add_argument("--mock", action="store_true", help="mock provider 只验流程(无需隧道)")
    ap.add_argument("--nga-ratio", type=int, default=50,
                    help="NGA 占比%% 0-100(默认50=均等≈现状); 100=只NGA 0=只bili")
    args = ap.parse_args(argv)
    if not (0 <= args.nga_ratio <= 100):
        print("!! --nga-ratio 须在 0..100(现=%d)" % args.nga_ratio)
        return 2

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not args.mock:
        from harness.sources import live as _live
        if _live.is_local():
            # 装机版: 两个爬虫是**按需拉起**的, 起跑这一刻它们本来就没在跑,
            # 所以老那套"探 18770/18771 隧道、全挂就拒跑"的守卫在这儿不适用(会误拒)。
            # 真连不上时 live 层会自己把爬虫拉起来, 拉不起来就如实报错 —— 不会静默打到假后端。
            print("本地模式: 爬虫按需拉起, 跳过隧道守卫")
        else:
            tok_nga = os.path.join(root, "harness", "data", ".nga_token")
            tok_bili = os.path.join(root, "harness", "data", ".bili_token")
            ok_nga, ok_bili = _health(18770, tok_nga), _health(18771, tok_bili)
            if not (ok_nga or ok_bili):
                print("!! 本地隧道未就绪: 18770(NGA)=%s 18771(bili)=%s" % (ok_nga, ok_bili))
                print("   需先起隧道(18770/18771 -> <YOUR_SERVER_IP>:8770/8771)。--mock 可只验流程。")
                return 2
            print("隧道健康: NGA=%s bili=%s" % (ok_nga, ok_bili))
        os.environ["HARNESS_LIVE"] = "1"

    from harness.runner.session import Session
    qs = _load_qs(args.qsfile) if args.qsfile else DEFAULT_QS
    qs = qs[args.start:]
    if args.max_q > 0:
        qs = qs[:args.max_q]
    if not qs:
        print("题单为空")
        return 1
    print("题数=%d interval=%ss report=%s" % (len(qs), args.interval, args.report))

    s = Session(db_path=args.db, nga_ratio=args.nga_ratio)
    run_tag = args.tag or time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)   # 报告目录可能还不存在
    with open(args.report, "a", encoding="utf-8") as rep:
        def w(line):
            print(line, flush=True)
            rep.write(line + "\n")
            rep.flush()          # 逐行落盘: 中途被杀(手动 Ctrl-C / 杀进程)也不丢已跑题的记录

        w("==== calib %s scope-tag=%s nga_ratio=%d ====" % (time.strftime("%Y-%m-%d %H:%M:%S"), run_tag,
                                                            args.nga_ratio))
        for i, (game, mode, q) in enumerate(qs):
            qno = args.start + i + 1                # 全局题号: 分批跑(--start)时 scope 不撞车
            scope = "exp:calib:%s:q%d" % (run_tag, qno)
            s.set_scope(scope)                     # 每题一套记忆: 本问只见本问爬的/翻篇的, 不串题
            w("--[%d/%d] %s(%s) scope=%s | %s" % (qno, args.start + len(qs), game, mode, scope, q))
            t0 = time.time()
            log_before = s.log_count()
            try:
                r = s.ask_turn(q, mode=mode, game=game)
                dt = int(time.time() - t0)
                pnew, blocked = {}, []
                for rec in r.get("trace", []):
                    if (rec.get("tool") or "").startswith("crawl_"):   # crawl_nga / crawl_bili(旧 crawl_live 同形)
                        if rec.get("blocked"):
                            blocked.append("%s:%s" % (rec.get("platform"),
                                                      str(rec["blocked"])[:36]))
                        else:
                            p = rec.get("platform")
                            pnew[p] = pnew.get(p, 0) + int(rec.get("new", 0))
                w("  用时=%ds rounds=%d in/out=%d/%d | 平台新增=%s | 阻塞=%s | 翻篇=%s(+%d)" % (
                    dt, r["ctx"]["rounds"], r["prompt_tokens"], r["completion_tokens"],
                    pnew or "(无)", "、".join(blocked) if blocked else "-",
                    s.log_count(), s.log_count() - log_before))
                ans = (r.get("answer") or "").strip().splitlines()
                w("  结论行: %s" % ((ans[-1] if ans else "(空)")[:140]))
            except Exception as e:
                w("  !! 题失败: %r" % e)
            if i < len(qs) - 1 and args.interval > 0:
                w("  sleep %ss..." % args.interval)
                time.sleep(args.interval)
    s.flush()          # 收尾: 最后一题结论也提交到它自己的 scope(不留内存)
    print("DONE ->", args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
