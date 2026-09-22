# -*- coding: utf-8 -*-
"""bili-demo 一次性扫码登录（headless，自包含）。

为什么不走框架的 BilibiliLogin.login_by_qrcode：B站 首页登录入口已改版——点那个按钮只会弹一个
「立即登录」浮层，框架里那条 xpath `//div[@class='right-entry__outside go-login-btn']//div` 点下去
等不到可点状态（2026-09-12 实测 30s TimeoutError）。改成直接开官方登录页
https://passport.bilibili.com/login —— 它一进来就把二维码渲染成 base64 PNG（.login-scan__qrcode img），
不需要点任何按钮。框架的 login.py 是 vendored 代码，不动它。

流程：
  持久 profile（browser_data/bili_user_data_dir，SAVE_LOGIN_STATE=True）→ 清掉残留死 cookie →
  开登录页 → 存二维码 PNG + 落 qr_ready.flag（内容=第几版码）→ 每秒轮询 cookie 等扫码 →
  码失效就刷新页面重存新码 → 成功后 update_cookies 写回 profile + nav 验 isLogin → 落 qr_done.flag。
只打印状态，绝不打印任何 cookie 值。

用法: cd <bili 爬虫树> && python -u qr_login_once.py
"""
import asyncio
import base64
import io
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # 跟脚本走: 写死部署目录换个机器就失效
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

import config  # noqa: E402

from PIL import Image  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

import tools.utils as U  # noqa: E402
from media_platform.bilibili import BilibiliCrawler  # noqa: E402

QR_PNG = f"{BASE_DIR}/qr_login.png"
FLAG_READY = f"{BASE_DIR}/qr_ready.flag"
FLAG_DONE = f"{BASE_DIR}/qr_done.flag"
LOGIN_PAGE = "https://passport.bilibili.com/login"
QR_SEL = "div.login-scan__qrcode img"
LOGIN_TIMEOUT = int(os.environ.get("QR_LOGIN_TIMEOUT", "300"))  # 等扫码总时长(秒)


def _clean_flags():
    for f in (FLAG_READY, FLAG_DONE):
        if os.path.exists(f):
            os.remove(f)


def _save_qr(data_uri: str, seq: int) -> None:
    """base64 data-URI -> PNG，并写 qr_ready.flag（内容=版本号，外部据此判断要不要重取图）。"""
    data = data_uri.split(",", 1)[1] if "," in data_uri else data_uri
    with Image.open(io.BytesIO(base64.b64decode(data))) as img:
        img.convert("RGB").save(QR_PNG)
    open(FLAG_READY, "w").write(str(seq))
    print("[qr] QR v%d saved -> %s" % (seq, QR_PNG), flush=True)


async def _grab(page):
    """取当前二维码 img 的 src；没有 / 页面正在跳转就返回 None。
    取码绝不能抛：登录页加载完还会自己跳一次，一次导航曾把整个进程带走（2026-09-12 实测）。"""
    try:
        el = await page.query_selector(QR_SEL)
        if not el:
            return None
        return await el.get_attribute("src")
    except Exception:
        return None


async def main() -> None:
    _clean_flags()
    crawler = BilibiliCrawler()
    async with async_playwright() as playwright:
        crawler.browser_context = await crawler.launch_browser(
            playwright.chromium, None, crawler.user_agent, headless=config.HEADLESS)
        await crawler.browser_context.add_init_script(path="libs/stealth.min.js")
        crawler.context_page = await crawler.browser_context.new_page()
        # 先清死 cookie 再开登录页：反过来的话，清 cookie 会让登录页自己跳走，取码当场炸。
        await crawler.browser_context.clear_cookies()
        crawler.bili_client = await crawler.create_bilibili_client(None)
        await crawler.context_page.goto(LOGIN_PAGE, wait_until="load")
        await asyncio.sleep(2)

        page = crawler.context_page
        seq, last_src, waited, missing = 0, None, 0, 0
        while waited < LOGIN_TIMEOUT:
            src = await _grab(page)
            if src and src != last_src:
                seq += 1
                _save_qr(src, seq)
                last_src = src
                missing = 0
            elif src is None:
                missing += 1
            # 扫码后 SESSDATA/DedeUserID 会落到 context cookie 里
            cookies = await crawler.browser_context.cookies()
            names = {c.get("name") for c in cookies}
            if "SESSDATA" in names and "DedeUserID" in names:
                print("[qr] scan detected after %ds, verifying ..." % waited, flush=True)
                await crawler.bili_client.update_cookies(browser_context=crawler.browser_context)
                ok = await crawler.bili_client.pong()
                print("[qr] nav isLogin =", ok, flush=True)
                open(FLAG_DONE, "w").write("1" if ok else "0")
                await crawler.browser_context.close()
                return
            # 码有时效 / 页面自己跳走了：换一张新码，别让用户对着死码扫
            need_reload = False
            if src is None and missing >= 15:
                need_reload = True
            elif waited and waited % 5 == 0:
                try:
                    body = await page.inner_text("body")
                except Exception:
                    body = ""
                if "失效" in body or "过期" in body:
                    need_reload = True
            if need_reload:
                print("[qr] QR gone/expired at %ds, reloading ..." % waited, flush=True)
                try:
                    await page.goto(LOGIN_PAGE, wait_until="load")
                except Exception:
                    pass
                last_src, missing = None, 0
            await asyncio.sleep(1)
            waited += 1

        print("[qr] timeout: no scan within", LOGIN_TIMEOUT, "s — rerun for a fresh QR", flush=True)
        await crawler.browser_context.close()


if __name__ == "__main__":
    asyncio.run(main())
