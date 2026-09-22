# -*- coding: utf-8 -*-
"""
NGA 常驻 HTTP 工具（agent harness 用）—— 包在 nga_search_demo 外面的服务层。

血缘（如实分层）：
  - 复用（继承/直接调 nga_search_demo.NGASearchDemo，不重写）：
    _launch_browser / _login / run_query / _resolve_board_fid / _parse_detail / 热评提取 / 落盘。
  - 新写（demo 里没有、纯服务脚手架，属本次"常驻工具"需求本身）：
    ThreadingHTTPServer + X-Token 校验 + 单飞队列（忙时立即 429）
    + 后台线程把请求投递到持有 playwright 的主 asyncio 循环
    + 每 RECYCLE_EVERY 次查询重建一次 context（防长跑脏状态/内存累积）。

运行：
  cd <本目录> && python nga_search_server.py [--port 8770]
  # token 文件不存在时首次启动自动生成到 <本目录>/.tool_token（0600，别外传）
"""
import argparse
import asyncio
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from nga_search_demo import NGASearchDemo, bj_now, log

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(BASE_DIR, 'out')
RECYCLE_EVERY = 25          # 每 N 次查询重建一次 context

TOKEN = None
TOKEN_FILE = os.path.join(BASE_DIR, '.tool_token')
MAIN_LOOP = None
PW = None             # playwright 句柄(重建浏览器要用, 见 _rebuild)
BROWSER = None
CONTEXT = None
CRAWLER = None
RUN_LOCK = threading.Lock()
QUERY_COUNT = 0
LOGIN_OK = None       # 最近一次 _login 的结果; /health 报给前端(没登录 -> 提示更新 cookie)

# ---- 扫码登录(NGA 网页版有: 登录界面是个 iframe, 二维码由页面用 js_qrcode 现画成 data URI) ----
LOGIN_UI = 'https://ngabbs.com/nuke.php?__lib=login&__act=login_ui'
QR_LOGIN_TIMEOUT = int(os.environ.get('QR_LOGIN_TIMEOUT', '300'))   # 等扫码总时长(秒)
QR = {'state': 'idle', 'seq': 0, 'png': None, 'err': None}   # 网页轮询它
QR_LOCK = threading.Lock()


def config_path():
    """config.json 位置: 优先 env NGA_CONFIG, 其次服务目录。
    原先写死过一个老的服务器绝对路径, 换台机器就 FileNotFoundError 起不来。"""
    cands = [os.environ.get('NGA_CONFIG'), os.path.join(BASE_DIR, 'config.json')]
    for p in cands:
        if p and os.path.exists(p):
            return p
    raise SystemExit('找不到 NGA config.json; 请放到 %s 下, 或用环境变量 NGA_CONFIG 指定'
                     % BASE_DIR)


def load_cfg():
    """读 config.json: 只要 cookies(登录凭据)与 login_marker(登录态判据, 见 _login)。
    两个键都可能缺 —— 缺了就是"没登录", 照样能起服务, 只是样本少。"""
    cfg = json.load(open(config_path(), encoding='utf-8'))
    marker = (cfg.get('login_marker') or '').strip() or None
    return cfg.get('cookies', {}) or {}, marker


def load_token():
    """token 不存在则生成一次并写 .tool_token（0600）；只打印路径不打印值"""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding='utf-8') as f:
            tok = f.read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
        f.write(tok + '\n')
    os.chmod(TOKEN_FILE, 0o600)
    log('已生成新 token -> %s（读这里配置 harness 端）' % TOKEN_FILE)
    return tok


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log('http %s | %s' % (self.address_string(), fmt % args))

    def _auth(self):
        return self.headers.get('X-Token') == TOKEN

    def do_GET(self):
        if urlparse(self.path).path == '/health':
            if not self._auth():
                return self._send(401, {'error': 'unauthorized'})
            return self._send(200, {
                'ok': True, 'busy': RUN_LOCK.locked(),
                'queries_done': QUERY_COUNT, 'time': bj_now().isoformat(),
                'login': ('ok' if LOGIN_OK else 'bad') if LOGIN_OK is not None else 'unknown'})
        if urlparse(self.path).path == '/login/qr/state':
            if not self._auth():
                return self._send(401, {'error': 'unauthorized'})
            with QR_LOCK:
                st = dict(QR)
            st['login'] = ('ok' if LOGIN_OK else 'bad') if LOGIN_OK is not None else 'unknown'
            return self._send(200, st)
        self._send(404, {'error': 'not found',
                         'endpoints': ['GET /health', 'GET /login/qr/state',
                                       'POST /crawl', 'POST /login/recheck',
                                       'POST /login/qr/start']})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ('/crawl', '/login/recheck', '/login/qr/start'):
            return self._send(404, {'error': 'not found'})
        if not self._auth():
            return self._send(401, {'error': 'unauthorized'})
        if path == '/login/qr/start':
            try:
                self.rfile.read(int(self.headers.get('Content-Length') or 0))
            except Exception:
                pass
            return self._do_qr_start()
        if path == '/login/recheck':
            # 请求体要读掉: HTTP/1.1 keep-alive, 留着不读会把下一条请求读串。
            try:
                self.rfile.read(int(self.headers.get('Content-Length') or 0))
            except Exception:
                pass
            return self._do_recheck()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        except Exception:
            return self._send(400, {'error': 'bad json body'})
        query = (body.get('query') or '').strip()
        if not query:
            return self._send(400, {'error': 'empty query, need {"query": "..."}'})
        # 可选时间窗(YYYY-MM-DD): 给了就让爬虫翻页到该时段再采(见 nga_search_demo 时间窗导航)
        since = (body.get('since') or '').strip() or None
        until = (body.get('until') or '').strip() or None
        # 分段窗(对比题"今年 vs 去年"): [[since,until],...] 每段各翻一窗再合并; 优先于 since/until
        windows = body.get('windows')
        if not isinstance(windows, list):
            windows = None
        if not RUN_LOCK.acquire(blocking=False):
            return self._send(429, {'error': 'busy', 'hint': '已有查询在跑，稍后重试'})
        try:
            fut = asyncio.run_coroutine_threadsafe(_crawl(query, since, until, windows), MAIN_LOOP)
            # 带时间窗要翻页到目标时段; 分段窗(对比题)要翻 2 段, 故超时放宽
            tmo = 600 if windows else 300
            result = fut.result(timeout=tmo)
            return self._send(200, result if isinstance(result, dict)
                              else {'query': query, 'posts': [], 'note': 'no result'})
        except asyncio.TimeoutError:
            return self._send(504, {'error': 'crawl timeout %ds' % (600 if windows else 300)})
        except Exception as e:
            log('crawl fail %r' % e)
            return self._send(500, {'error': 'crawl internal error'})
        finally:
            RUN_LOCK.release()

    def _do_recheck(self):
        """重探登录态: 重新读 config.json 的 cookie(用户刚手工改的那份, 或扫码写回的那份) ->
        拿它重登 -> 如实回报。手工填 cookie 与扫码登录是两条并行的路, 都落到同一个 config.json。"""
        try:
            cookies, marker = load_cfg()
        except Exception as e:
            return self._send(500, {'error': 'config.json 读不了: %r' % e})
        if not RUN_LOCK.acquire(blocking=False):
            return self._send(429, {'error': 'busy', 'hint': '有查询在跑，等它完再校验'})
        try:
            CRAWLER.cookies = cookies
            CRAWLER.login_marker = marker
            fut = asyncio.run_coroutine_threadsafe(_recheck(CONTEXT), MAIN_LOOP)
            ok = fut.result(timeout=90)
        except Exception as e:
            log('recheck fail %r' % e)
            return self._send(500, {'error': 'recheck 失败: %r' % e})
        finally:
            RUN_LOCK.release()
        return self._send(200, {'ok': True, 'login': 'ok' if ok else 'bad',
                                'cookies': len(cookies)})

    def _do_qr_start(self):
        """起一次扫码取码任务。每点一次就重开一版码(旧版作废)。立即返回, 网页轮询 /login/qr/state。"""
        with QR_LOCK:
            QR.update({'state': 'waiting', 'seq': 0, 'png': None, 'err': None})
        try:
            asyncio.run_coroutine_threadsafe(_qr_login(), MAIN_LOOP)
        except Exception as e:
            return self._send(500, {'error': '起扫码任务失败: %r' % e})
        return self._send(200, {'ok': True, 'state': 'waiting', 'seq': 0})


def _save_cookies(uid, cid):
    """把扫码拿到的 cookie 写回 config.json(和用户手工填的是同一处, 下次启动直接生效)。
    凭证值不打印、不回传前端。原子替换写入, 免得写一半留下半个 config。"""
    try:
        path = config_path()
        cfg = json.load(open(path, encoding='utf-8'))
        cfg['cookies'] = dict(cfg.get('cookies') or {},
                              ngaPassportUid=uid, ngaPassportCid=cid)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True, ''
    except Exception as e:
        return False, 'config.json 写不进去: %r' % e


async def _grab_qr(frame):
    """取登录窗口里那张二维码的 data URI; 没有/正在跳转就返回 None。
    取码绝不抛 —— 登录页加载完还会自己重画一次, 一次异常会把整个扫码任务带走。"""
    try:
        el = await frame.query_selector('img[src^="data:image/png"]')
        if not el:
            return None
        return await el.get_attribute('src')
    except Exception:
        return None


async def _login_cookies(page):
    """读 context 里的登录态 cookie 对 (ngaPassportUid, ngaPassportCid); 缺就是没登录。"""
    try:
        cs = {c.get('name'): c.get('value') for c in await page.context.cookies()}
    except Exception:
        return None, None
    return (cs.get('ngaPassportUid') or '').strip(), (cs.get('ngaPassportCid') or '').strip()


async def _qr_login():
    """常驻 context 里开 NGA 登录页 -> 把二维码交给网页 -> 轮询 cookie -> 写回 config.json 并重登。
    NGA 的登录界面在 iframe(nuke/account_copy.html?login) 里, 二维码是页面用 js_qrcode 现画的 data URI。"""
    global LOGIN_OK
    page = None
    try:
        page = await _new_page()
        _, before_cid = await _login_cookies(page)      # 已经带着旧凭据时, 得等它换成新的才算扫上
        await page.goto(LOGIN_UI, wait_until='domcontentloaded', timeout=60000)
        frame = None
        for _ in range(40):
            for f in page.frames:
                if 'account_copy' in f.url:
                    frame = f
                    break
            if frame:
                break
            await asyncio.sleep(1)
        if frame is None:
            with QR_LOCK:
                QR['state'], QR['err'] = 'error', '登录页里没找到登录窗口(iframe)'
            log('NGA 扫码: 登录页里没找到 iframe')
            return
        log('NGA 扫码: 登录窗口已就绪, 开始取码')
        last, waited = None, 0
        while waited < QR_LOGIN_TIMEOUT:
            src = await _grab_qr(frame)
            if src and src != last:
                with QR_LOCK:
                    QR['seq'] += 1
                    QR['png'] = src
                    QR['state'] = 'waiting'
                    n = QR['seq']
                last = src
                log('NGA 扫码: 二维码已就绪(第 %d 版)' % n)
            uid, cid = await _login_cookies(page)
            if uid and cid and cid != before_cid:
                with QR_LOCK:
                    QR['state'] = 'scanned'
                log('NGA 扫码: 检测到登录 cookie, 写回 config.json（值不打印）')
                ok, msg = _save_cookies(uid, cid)
                if not ok:
                    with QR_LOCK:
                        QR['state'], QR['err'] = 'error', msg
                    return
                CRAWLER.cookies = {'ngaPassportUid': uid, 'ngaPassportCid': cid}
                try:
                    if RUN_LOCK.acquire(blocking=False):
                        try:
                            LOGIN_OK = await CRAWLER._login(CONTEXT)
                        finally:
                            RUN_LOCK.release()
                    else:
                        log('NGA 扫码: 有查询在跑, 跳过立刻重登(下次查询会自然带上新 cookie)')
                except Exception as e:
                    log('NGA 扫码: 重登失败 %r' % e)
                    LOGIN_OK = False
                with QR_LOCK:
                    QR['state'] = 'ok' if LOGIN_OK else 'error'
                    QR['err'] = None if LOGIN_OK else '扫到了码, 但重登校验没过'
                log('NGA 扫码登录 %s' % ('成功, 爬虫已换登录态' if LOGIN_OK else '失败(校验没过)'))
                return
            await asyncio.sleep(2)
            waited += 2
        with QR_LOCK:
            QR['state'] = 'error'
            QR['err'] = '等扫码超时(%ds), 请重开一次拿新码' % QR_LOGIN_TIMEOUT
        log('NGA 扫码: 超时未扫')
    except Exception as e:
        log('NGA 扫码异常 %r' % e)
        with QR_LOCK:
            QR['state'], QR['err'] = 'error', repr(e)
    finally:
        try:
            if page is not None:
                await page.close()
        except Exception:
            pass


async def _recheck(ctx):
    """拿 CRAWLER.cookies 当前值重登一次, 刷新 LOGIN_OK。(跑在 MAIN_LOOP 上)"""
    global LOGIN_OK
    LOGIN_OK = await CRAWLER._login(ctx)
    log('重新校验登录态 -> %s' % ('ok' if LOGIN_OK else 'bad'))
    return LOGIN_OK


async def _rebuild():
    """整套重建浏览器 + 上下文 + 重登。常驻服务最怕"死一次就再也起不来":
    浏览器被关/崩掉之后, 以后每次 CONTEXT.new_page() 都抛 TargetClosedError -> 每次都回 500,
    非重启服务不可。这里自愈, 重建完本次请求照常跑完。"""
    global BROWSER, CONTEXT, LOGIN_OK
    try:
        if BROWSER is not None:
            await BROWSER.close()
    except Exception:
        pass
    BROWSER = await CRAWLER._launch_browser(PW)
    CONTEXT = await BROWSER.new_context()
    LOGIN_OK = await CRAWLER._login(CONTEXT)
    log('浏览器/上下文已重建（登录态 %s）' % ('ok' if LOGIN_OK else 'bad'))


async def _new_page():
    """取一个可用页面: 先用现成的上下文, 炸了(浏览器/上下文已死)就整套重建再取一次。
    只重试一次 —— 重建还失败就是环境问题, 如实抛给调用方回 500。"""
    try:
        return await CONTEXT.new_page()
    except Exception as e:
        log('取页面失败(%r) -> 重建浏览器/上下文后重试' % e)
        await _rebuild()
        return await CONTEXT.new_page()


async def _crawl(query, since=None, until=None, windows=None):
    global QUERY_COUNT
    page = await _new_page()
    try:
        result = await CRAWLER.run_query(page, query, since=since, until=until, windows=windows)
    finally:
        try:
            await page.close()
        except Exception:
            pass
    QUERY_COUNT += 1
    if QUERY_COUNT % RECYCLE_EVERY == 0:
        await _recycle_context()
    return result


async def _recycle_context():
    global CONTEXT, LOGIN_OK
    old = CONTEXT
    try:
        ctx = await BROWSER.new_context()
    except Exception as e:
        log('context 重建失败(%r) -> 整套重建' % e)
        await _rebuild()
        return
    try:
        LOGIN_OK = await CRAWLER._login(ctx)
    except Exception as e:
        await ctx.close()
        log('context 重建失败 %r' % e)
        return
    CONTEXT = ctx
    try:
        await old.close()
    except Exception:
        pass
    log('context 已重建（累计 %d 次查询）' % QUERY_COUNT)


class _QuietServer(ThreadingHTTPServer):
    """浏览器/客户端中途把连接断了是常态, 别让它刷 traceback 把真日志淹掉。
    只吞连接类异常, 其他异常照旧打出来(那才是要找的 bug)。"""
    def handle_error(self, request, client_address):
        import sys
        et = sys.exc_info()[0]
        if et is not None and issubclass(et, (ConnectionResetError, ConnectionAbortedError,
                                              BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


async def amain(port):
    global TOKEN, MAIN_LOOP, PW, BROWSER, CONTEXT, CRAWLER, LOGIN_OK
    TOKEN = load_token()
    cookies, marker = load_cfg()
    CRAWLER = NGASearchDemo(cookies, OUTDIR)
    CRAWLER.login_marker = marker
    os.makedirs(OUTDIR, exist_ok=True)
    MAIN_LOOP = asyncio.get_running_loop()
    async with async_playwright() as p:
        PW = p
        BROWSER = await CRAWLER._launch_browser(p)
        CONTEXT = await BROWSER.new_context()
        LOGIN_OK = await CRAWLER._login(CONTEXT)
        srv = _QuietServer(('127.0.0.1', port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log('NGA tool listening http://127.0.0.1:%d （X-Token 校验已开）' % port)
        log('cookie 字段 %d | token 文件 %s' % (len(cookies), TOKEN_FILE))
        log('登录态 %s（bad=没登录, 前端应提示更新 cookie; 见 /health 的 login 字段）'
            % ('ok' if LOGIN_OK else 'bad'))
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            srv.shutdown()
            try:
                await CONTEXT.close()
            except Exception:
                pass
            try:
                await BROWSER.close()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8770)
    args = ap.parse_args()
    asyncio.run(amain(args.port))


if __name__ == '__main__':
    main()
