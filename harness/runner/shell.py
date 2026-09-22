# -*- coding: utf-8 -*-
"""交互对话壳(demo): python -m harness.runner [--db path] [--live] [--local]
会话逻辑全在 session.Session, 本文件只做 stdin 界面。
缺省 mock provider(断网可跑); --live 走真爬虫(HARNESS_LIVE=1, 需本地隧道 18770/18771);
--local 走本机爬虫(HARNESS_LOCAL=1, 直连 8770/8771, 隐含 --live)。"""
import sys


def _utf8():
    for s in (sys.stdin, sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


HELP = """命令:
  <任意文本>        当作游戏问题提问
  /mode 快速|精准    切换档位(默认快速)
  /game <名>         钉死游戏上下文(自动探测不到时用); /game off 解除
  /correct <cid> <文> 纠正某条活跃结论(原文留痕, 结论 supersede)
  /card             打印 L1 状态卡(本线程未翻篇结论, 含 cid)
  /log              看已翻篇结论数(会话日志)
  /help /quit        帮助 / 退出
提示: 本地缺省 mock 数据源; 要真爬请加 --live 启动(--local 走本机爬虫)。"""


def main(argv=None):
    _utf8()
    import argparse
    import os

    ap = argparse.ArgumentParser(description="游戏社区问答 Agent — 本地对话壳")
    ap.add_argument("--db", default=None, help="SQLite 路径(默认 harness/data/harness_dev.db)")
    ap.add_argument("--live", action="store_true", help="用真爬虫(需本地隧道); 缺省 mock")
    ap.add_argument("--local", action="store_true", help="爬虫跑在本机 8770/8771(隐含 --live); 缺省走远端隧道")
    args = ap.parse_args(argv)

    if args.local:
        os.environ["HARNESS_LOCAL"] = "1"
        args.live = True
    if args.live:
        os.environ["HARNESS_LIVE"] = "1"

    from .session import Session
    s = Session(db_path=args.db)

    print("游戏社区问答 Agent — 对话壳 (mock | mode=快速)  输入 /help 看命令")
    while True:
        try:
            line = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            return 0
        if not line:
            continue
        low = line.lower()
        if low in ("/quit", "/exit"):
            return 0
        if low in ("/help", "/h", "help"):
            print(HELP)
            continue
        if low == "/card":
            print(s.card_text())
            continue
        if low == "/log":
            print("已翻篇结论数:", s.log_count())
            continue
        if low == "/mode" or low.startswith("/mode "):
            arg = line[5:].strip()
            if arg in ("快速", "精准"):
                s.mode = arg
                print("档位 ->", s.mode)
            else:
                print("档位取值: 快速 / 精准")
            continue
        if low == "/game" or low.startswith("/game "):
            arg = line[5:].strip()
            if not arg or arg.lower() in ("off", "none"):
                s.game_hint = None
                print("游戏上下文 -> 自动探测")
            else:
                s.game_hint = arg
                print("游戏上下文 ->", arg)
            continue
        if low == "/correct" or low.startswith("/correct "):
            parts = line.split(None, 2)
            if len(parts) < 3 or not parts[1].strip():
                print("用法: /correct <cid> <新结论文本>   (cid 见上一条回答的元信息或 /card)")
            else:
                ok, msg = s.correct(parts[1], parts[2])
                print(msg if ok else "!! " + msg)
            continue
        try:
            res = s.ask_turn(line)
        except Exception as e:
            print("!! 本轮失败(会话仍在, 可直接继续):", e)
            continue
        print("\n" + (res.get("answer") or "").strip())
        meta = "mode=%s game=%s cid=%s rounds=%s in_tok=%s out_tok=%s 已翻篇=%s" % (
            res.get("mode"), res.get("game_hint") or "-", res.get("claim_cid") or "-",
            res.get("ctx", {}).get("rounds"),
            res.get("prompt_tokens"), res.get("completion_tokens"), s.log_count())
        print("\n[" + meta + "]")
