# -*- coding: utf-8 -*-
"""
B站(bilibili) 常驻 HTTP 工具（agent harness 用）—— 包在本爬虫树外面的服务层。

血缘（如实分层）：
  - 复用（直接调本目录的 BilibiliCrawler）：_init_resources（本次从 start() 抽出的初始化）、
    search_one_query（本次从 search_demo 抽出的单查询逻辑），及背后所有 bili 搜索/视频详情/评论 API。
  - 新写（demo 里没有、纯服务脚手架）：ThreadingHTTPServer + X-Token 校验 + 单飞队列(忙时立即429)
    + 后台线程把请求投递到持有 playwright 的主 asyncio 循环 + 每 N 次查询同步一次 context cookie。

不做自动重建 browser context 的原因：bili 登录靠 config.COOKIES 驱动，重建要重新过登录（有风控），
收益小于风险；单 context 长跑在 batch demo 里已被验证。真崩了靠 systemd Restart=always 兜底。

运行：
  cd <bili 爬虫树> && python3 -u bili_search_server.py [--port 8771]
  # token 文件不存在时首次启动自动生成到 <bili 爬虫树>/.tool_token（0600，别外传）

POST /crawl 体: {"query": "...", 可选 "since"/"until"(YYYY-MM-DD), 可选 "windows": [[a,b],...]}
  - windows(分段窗, 对比题「今年 vs 去年」): 每段各按发布时间搜一页再按 aid 去重合并;
    只给 since/until 则合成一段。都不给 = 原行为(综合排序取前 N)。

POST /creator 体: {"mid": <int>, 可选 "max_dynamics"(默认 10), 可选 "debug": true}
  - 官号探针（PLAN §10.1：引擎侧一步探针）：读某 UP 主/官号的基本信息
    (名称/签名)、粉丝数、以及最近动态(发帖)流。复用同一个常驻爬虫实例，不新起进程。
  - 动态流游客必 412，靠 profile 登录态；失败时返回 dynamics_error 而不是崩。
  - debug=true 附上游返回的键骨架(用于对形状)，正常调用不带。

POST /official 体: {"game": "<游戏名>", 可选 "since"/"until"(YYYY-MM-DD)}
  - 舆情用的官号探针（LLM 工具面 crawl_official 打这个）：取**该时间段内**的官号动态(不是"最近 N 条")
    -> 按评论数中位数算基准线 -> 标出高峰动态 -> **高峰深采 CMT_PEAK 条评论、平峰只 CMT_NORMAL 条**。
    返回 动态正文 + 各自评论(作者/正文/点赞/时间)，供模型总结"官方口径 + 评论区态度"。
    since 缺省 = 近 OFFICIAL_DEFAULT_DAYS 天(约一个版本周期); 动态流按时间倒序翻页, 翻到早于 since 即停
    —— 窗口本身就是口径, 不靠条数截断; OFFICIAL_MAX_DYN/OFFICIAL_SCAN_MAX 只是防失控的保险丝。
  - 账号先查 OFFICIAL_MIDS 表；表里没有则按名搜，只有名字全等或唯一官方认证号才认，
    否则报错并回候选名单（不瞎认号）。
  - 动态评论接口按动态类型取不同锚点：图集 draw.id+type11 / 投稿 aid+type1 / 其余 动态号+type17；
    且**不能带 pagination_str**。传错即 '啥都木有' / '访问权限不足'(2026-09-12 实测标定)。

POST /subtitle 体: {"bvids": ["BV...", ...], 可选 "limit"(默认 2), 可选 "max_minutes"(默认 30)}
  - 视频字幕全文(高热度视频解析用): 按**给定顺序**逐个看, 取满 limit 条就停。
    时长门槛: 首 P 超过 max_minutes 的**直接跳过**(长时间视频字幕不是"这段在说什么", 是整部片子)。
    跳过的每一条都带原因(too_long/no_sub/no_body/error)回来, 不静默吞。
  - 必须登录: 匿名请求播放器接口的 subtitle.subtitles 恒为空(实测 10/10 视频 0 条),
    登录态下同一批 10/15 有 AI 字幕。所以这条链走本服务自己那份登录态。

POST /login/qr/start  -> 起一条扫码链(幂等; 已登录则不重开)
GET  /login/qr/image  -> 最新那张二维码图(PNG/JPEG 原始字节, 不是 JSON)
GET  /login/qr/state  -> {"state": idle|waiting|scanned|ok|error, "seq": 第几版码, "login": ...}
  - 扫码页在**本服务常驻的那个 browser context** 里开: 扫完 update_cookies 一调, 正在跑的爬虫
    当场换登录态, 不用重启服务。前端「没登录 -> 扫码」那条链打这三个端点(harness/webapp.py 转发)。
"""
import argparse
import asyncio
import base64
import hmac
import json
import os
import re
import secrets
import statistics
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from playwright.async_api import async_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_VIDEOS = int(os.environ.get('BILI_MAX_VIDEOS', '6'))  # 每 query 取前 N 视频(交互搜索; demo 默认15不受影响)

# --- 视频字幕(2026-09-17): 高热度视频取 AI 字幕全文, 当又一格证据 ---
# 为什么要登录: 匿名请求播放器接口的 subtitle.subtitles 恒为空(实测 10/10 视频 0 条);
# 登着录同一批 10/15 有 AI 字幕(423~15784 字)。所以这条链必须走本服务自己那份登录态。
SUB_LIMIT = int(os.environ.get('BILI_SUB_LIMIT', '2'))       # 默认额度: 一次调用最多取几条
SUB_WINDOW_S = int(os.environ.get('BILI_SUB_WINDOW_S', '1800'))  # 时长门槛(秒): 首 P 超了不取
SUB_MAX_CHARS = 24000        # 保险丝: 单条字幕正文字符上限。超了截断, 但如实带 truncated 标记
SUB_SCAN_MAX = 10            # 保险丝: 一次调用最多试几个 bvid(too_long/no_sub 会被跳过, 得往下走)
sys.path.insert(0, BASE_DIR)

from media_platform.bilibili import BilibiliCrawler  # noqa: E402

OUTDIR = os.path.join(BASE_DIR, 'out')
COOKIE_REFRESH_EVERY = 25      # 每 N 次查询把 context 最新 cookie 同步给 bili_client
TZ = timezone(timedelta(hours=8))

# --- 官号探针(舆情用: 看官方账号最近动态的评论量涨落, 高峰=可能有节奏) ---
# 账号表(2026-09-12 实测 info.name 逐个核对过; 别名指向同一 mid)。不在表里 -> 按名搜, 搜不准则如实报错不猜。
OFFICIAL_MIDS = {
    '原神': 401742377,
    '明日方舟': 161775300,
    '明日方舟：终末地': 1265652806,
    '崩坏：星穹铁道': 1340190821,
    '崩铁': 1340190821,
    '星铁': 1340190821,
    '鸣潮': 1955897084,
}
OFFICIAL_DEFAULT_DAYS = 42     # 题里没给时间锚点时: 默认取最近这么多天(约一个版本周期)
OFFICIAL_MAX_DYN = 100         # 安全熔断: 窗内动态收集上限 —— **不是口径**, 是防翻页/评论失控的保险丝
OFFICIAL_SCAN_MAX = 300        # 安全熔断: 翻页扫描总量上限(锚点在很远过去时, 别无限往下翻)
PEAK_FACTOR = 3.0              # 高于基准线这么多倍…
PEAK_FLOOR = 100               # …且评论数不低于这个(挡小号噪声) 才算高峰
CMT_PEAK, CMT_NORMAL = 25, 5   # 高峰动态深采 25 条评论 / 平峰只 5 条
CMT_MODE = 0                   # 评论排序, 实测 2026-09-12: 0/3=热度(首条赞 14599, 网站上默认那个) 2=时间(首条赞 2) 1=综合**返回 0 条不可用**

TOKEN = None
TOKEN_FILE = os.path.join(BASE_DIR, '.tool_token')
MAIN_LOOP = None
CRAWLER = None
RUN_LOCK = threading.Lock()
QUERY_COUNT = 0

# --- 扫码登录（前端「没登录 -> 让用户扫码」那条链的服务端） ---
# 与 qr_login_once.py 的区别：这里跑在**服务自己那个常驻 context** 里开一个新页面，扫完
# update_cookies 一调，正在跑的爬虫当场变登录态，不用重启服务。取码细节（选择器 / 失效换码 /
# 认 SESSDATA 后 pong 验）照搬那条已验过的链。
QR_LOGIN_PAGE = 'https://passport.bilibili.com/login'
QR_SEL = 'div.login-scan__qrcode img'
QR_TIMEOUT = int(os.environ.get('QR_LOGIN_TIMEOUT', '300'))   # 等扫码总时长(秒)
QR = {'state': 'idle',    # idle|waiting|scanned|ok|error
      'seq': 0,           # 第几版码；前端看 seq 变没变决定要不要重取图
      'png': None, 'ctype': 'image/png',
      'err': None, 'waited': 0, 'task': None}
QR_LOCK = threading.Lock()


def bj_now():
    return datetime.now(TZ)


def log(msg):
    print('[%s] %s' % (bj_now().strftime('%H:%M:%S'), msg), flush=True)


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


def _qr_sniff(data):
    """二维码图是什么格式 —— 只认魔数, 不引 PIL(那是个额外依赖)。"""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:2] == b'\xff\xd8':
        return 'image/jpeg'
    return 'application/octet-stream'


def _qr_decode(data_uri):
    """data-URI -> 原始图字节。坏值回 None(取码绝不能把整条链带走)。"""
    s = data_uri or ''
    if ',' in s:
        s = s.split(',', 1)[1]
    try:
        return base64.b64decode(s)
    except Exception:
        return None


async def _qr_grab(page):
    """取当前二维码 img 的 src；没有 / 页面正在跳转就返回 None。
    取码绝不能抛：登录页加载完还会自己跳一次，一次导航曾把整个进程带走（2026-09-12 实测）。"""
    try:
        el = await page.query_selector(QR_SEL)
        if not el:
            return None
        return await el.get_attribute('src')
    except Exception:
        return None


def _qr_reset(state='waiting'):
    """把扫码态清回初始值。**调用方必须已持有 QR_LOCK** —— 见 _do_qr_start 的「检查+置位」原子块。"""
    QR.update({'state': state, 'seq': 0, 'png': None, 'err': None, 'waited': 0})


async def _qr_login():
    """常驻 context 里开登录页取码轮询，扫到了就热更新登录态。跑在 MAIN_LOOP 上。"""
    page = None
    try:
        # 只在**当前确实没登录**时才清 cookie：登录页带着死 cookie 打开会自己跳走，
        # 码永远取不到；而登录态好用的时候清 cookie 会把正在用的态洗掉。
        if CRAWLER.logged_in is not True:
            await CRAWLER.browser_context.clear_cookies()
            log('扫码前清掉残留 cookie(当前未登录)')
        page = await CRAWLER.browser_context.new_page()
        try:
            await page.goto(QR_LOGIN_PAGE, wait_until='load')
        except Exception:
            pass                      # 登录页自己会再跳一次, 这里失败不算数
        await asyncio.sleep(2)

        last_src, missing, waited = None, 0, 0
        while waited < QR_TIMEOUT:
            src = await _qr_grab(page)
            if src and src != last_src:
                data = _qr_decode(src)
                if data:
                    with QR_LOCK:
                        QR['seq'] += 1
                        QR['png'] = data
                        QR['ctype'] = _qr_sniff(data)
                        QR['state'] = 'waiting'
                    last_src, missing = src, 0
                    log('二维码已就绪(第 %d 版, %d 字节)' % (QR['seq'], len(data)))
            elif src is None:
                missing += 1

            # 扫码后 SESSDATA/DedeUserID 会落到 context cookie 里
            names = {c.get('name') for c in await CRAWLER.browser_context.cookies()}
            if 'SESSDATA' in names and 'DedeUserID' in names:
                with QR_LOCK:
                    QR['state'] = 'scanned'
                log('检测到扫码(%ds), 正在校验登录态…' % waited)
                await CRAWLER.bili_client.update_cookies(browser_context=CRAWLER.browser_context)
                ok = await CRAWLER.bili_client.pong()
                CRAWLER.logged_in = bool(ok)
                with QR_LOCK:
                    QR['state'] = 'ok' if ok else 'error'
                    QR['err'] = None if ok else '扫到了码, 但 pong 校验没过(登录态不可用)'
                log('扫码登录 %s' % ('成功, 爬虫已换登录态' if ok else '失败(校验没过)'))
                return

            # 码有时效 / 页面自己跳走了：换一张新码, 别让用户对着死码扫
            reload_now = (src is None and missing >= 15)
            if not reload_now and waited and waited % 5 == 0:
                try:
                    body = await page.inner_text('body')
                except Exception:
                    body = ''
                reload_now = ('失效' in body or '过期' in body)
            if reload_now:
                log('二维码失效/页面跳走(%ds), 换一张' % waited)
                try:
                    await page.goto(QR_LOGIN_PAGE, wait_until='load')
                except Exception:
                    pass
                last_src, missing = None, 0

            await asyncio.sleep(1)
            waited += 1
            with QR_LOCK:
                QR['waited'] = waited

        with QR_LOCK:
            QR['state'] = 'error'
            QR['err'] = '等扫码超时(%ds), 请重开一次拿新码' % QR_TIMEOUT
    except Exception as e:
        log('扫码链路异常 %r' % e)
        with QR_LOCK:
            QR['state'] = 'error'
            QR['err'] = repr(e)[:200]
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        with QR_LOCK:
            QR['task'] = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log('http %s | %s' % (self.address_string(), fmt % args))

    def _auth(self):
        # 常量时间比较(别用 ==: 逐字符比较, 时间差能一个字节一个字节地把 token 试出来)。
        # 比 bytes 而不是 str: 头里塞非 ASCII 时 str 比较会抛 TypeError, 那等于给个 500 的探测口子。
        return hmac.compare_digest((self.headers.get('X-Token') or '').encode('utf-8'),
                                   (TOKEN or '').encode('utf-8'))

    def _login_state(self):
        """登录态供前端健康检查: ok=有登录态 / bad=没登录(该提示扫码) / unknown=还没探到。"""
        v = getattr(CRAWLER, 'logged_in', None)
        if v is True:
            return 'ok'
        if v is False:
            return 'bad'
        return 'unknown'

    def _qr_state(self):
        with QR_LOCK:
            return {'state': QR['state'], 'seq': QR['seq'], 'waited': QR['waited'],
                    'error': QR['err'], 'login': self._login_state()}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/health':
            if not self._auth():
                return self._send(401, {'error': 'unauthorized'})
            return self._send(200, {
                'ok': True, 'busy': RUN_LOCK.locked(),
                'queries_done': QUERY_COUNT, 'time': bj_now().isoformat(),
                'login': self._login_state()})
        if path in ('/login/qr/image', '/login/qr/state'):
            if not self._auth():
                return self._send(401, {'error': 'unauthorized'})
            if path == '/login/qr/state':
                return self._send(200, self._qr_state())
            return self._qr_image()
        self._send(404, {'error': 'not found',
                         'endpoints': ['GET /health', 'POST /crawl', 'POST /creator', 'POST /official',
                                       'POST /subtitle',
                                       'POST /login/qr/start', 'GET /login/qr/image',
                                       'GET /login/qr/state']})

    def _qr_image(self):
        """出最新那张码。生成要等页面渲染, 所以在这里**等一下**(最多 25s), 让前端一个请求就能拿到。"""
        for _ in range(50):
            with QR_LOCK:
                png, ctype, state = QR['png'], QR['ctype'], QR['state']
            if png:
                return self._send_bytes(200, png, ctype)
            if state in ('idle', 'error'):
                break
            time.sleep(0.5)
        with QR_LOCK:
            err = QR['err'] or ('还没开始取码' if QR['state'] == 'idle' else '二维码还没出来')
        return self._send(503, {'error': err})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ('/crawl', '/creator', '/official', '/subtitle', '/login/qr/start'):
            return self._send(404, {'error': 'not found'})
        if not self._auth():
            return self._send(401, {'error': 'unauthorized'})
        if path == '/login/qr/start':
            # 请求体要读掉: 这是 HTTP/1.1 keep-alive, 留着不读会把下一条请求读串。
            try:
                self.rfile.read(int(self.headers.get('Content-Length') or 0))
            except Exception:
                pass
            return self._do_qr_start()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        except Exception:
            return self._send(400, {'error': 'bad json body'})

        if path == '/creator':
            return self._do_creator(body)
        if path == '/official':
            return self._do_official(body)
        if path == '/subtitle':
            return self._do_subtitle(body)
        query = (body.get('query') or '').strip()
        if not query:
            return self._send(400, {'error': 'empty query, need {"query": "..."}'})
        windows = body.get('windows')
        if not isinstance(windows, list):
            windows = None                    # 分段窗(对比题): 每段各搜一页再合并
        since = (body.get('since') or '').strip() or None
        until = (body.get('until') or '').strip() or None
        if not RUN_LOCK.acquire(blocking=False):
            return self._send(429, {'error': 'busy', 'hint': '已有查询在跑，稍后重试'})
        tmo = 600 if windows else 300          # 分段窗要跑 N 段搜索, 放宽
        try:
            fut = asyncio.run_coroutine_threadsafe(_crawl(query, windows, since, until), MAIN_LOOP)
            result = fut.result(timeout=tmo)
            return self._send(200, result if isinstance(result, dict)
                              else {'query': query, 'videos': [], 'note': 'no result'})
        except asyncio.TimeoutError:
            return self._send(504, {'error': 'crawl timeout %ds' % tmo})
        except Exception as e:
            log('crawl fail %r' % e)
            if getattr(e, 'risk', False):
                # 风控拦截(412/403): 明说是"被平台挡住"并带 risk 标记 —— 上游据此把 bili 记成风控
                # 并会话冷却。混进 'crawl internal error' 的话, 上游只会读成"这题社区没讨论"。
                return self._send(503, {'error': str(e)[:200], 'risk': True})
            return self._send(500, {'error': 'crawl internal error'})
        finally:
            RUN_LOCK.release()

    def _do_qr_start(self):
        """起扫码。幂等: 已有一条在跑就复用; 而且**已经登录就不重开**(别去洗掉好用的态)。

        「有没有在跑」的检查和「置上 task」必须在**同一把 QR_LOCK** 里做完: 分成两段的话,
        两个并发请求会同时看到 task 为空, 各起一条扫码链(两个页面抢同一个 context 的 cookie)。"""
        if self._login_state() == 'ok':
            with QR_LOCK:
                QR['state'] = 'ok'
                QR['err'] = None
            return self._send(200, self._qr_state())
        with QR_LOCK:
            t = QR['task']
            if t is not None and not t.done():
                started = False                           # 已有在跑的, 别开第二条
            else:
                _qr_reset()                               # 检查+置位同一把锁 => 原子
                QR['task'] = asyncio.run_coroutine_threadsafe(_qr_login(), MAIN_LOOP)
                started = True
        if started:
            log('扫码链路已起(前端点了「去修复」)')
        return self._send(200, self._qr_state())

    def _do_creator(self, body):
        try:
            mid = int(body.get('mid'))
        except (TypeError, ValueError):
            return self._send(400, {'error': 'need {"mid": <int>}'})
        max_dynamics = int(body.get('max_dynamics') or 10)
        debug = bool(body.get('debug'))
        if not RUN_LOCK.acquire(blocking=False):
            return self._send(429, {'error': 'busy', 'hint': '已有任务在跑，稍后重试'})
        try:
            fut = asyncio.run_coroutine_threadsafe(_creator(mid, max_dynamics, debug), MAIN_LOOP)
            return self._send(200, fut.result(timeout=240))
        except asyncio.TimeoutError:
            return self._send(504, {'error': 'creator probe timeout'})
        except Exception as e:
            log('creator fail %r' % e)
            return self._send(500, {'error': 'creator internal error'})
        finally:
            RUN_LOCK.release()

    def _do_official(self, body):
        game = (body.get('game') or '').strip()
        if not game:
            return self._send(400, {'error': 'need {"game": "<游戏名>"}'})
        since = (body.get('since') or '').strip() or None
        until = (body.get('until') or '').strip() or None
        if not RUN_LOCK.acquire(blocking=False):
            return self._send(429, {'error': 'busy', 'hint': '已有任务在跑，稍后重试'})
        try:
            mode = int(body.get('mode') or CMT_MODE)   # 评论排序, 只给标定用; harness 不传
            debug = bool(body.get('debug'))
            # 时限给足: 窗口按时间算, 一个版本周期可能几十条动态, 每条都要取评论
            fut = asyncio.run_coroutine_threadsafe(
                _official(game, mode, debug, since, until), MAIN_LOOP)
            return self._send(200, fut.result(timeout=600))
        except asyncio.TimeoutError:
            return self._send(504, {'error': 'official probe timeout'})
        except Exception as e:
            log('official fail %r' % e)
            return self._send(500, {'error': 'official internal error'})
        finally:
            RUN_LOCK.release()

    def _do_subtitle(self, body):
        bvids = [str(b).strip() for b in (body.get('bvids') or []) if str(b).strip()]
        if not bvids:
            return self._send(400, {'error': 'need {"bvids": ["BV..."]}'})
        limit = int(body.get('limit') or SUB_LIMIT)
        max_minutes = int(body.get('max_minutes') or (SUB_WINDOW_S // 60))
        if limit <= 0:
            return self._send(200, {'items': [], 'skipped': [], 'note': '额度 0, 本轮不取字幕'})
        if not RUN_LOCK.acquire(blocking=False):
            return self._send(429, {'error': 'busy', 'hint': '已有任务在跑，稍后重试'})
        try:
            # 一条字幕要三步请求(视频信息->播放器->下载), 扫不满还要往下走, 时限给足
            fut = asyncio.run_coroutine_threadsafe(_subtitle(bvids, limit, max_minutes), MAIN_LOOP)
            return self._send(200, fut.result(timeout=240))
        except asyncio.TimeoutError:
            return self._send(504, {'error': 'subtitle timeout'})
        except Exception as e:
            log('subtitle fail %r' % e)
            return self._send(500, {'error': 'subtitle internal error'})
        finally:
            RUN_LOCK.release()

_NORM_DROP = re.compile(r'[\s:：;；、,，。.·・()（）\[\]【】《》"\'\-/]+')


def _norm_name(s):
    """名字归一: 去空白/中英标点/连字符, 好让「崩坏3」≈「崩坏3」和「崩坏：星穹铁道」≈「崩坏星穹铁道」。"""
    return _NORM_DROP.sub('', s or '')


def _pick_official(game, cands):
    """从候选里挑官号。判据(2026-09-13 实测标定, 15 游戏全对/安全拒答):
    ① 只认 official_verify.type==1(机构/官方认证) 且 名字或认证描述里含游戏名的候选;
       这一层挡掉「名字恰巧全等但没认证的蹭名号」——旧版 bug 本体。
    ② 一个都没有 -> 拒答(不瞎认)。
    ③ 有多个: 优先「归一后名字全等」; 并列再取粉丝最多的。
       不设粉丝硬门槛 —— 金铲铲之战官号仅 75 万粉, 卡门槛会误拒。
    ④ 没有全等 -> 取粉丝最多的(阴阳师/第五人格这类官号名带前缀, 靠粉丝量级取胜)。"""
    g = _norm_name(game)
    offs = [c for c in cands
            if c.get('vt') == 1 and g
            and (g in _norm_name(c.get('uname')) or g in _norm_name(c.get('official')))]
    if not offs:
        return None, 'no_official', []
    exact = [c for c in offs if _norm_name(c.get('uname')) == g]
    pool = exact or offs
    best = max(pool, key=lambda c: c.get('fans') or 0)
    return best['mid'], ('official_name_exact' if exact else 'official_top_fans'), offs


async def _resolve_mid(cli, game):
    """游戏名 -> 官号 mid: 先查表; 表里没有按名搜, 候选过 _pick_official 判据(见其 docstring)。
    有歧义/没搜到一律返回 None + 候选名单 —— 宁可报错让人工加表, 也不瞎认一个号(认错=整题证据全废)。"""
    if game in OFFICIAL_MIDS:
        return OFFICIAL_MIDS[game], 'table', None
    try:
        # 必须走 wbi 签名端点: 非 wbi 的 /x/web-interface/search/type 会被风控甩 HTML 拦截页
        # (2026-09-13 实测: DataFetchError Failed to decode JSON ... <!DOCTYPE HTML>), 故用 wbi + 签名。
        res = await cli.get('/x/web-interface/wbi/search/type',
                            {'search_type': 'bili_user', 'page': 1, 'keyword': game},
                            enable_params_sign=True)
    except Exception as e:
        return None, 'search_fail', [{'error': repr(e)[:160]}]
    users = (res.get('result') or [])[:20]
    cands = [{'mid': u.get('mid'), 'uname': u.get('uname'),
              'fans': u.get('fans'),
              'vt': (u.get('official_verify') or {}).get('type'),
              'official': ((u.get('official_verify') or {}).get('desc') or '')} for u in users]
    mid, why, offs = _pick_official(game, cands)
    if mid is None:
        return None, 'ambiguous', cands
    return mid, why, offs


def _day_bounds(since, until):
    """'YYYY-MM-DD' -> (since_dt, until_dt) 东八区自然日边界(since 取当日 00:00, until 取当日 23:59:59)。
    两者都可能为 None: until 缺省 = 现在(探到最新); since 缺省 = 近 OFFICIAL_DEFAULT_DAYS 天。
    解析不了的日期当没给(退回缺省), 不报错 —— 时段只是缩小范围, 不该让整次探针失败。"""
    now = bj_now()
    s = None
    if since:
        try:
            s = datetime.strptime(str(since)[:10], '%Y-%m-%d').replace(tzinfo=TZ)
        except ValueError:
            s = None
    if s is None:
        s = (now - timedelta(days=OFFICIAL_DEFAULT_DAYS)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    u = now
    if until:
        try:
            u = datetime.strptime(str(until)[:10], '%Y-%m-%d').replace(
                hour=23, minute=59, second=59, tzinfo=TZ)
        except ValueError:
            u = now
    if u < s:
        u = s + timedelta(days=1)
    return s, u


def _pub_ts(it):
    """动态发布时间戳(秒, int)。上游给的是字符串, 缺/坏 -> None(拿不到时间的动态不按窗口丢)。"""
    t = ((it.get('modules') or {}).get('module_author') or {}).get('pub_ts')
    try:
        return int(t)
    except (TypeError, ValueError):
        return None


def _peak_flags(counts):
    """高峰判定: 基准线取**中位数**(不是均值 —— 那条高峰自己会把均值抬上去, 峰越大越认不出来),
    某条评论数 >= 基准线 PEAK_FACTOR 倍、且 >= PEAK_FLOOR 才算高峰。返回 (flags, baseline)。"""
    vals = [c for c in counts if isinstance(c, int)]
    if not vals:
        return [False] * len(counts), None
    base = statistics.median(vals)
    flags = [bool(isinstance(c, int) and c >= PEAK_FLOOR and c >= PEAK_FACTOR * base)
             for c in counts]
    return flags, base


async def _dyn_comments(cli, oid, ctype, n, mode=CMT_MODE, raw_sink=None):
    """动态下的评论。oid/type 由 _dyn 按动态类型给出(图集 draw.id+11 / 投稿 aid+1 / 其余 动态号+17)。
    按 mode 排序取前 n 条。注意: **不能带 pagination_str** —— 一带有就 访问权限不足。"""
    out, nxt = [], 0
    while len(out) < n:
        res = await cli.get('/x/v2/reply/wbi/main',
                            {'oid': int(oid), 'type': ctype, 'mode': mode,
                             'ps': 20, 'next': nxt}, enable_params_sign=True)
        if raw_sink is not None:
            raw_sink.clear()
            raw_sink.update(_trunc(res, 300))
        batch = res.get('replies') or []
        for c in batch:
            msg = ((c.get('content') or {}).get('message') or '').strip()
            if not msg:
                continue
            ts = c.get('ctime')
            out.append({'id': str(c.get('rpid') or ''),
                        'author': (c.get('member') or {}).get('uname') or '',
                        'text': msg.replace('\n', ' ')[:400],
                        'like': c.get('like') if isinstance(c.get('like'), int) else None,
                        'published_at': datetime.fromtimestamp(ts, TZ).strftime('%Y-%m-%d %H:%M') if ts else ''})
        cur = res.get('cursor') or {}
        if not batch or cur.get('is_end'):
            break
        nxt = cur.get('next') or 0
        if not nxt:
            break
        await asyncio.sleep(1.0)                  # 相邻页留间隔, 别催
    return out[:n]


async def _official(game, cmt_mode=CMT_MODE, debug=False, since=None, until=None):
    """官号动态探针(舆情用): 取**该时间段内**的动态 -> 算评论量基准线 -> 标出高峰动态 ->
    高峰深采 CMT_PEAK 条评论、平峰只 CMT_NORMAL 条。给模型的是"动态正文 + 各自评论态度"。
    时段口径: since/until = 'YYYY-MM-DD'(题里的显式时间锚点); since 缺省 = 近 OFFICIAL_DEFAULT_DAYS 天。
    动态流按发布时间倒序, 遇到早于 since 的动态即停 —— **窗口由时间定, 不用条数截断**;
    OFFICIAL_MAX_DYN/OFFICIAL_SCAN_MAX 只是保险丝, 真撞上会在返回里标 truncated(别读成"就这么多")。
    任一动态取评论失败只记空, 不整体崩(评论数仍是动态自带的, 可供判断)。"""
    cli = CRAWLER.bili_client
    since_dt, until_dt = _day_bounds(since, until)
    since_ts, until_ts = since_dt.timestamp(), until_dt.timestamp()
    out = {'game': game, 'probed_at': bj_now().isoformat(),
           'window_since': since_dt.strftime('%Y-%m-%d'),
           'window_until': until_dt.strftime('%Y-%m-%d')}
    mid, how, cands = await _resolve_mid(cli, game)
    out['resolved_by'] = how
    if not mid:
        out['error'] = '没找到 %r 的官号(不猜账号); 候选见 candidates' % game
        if cands:
            out['candidates'] = cands
        return out
    out['mid'] = mid
    try:
        info = await cli.get_creator_info(mid)
        out['official_name'] = info.get('name')
    except Exception as e:
        out['official_name'] = None
        out['info_error'] = repr(e)[:160]
    try:
        stat = await cli.get('/x/relation/stat', {'vmid': mid}, enable_params_sign=False)
        out['follower'] = stat.get('follower')
    except Exception as e:
        out['follower_error'] = repr(e)[:160]

    items, offset, err = [], '', None
    scanned, reached = 0, False
    try:
        while scanned < OFFICIAL_SCAN_MAX and len(items) < OFFICIAL_MAX_DYN:
            for attempt in range(3):              # 该端点偶发甩 HTML 拦截页, 退避重试
                try:
                    page = await cli.get_creator_dynamics(mid, offset)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(3.0 * (attempt + 1))
            batch = page.get('items') or []
            if not batch:
                reached = True                    # 动态流到头了: 窗内这些就是全部
                break
            scanned += len(batch)
            stop = False
            for it in batch:
                ts = _pub_ts(it)
                if ts is None:                    # 时间缺失的照收(宁可多给, 别按窗口误丢)
                    items.append(it)
                elif ts < since_ts:
                    stop = True                   # 倒序流: 到窗外(早于 since)了, 后面只会更早
                    break
                elif ts > until_ts:
                    continue                      # 晚于窗口上沿(until): 不算本窗, 接着往下翻
                else:
                    items.append(it)
                if len(items) >= OFFICIAL_MAX_DYN:
                    break
            if stop:
                reached = True
                break
            if not page.get('has_more'):
                reached = True
                break
            offset = page.get('offset') or ''
            await asyncio.sleep(1.0)              # 相邻页留间隔, 别催
    except Exception as e:
        err = repr(e)[:160]
    if err:
        out['dynamics_error'] = err

    out['scanned'] = scanned
    out['window_reached'] = reached               # False = 翻够保险丝还没到窗口起点(更早的月份没取到)
    out['truncated'] = len(items) >= OFFICIAL_MAX_DYN
    out['dynamics_count'] = len(items)
    dyns = [_dyn(it) for it in items[:OFFICIAL_MAX_DYN]]
    flags, base = _peak_flags([d['comment'] for d in dyns])
    out['baseline_comments'] = base
    out['peak_rule'] = '评论数 >= 基准线 %g 倍且 >= %d 条' % (PEAK_FACTOR, PEAK_FLOOR)
    sink = {} if debug else None
    for i, (d, peak) in enumerate(zip(dyns, flags)):
        d['is_peak'] = peak
        try:
            d['comments'] = await _dyn_comments(cli, d['cmt_oid'], d['cmt_type'],
                                                CMT_PEAK if peak else CMT_NORMAL,
                                                cmt_mode, sink if i == 0 else None)
        except Exception as e:
            d['comments'] = []
            d['comments_error'] = repr(e)[:120]
        await asyncio.sleep(0.6)
    if debug:
        out['debug'] = {'comment_raw': sink}
    out['dynamics'] = dyns
    out['peak_count'] = sum(1 for f in flags if f)
    return out


async def _creator(mid, max_dynamics=10, debug=False):
    """官号探针: 基本信息 + 粉丝数 + 最近动态流。任一段失败只记错误, 不整体崩
    (动态流游客必 412, 全靠 profile 登录态)。"""
    cli = CRAWLER.bili_client
    out = {'mid': mid, 'probed_at': bj_now().isoformat()}

    try:
        info = await cli.get_creator_info(mid)
        out['info'] = {'name': info.get('name'), 'sign': (info.get('sign') or '')[:120],
                       'level': info.get('level'),
                       'vip': bool((info.get('vip') or {}).get('status'))}
    except Exception as e:
        out['info'] = None
        out['info_error'] = repr(e)[:160]

    try:
        stat = await cli.get('/x/relation/stat', {'vmid': mid}, enable_params_sign=False)
        out['stat'] = {'follower': stat.get('follower'), 'following': stat.get('following')}
    except Exception as e:
        out['stat'] = None
        out['stat_error'] = repr(e)[:160]

    items, offset, err = [], '', None
    try:
        while len(items) < max_dynamics:
            # 该端点偶发甩 HTML 拦截页(实测: 同一账号连续探会中, 隔一会儿再探就好), 退避重试
            for attempt in range(3):
                try:
                    page = await cli.get_creator_dynamics(mid, offset)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(3.0 * (attempt + 1))
            batch = page.get('items') or []
            items.extend(batch)
            if not page.get('has_more') or not batch:
                break
            offset = page.get('offset') or ''
            await asyncio.sleep(1.0)                 # 相邻页留间隔, 别催
    except Exception as e:
        err = repr(e)[:160]
    out['dynamics_count'] = len(items)
    out['dynamics'] = [_dyn(it) for it in items[:max_dynamics]]
    if err:
        out['dynamics_error'] = err
    if debug:
        first = items[0] if items else {}
        out['debug'] = {'first_item_keys': sorted(first.keys()),
                        'first_modules': sorted((first.get('modules') or {}).keys()),
                        'shapes': [_shape(it) for it in items[:6]],
                        'md_raw': [_trunc((it.get('modules') or {}).get('module_dynamic'))
                                   for it in items[:4]]}
    return out


def _trunc(o, n=200):
    """debug: 递归截断字符串, 让原始子树能塞进一次响应里看全。"""
    if isinstance(o, dict):
        return {k: _trunc(v, n) for k, v in o.items()}
    if isinstance(o, list):
        return [_trunc(v, n) for v in o[:2]]
    if isinstance(o, str):
        return o[:n]
    return o


def _shape(it):
    """debug: 每种动态类型的 module_dynamic / major 子结构键名, 用来写正文提取的兜底。"""
    md = (it.get('modules') or {}).get('module_dynamic') or {}
    major = md.get('major') or {}

    def sub(d, k):
        v = d.get(k)
        return sorted(v.keys()) if isinstance(v, dict) else (type(v).__name__ if v is not None else None)

    draw_items = ((major.get('draw') or {}).get('items') or [])
    return {'type': it.get('type'),
            'md_keys': sorted(md.keys()),
            'major_keys': sorted(major.keys()),
            'major_sub': {k: sub(major, k) for k in major},
            'draw_item0_keys': sorted((draw_items[0] or {}).keys()) if draw_items else None}


def _dyn(it):
    """动态卡 -> 瘦身条目: 时间 / 正文 / 类型 / 作者 / 互动数。缺字段留 None, 不编。

    正文兜底(2026-09-12 实测形状): 图集类上游**根本没有正文**(desc 键都不存在),
    直播推流类的正文是 major.live_rcmd.content 里的一个 JSON 串, 投稿类标题在
    major.archive。不兜底的话一半动态是空白。"""
    mods = it.get('modules') or {}
    author = mods.get('module_author') or {}
    dyn = mods.get('module_dynamic') or {}
    desc, major = dyn.get('desc') or {}, dyn.get('major') or {}
    stat = mods.get('module_stat') or {}
    arc = major.get('archive') or {}
    kind = major.get('type') or it.get('type')

    def cnt(k):
        v = (stat.get(k) or {}).get('count')
        return v if isinstance(v, int) else None

    text = (desc.get('text') or '').replace('\n', ' ').strip()
    if not text:
        if kind == 'MAJOR_TYPE_LIVE_RCMD':
            try:
                lc = json.loads((major.get('live_rcmd') or {}).get('content') or '{}')
                text = '[直播] ' + ((lc.get('live_play_info') or {}).get('title') or '')
            except ValueError:
                text = '[直播]'
        elif kind == 'MAJOR_TYPE_ARCHIVE':
            text = (arc.get('title') or '').replace('\n', ' ')
        elif kind == 'MAJOR_TYPE_DRAW':
            n = len(((major.get('draw') or {}).get('items') or []))
            text = '[图集 %d 张]' % n if n else ''
        elif it.get('type') == 'DYNAMIC_TYPE_FORWARD':     # 纯转发(自己没写话)时看原文
            od = (((it.get('orig') or {}).get('modules') or {}).get('module_dynamic') or {}).get('desc') or {}
            text = (od.get('text') or '').replace('\n', ' ').strip()

    # 取评论的锚点: 按动态类型取不同 oid 和 type 号(2026-09-12 实测, 见 §10.40)。
    # 图集 -> draw.id + 11; 投稿 -> archive.aid + 1; 其余(转发/纯文字/直播) -> 动态号 + 17。
    # 传错就是 DataFetchError('啥都木有')。
    if kind == 'MAJOR_TYPE_DRAW' and (major.get('draw') or {}).get('id'):
        cmt_oid, cmt_type = str(major['draw']['id']), 11
    elif arc.get('aid'):
        cmt_oid, cmt_type = str(arc['aid']), 1
    else:
        cmt_oid, cmt_type = it.get('id_str'), 17

    # pub_ts 上游给的是**字符串**(如 '1786420820'); pub_time 只是"8月11日/3天前"这类相对文案,
    # 归档要可排序可比对(引擎按时间窗丢老样本), 必须换算成 ISO, 换算不了才退回文案。
    ts = _pub_ts(it)
    iso = datetime.fromtimestamp(ts, TZ).strftime('%Y-%m-%d %H:%M') if ts else ''

    return {'id': it.get('id_str'), 'type': it.get('type'), 'kind': kind,
            'published_at': iso or (author.get('pub_time') or ''),
            'published_at_rel': author.get('pub_time'), 'pub_ts': author.get('pub_ts'),
            'author': author.get('name'), 'text': text[:600],
            'bvid': arc.get('bvid'),
            'play': (arc.get('stat') or {}).get('play'),
            'like': cnt('like'), 'comment': cnt('comment'), 'forward': cnt('forward'),
            'cmt_oid': cmt_oid, 'cmt_type': cmt_type}


async def _crawl(query, windows=None, since=None, until=None):
    global QUERY_COUNT
    result = await CRAWLER.search_one_query(query=query, max_videos=MAX_VIDEOS,
                                            windows=windows, since=since, until=until)
    QUERY_COUNT += 1
    if QUERY_COUNT % COOKIE_REFRESH_EVERY == 0:
        try:
            await CRAWLER.bili_client.update_cookies(browser_context=CRAWLER.browser_context)
            log('已同步 context cookie（累计 %d 次查询）' % QUERY_COUNT)
        except Exception as e:
            log('cookie 同步失败 %r' % e)
    return result


def _sub_url(s):
    """字幕文件的 url。上游有时给 '//aisubtitle...' 这种协议相对写法, 补上 https:。"""
    u = (s.get('subtitle_url') or '').strip()
    return ('https:' + u) if u.startswith('//') else u


def _sub_pick(subs):
    """挑一条字幕轨: 优先 AI 中文(ai-zh), 再任意 ai-*, 再中文, 最后第一条。

    **不能按返回顺序拿**: 实测返回顺序不是优先级顺序(要的中文 AI 轨常排在后面);
    空 url 的轨也不能选(选了等于白跳过这条视频)。
    """
    cands = [s for s in (subs or []) if _sub_url(s)]
    if not cands:
        return None
    for s in cands:
        if (s.get('lan') or '').lower() == 'ai-zh':
            return s
    for pre in ('ai-', 'zh'):
        for s in cands:
            if (s.get('lan') or '').lower().startswith(pre):
                return s
    return cands[0]


def _sub_text(body):
    """{from,to,content} 数组 -> 正文。中间静默超过 1.5 秒就断行 —— 一口气拼成一大坨没法读。"""
    out, prev_to = [], None
    for seg in body or []:
        c = (seg.get('content') or '').strip()
        if not c:
            continue
        frm = seg.get('from')
        if prev_to is not None and isinstance(frm, (int, float)) and frm - prev_to > 1.5:
            out.append('\n')
        out.append(c)
        prev_to = seg.get('to')
    return ''.join(out).strip()


async def _subtitle(bvids, limit, max_minutes):
    """按给定顺序取视频字幕, 取满 limit 条即停。单条出问题只记一笔, 不带走整轮。"""
    cli = CRAWLER.bili_client
    items, skipped = [], []
    for bv in bvids[:SUB_SCAN_MAX]:
        if len(items) >= limit:
            break
        try:
            v = await cli.get('/x/web-interface/view', {'bvid': bv}, enable_params_sign=False)
            dur, aid, cid = v.get('duration') or 0, v.get('aid'), v.get('cid')
            # 时长看**首 P**: aid/cid 本来就只有首段(合集/分P 只暴露第一段, 见实测记录)
            if dur and dur > max_minutes * 60:
                skipped.append({'bvid': bv, 'why': 'too_long', 'minutes': round(dur / 60.0)})
                continue
            if not (aid and cid):
                skipped.append({'bvid': bv, 'why': 'no_cid'})
                continue
            p = await cli.get('/x/player/wbi/v2', {'aid': aid, 'cid': cid, 'bvid': bv})
            s = _sub_pick((p.get('subtitle') or {}).get('subtitles'))
            if not s:
                skipped.append({'bvid': bv, 'why': 'no_sub'})
                continue
            raw = await cli.get_video_media(_sub_url(s))
            body = []
            if raw:
                body = (json.loads(raw.decode('utf-8')) or {}).get('body') or []
            text = _sub_text(body)
            if not text:
                skipped.append({'bvid': bv, 'why': 'no_body'})
                continue
            items.append({'bvid': bv, 'aid': aid, 'cid': cid,
                          'title': (v.get('title') or '')[:120],
                          'minutes': round(dur / 60.0), 'lan': s.get('lan'),
                          'segments': len(body), 'chars': len(text),
                          'truncated': len(text) > SUB_MAX_CHARS,
                          'text': text[:SUB_MAX_CHARS]})
        except Exception as e:
            log('字幕取失败 %s: %r' % (bv, e))
            skipped.append({'bvid': bv, 'why': 'error', 'detail': repr(e)[:120]})
    if skipped:
        log('字幕: 取到 %d 条, 跳过 %d 条(%s)'
            % (len(items), len(skipped), ','.join(s['why'] for s in skipped)))
    return {'items': items, 'skipped': skipped}


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
    global TOKEN, MAIN_LOOP, CRAWLER
    TOKEN = load_token()
    CRAWLER = BilibiliCrawler()
    os.makedirs(OUTDIR, exist_ok=True)
    MAIN_LOOP = asyncio.get_running_loop()
    async with async_playwright() as playwright:
        await CRAWLER._init_resources(playwright)
        srv = _QuietServer(('127.0.0.1', port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log('bili tool listening http://127.0.0.1:%d （X-Token 校验已开）' % port)
        log('token 文件 %s | out %s' % (TOKEN_FILE, OUTDIR))
        log('登录态 %s（bad=没登录, 前端应提示扫码; 见 /health 的 login 字段）'
            % ('ok' if CRAWLER.logged_in else 'bad'))
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            srv.shutdown()
            try:
                await CRAWLER.browser_context.close()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8771)
    args = ap.parse_args()
    asyncio.run(amain(args.port))


if __name__ == '__main__':
    main()
