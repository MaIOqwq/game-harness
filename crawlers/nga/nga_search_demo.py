# -*- coding: utf-8 -*-
"""
NGA LLM 搜索 demo —— 改造自毕设 nga_crawler_playwright.py
（毕设原版未改动；本目录 nga_crawler_playwright.py 为原版只读副本，本文件继承其类做改造）

改造点：
  1) 驱动：原版 while-True 固定 22 游戏 + 12h 增量 daemon  -> 读 queries.txt 一次性跑（LLM 给问题）
  2) 出口：原版 Kafka/MySQL/文件树                          -> out/<目标>.json，跑完退出
  3) 版面定位：去掉硬编码 NGA_GAME_FID 映射表               -> 全站搜游戏名，统计返回帖子的主流 fid（动态）
  4) 热评提取：原版 _extract_hot_replies 在真实 DOM 提 0 条（它找 .ubbcode/.thumbsup，纯 JS 才有）
     -> 改为取 playwright 渲染后 DOM：作者 a.userlink、内容 [id^=postcomment__]、时间 .postdatec、赞 .recommendvalue

queries.txt 每行：
  board|游戏名              -> 动态找主板 fid, 拉主板最新帖(笼统问)
  search|实体词             -> 全站关键词搜(专有词/黑话可用; 泛用词会跨游戏漂移, 优先用下面)
  searchin|游戏名|泛用词    -> 先定游戏主板 fid, 再在版内按词搜(过滤跨游戏噪音; 版内没这词=真没搜到)
"""
import asyncio
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from nga_crawler_playwright import NGACrawlerPlaywright

BASE = 'https://ngabbs.com'
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
SEARCH_N = 10            # search 模式（有指定名词）取前几帖
BOARD_N = 20             # board 模式（笼统问题）取前几帖
HOT_REPLIES_N = 8        # 每帖保留几条热门回复
BOARD_CONF_MIN_HITS = 8          # 动态版面：命中数下限
BOARD_CONF_MIN_RATIO = 0.30      # 动态版面：主流 fid 占比下限
FALLBACK_BOARD_PAGES = 2         # 正文兜底(09-09): 主板最近帖拉几页
FALLBACK_DETAIL_MAX = 15         # 正文兜底: 最多进几帖详情做正文 grep(防 429/慢)
TZ = timezone(timedelta(hours=8))


def bj_now():
    return datetime.now(TZ)


def log(msg):
    print('[%s] %s' % (bj_now().strftime('%H:%M:%S'), msg))


def fmt_unix(ts):
    if not isinstance(ts, (int, float)):
        return ''
    try:
        return datetime.fromtimestamp(ts, TZ).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def to_unix(s):
    """'YYYY-MM-DD[ HH:MM:SS]' -> 北京墙钟 unix 秒; 解析不了回 None。"""
    s = (s or '').strip()
    if not s:
        return None
    for cand, f in ((s[:19], '%Y-%m-%d %H:%M:%S'), (s[:10], '%Y-%m-%d')):
        try:
            return int(datetime.strptime(cand, f).replace(tzinfo=TZ).timestamp())
        except Exception:
            continue
    return None


def _collapse(s):
    return re.sub(r'\n{3,}', '\n\n', s).strip()


class NGASearchDemo(NGACrawlerPlaywright):
    """复用原爬虫浏览器/登录基建；只 override 数据流与解析"""

    def __init__(self, cookies, outdir):
        # 不走 super().__init__：原 init 会开 kafka/MySQL/22 游戏状态文件，demo 不需要
        self.config = {}
        self.cookies = cookies
        self.keywords = []
        self.base_url = BASE
        self.output_dir = outdir

    # ---------- 列表/搜索：thread.php __output=11 JSON（urllib，快） ----------
    def _http_bytes(self, url, retries=4):
        ck = '; '.join('%s=%s' % (k, v) for k, v in self.cookies.items() if v)
        for i in range(retries):
            try:
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                                  ' (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Cookie': ck, 'Referer': BASE + '/', 'Accept-Language': 'zh-CN,zh;q=0.9'})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (302, 403, 429):
                    log('  HTTP %s 退避重试: %s' % (e.code, url))
                    time.sleep(5 + i * 5)
                    continue
                return b''
            except Exception as e:
                log('  fetch fail %s' % e)
                if i < retries - 1:
                    time.sleep(2)
        return b''

    def _thread_data(self, url):
        """thread.php __output=11 -> data 字典(含 __T/__F/__ROWS); 解析失败/410 回 {}。"""
        raw = self._http_bytes(url)
        try:
            d = json.loads(raw.decode('utf-8', 'replace'))
        except Exception:
            return {}
        return d.get('data') or {}

    def _thread_items(self, url):
        t = self._thread_data(url).get('__T') or []
        return t if isinstance(t, list) else []

    def _global_search(self, kw, page=1):
        return self._thread_items('%s/thread.php?key=%s&page=%d&__output=11' % (
            BASE, urllib.parse.quote(kw), page))

    def _board_list(self, fid, page=1):
        return self._thread_items('%s/thread.php?fid=%s&page=%d&__output=11' % (BASE, fid, page))

    def _board_search(self, fid, kw, page=1):
        """版内关键词搜(先定主板 fid 再在此版搜): 实测 NGA 认 fid+key 交集,
        返回帖 fid 全 == fid 且 subject 含词(单短词命中率最高); 词不在版内 -> 空(真没搜到, 不跨界)。"""
        return self._thread_items('%s/thread.php?fid=%s&key=%s&page=%d&__output=11' % (
            BASE, fid, urllib.parse.quote(kw), page))

    @staticmethod
    def _kw_tokens(kw):
        """检索词拆单个 token(空白分隔); 空/全空白 -> []。"""
        toks = [t for t in re.split(r'\s+', (kw or '').strip()) if t]
        return toks

    @staticmethod
    def _keep_item(it, fid):
        """主板帖合法性: 非 admin/#SYSTEM#/#ANONYMOUS#。fid 相等校验仅对正板强制——
        负 fid=版区聚合锚, 版内搜/列表返回帖分属区内各子板(735/734...), 按 fid 相等砍会误删。"""
        if (it.get('author') or '') == 'admin':
            return False
        if it.get('lastposter') in ('#SYSTEM#', '#ANONYMOUS#'):
            return False
        if fid and fid > 0 and it.get('fid') != fid:
            return False
        return True

    def _board_search_tokens(self, fid, kw, limit=SEARCH_N):
        """主板锚定后逐词搜(09-09): kw 拆单个词, 每词各发一次版内 title 搜, 按 tid 合并去重。
        不做多词空格 AND——NGA 版内搜对空格 AND 匹配标题, 多泛词整串几乎必 0;
        单短词(德玛/盖伦/长草)才最可能落标题。已滤 admin/#SYSTEM# 且 fid==主板。"""
        toks = self._kw_tokens(kw) or [kw]
        items, seen = [], set()
        for t in toks:
            for it in self._board_search(fid, t):
                tid = it.get('tid')
                if not tid or tid in seen:
                    continue
                if not self._keep_item(it, fid):
                    continue
                seen.add(tid)
                items.append(it)
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
        return items[:limit]

    # ---------- 时间窗导航（09-11）：翻页到目标时间段 ----------
    # 实测(09-11): thread.php 的版面列表(fid)与版内搜(fid+key)、全站搜(key) 三者的返回页
    # 都按"末回时间"降序 —— 翻页即时间轴(版内搜每页跨 2~4 周, 全站/综合版每页跨数月至数年)。
    # 于是: 想查某历史时段, 不必猜页码范围, 从 p1 逐页往回翻, 页整页早于窗口下界即停;
    # 翻过头 NGA 回 HTTP 410(不是空数组) -> _http_bytes 得 b'' -> _thread_items 回 [] 当到底。
    @staticmethod
    def _in_window(it, lo, hi):
        """帖的发帖时间或末回时间任一落在 [lo, hi] 即算命中(lo/hi 为 unix 秒)。"""
        for k in ('postdate', 'lastpost'):
            try:
                v = int(it.get(k))
            except Exception:
                continue
            if v > 86400 and lo <= v <= hi:
                return True
        return False

    @staticmethod
    def _lp_bounds(items):
        """该页 (最新末回, 最老末回) unix; 无有效值回 None。"""
        vs = []
        for it in items:
            try:
                v = int(it.get('lastpost'))
            except Exception:
                continue
            if v > 86400:
                vs.append(v)
        return (max(vs), min(vs)) if vs else None

    def _page_to_items(self, fetch_page, lo, hi, sink, seen, limit, max_pages, tag,
                       budget_s=120, contains=None):
        """通用翻页导航: p1 最新。整页早于窗口下界 -> 到头停; 与窗口相交 -> 收窗内帖; 空页(410) -> 到底。
        max_pages 与 budget_s 双闸(先到先停)。闸不能小: 版内搜一页只跨 2~4 周, 翻一年要 ~20 页,
        闸小了会在窗口之前被截断 -> 0 命中被误读成"那会儿没讨论"(实测踩过)。
        contains: 只收标题含任一 token 的帖(None=不滤, 全站搜冷门时用游戏名当检索词再按话题词过滤)。"""
        t0 = time.time()
        for p in range(1, max_pages + 1):
            if len(sink) >= limit:
                break
            if time.time() - t0 > budget_s:
                log('  [%s] 翻页耗尽 %ds 预算 -> 停(未确认到头)' % (tag, budget_s))
                self._win_capped = True
                break
            got = fetch_page(p)
            if not got:
                log('  [%s] p=%d 空页/410 -> 到底' % (tag, p))
                break
            b = self._lp_bounds(got)
            if b is None:
                break
            newest, oldest = b
            if newest < lo:
                log('  [%s] p=%d 整页早于窗口(newest=%s) -> 停' % (tag, p, fmt_unix(newest)[:10]))
                break
            hit = 0
            for it in got:
                tid = it.get('tid')
                if not tid or tid in seen or not self._in_window(it, lo, hi):
                    continue
                if contains and not any(t in (it.get('subject') or '') for t in contains):
                    continue
                seen.add(tid)
                sink.append(it)
                hit += 1
                if len(sink) >= limit:
                    break
            log('  [%s] p=%d 窗内 +%d(累计 %d) 页跨度 %s..%s' % (
                tag, p, hit, len(sink), fmt_unix(oldest)[:10], fmt_unix(newest)[:10]))
            time.sleep(1)
        else:
            # 翻满 max_pages 没触发任何停条件 = 窗口可能更深, 没翻到 -> 0 命中不等于"那会儿没讨论"
            log('  [%s] 翻满 %d 页仍未确认到头 -> 停(窗口可能更深; 0 命中≠没讨论)' % (tag, max_pages))
            self._win_capped = True

    def _board_search_window(self, fid, kw, lo, hi, limit=SEARCH_N, max_pages=40):
        """版内搜(fid+key) 翻页到窗口: kw 拆单 token 各翻(不做空格 AND), 按 tid 合并去重。
        max_pages 放到 40(≈翻一年), 由 budget_s 兜时间。"""
        toks = self._kw_tokens(kw) or [kw]
        sink, seen = [], set()
        for t in toks:
            if len(sink) >= limit:
                break
            self._page_to_items(
                lambda p, t=t: [it for it in self._board_search(fid, t, p) if self._keep_item(it, fid)],
                lo, hi, sink, seen, limit, max_pages, 'searchin窗:%s' % t)
        return sink[:limit]

    def _global_search_window(self, kw, lo, hi, limit=SEARCH_N, max_pages=25, contains=None):
        """全站搜(key) 翻页到窗口: 无专属版面(冷门/散落)时用。页跨度粗(数月至数年), 故页数闸小些。"""
        sink, seen = [], set()
        self._page_to_items(
            lambda p: self._global_search(kw, p),
            lo, hi, sink, seen, limit, max_pages, 'search窗', contains=contains)
        return sink[:limit]

    async def _board_body_fallback(self, page, fid, kw, limit=SEARCH_N):
        """主板最近帖正文兜底(09-09, 选项A): searchin 版内逐词 title 搜 0 命中时——
        词(长草/产能/复刻/节奏)常只出现在正文不进标题, title 搜够不到。
        拉主板最近 FALLBACK_BOARD_PAGES 页帖: 标题含词直接收; 其余候选进详情 grep 正文,
        命中才收。进详情数量封顶 FALLBACK_DETAIL_MAX(防 429/慢)。"""
        toks = self._kw_tokens(kw)
        if not toks:
            return []
        cands, seen = [], set()
        for pg in range(1, FALLBACK_BOARD_PAGES + 1):
            for it in self._board_list(fid, pg):
                tid = it.get('tid')
                if not tid or tid in seen:
                    continue
                if not self._keep_item(it, fid):
                    continue
                seen.add(tid)
                cands.append(it)
        # 标题含词直接收(不翻详情; 通常近空, 只因 NGA title 搜索引有滞后才补这层)
        hit = [it for it in cands if any(t in (it.get('subject') or '') for t in toks)]
        title_tids = {it['tid'] for it in hit}
        # 不够再翻详情 grep 正文; 详情抓取封顶
        fetches = 0
        for it in cands:
            if len(hit) >= limit or fetches >= FALLBACK_DETAIL_MAX:
                break
            if it['tid'] in title_tids:
                continue
            fetches += 1
            try:
                main, hot = await self._parse_detail(page, it['tid'])
            except Exception as e:
                log('  [正文兜底] 详情失败 %s: %r' % (it['tid'], e))
                main, hot = '', []
            await page.wait_for_timeout(400)
            if any(t in (main or '') for t in toks):
                it['content'] = main
                it['hot_replies'] = hot
                hit.append(it)
                log('  [正文兜底] %s 正文命中词(%s)' % (it['tid'], kw))
        hit.sort(key=lambda x: 0 if x['tid'] in title_tids else 1)
        return hit[:limit]

    _board_html_cache = {}
    _board_section_cache = {}

    def _board_html(self, fid):
        """板块页 HTML(gbk/utf8), 缓存; 失败回 ''。标题与面包屑复用同一请求。"""
        h = self._board_html_cache.get(fid)
        if h is None:
            raw = self._http_bytes('%s/thread.php?fid=%s' % (BASE, fid))
            h = ''
            if raw:
                # NGA 普通 HTML 页是 GBK 编码(JSON __output=11 才是 utf-8)
                for enc in ('gbk', 'utf-8'):
                    try:
                        h = raw.decode(enc)
                        break
                    except Exception:
                        continue
            self._board_html_cache[fid] = h
        return h

    _forum_cache = {}

    def _board_forum(self, fid):
        """fid -> 版面元信息(来自 __output=11 的 __F: name / sub_forums)。
        与列表同一趟 JSON 拿, 不必再拉 HTML 解 GBK; 顺带拿到子版树。失败回 {}。"""
        if fid not in self._forum_cache:
            d = self._thread_data('%s/thread.php?fid=%s&page=1&__output=11' % (BASE, fid))
            self._forum_cache[fid] = d.get('__F') or {}
        return self._forum_cache[fid]

    def _board_title(self, fid):
        """fid -> 版面标题, 判"是否本游戏专属板"(伞形综合板识别护栏)。优先 __F.name, 失败回退 HTML <title>。"""
        name = (self._board_forum(fid) or {}).get('name')
        if name:
            return re.sub(r'\s+', ' ', name).strip()[:60]
        h = self._board_html(fid)
        if not h:
            return ''
        m = re.search(r'<title>(.*?)</title>', h, re.S)
        return re.sub(r'\s+', ' ', m.group(1)).strip()[:60] if m else ''

    def _section_of(self, fid):
        """fid -> 所属"版区聚合页"(负fid)。读该版面包屑, 取当前版(h1)之前最近的一个负fid上级;
        无则 None。结果缓存。返回 (sec_fid, sec_title)。"""
        if fid in self._board_section_cache:
            return self._board_section_cache[fid]
        h = self._board_html(fid)
        seg = re.search(r"<div class='nav'>(.*?)<div class='clear'>", h or '', re.S)
        nav = seg.group(1) if seg else ''
        res = None
        if nav:
            chain = [(int(f2), re.sub(r'<[^>]+>|\s+', '', t2))
                     for f2, t2 in re.findall(
                         r"<a[^>]+href='/thread\.php\?fid=(-?\d+)'[^>]*>(.*?)</a>", nav, re.S)]
            for f2, t2 in reversed(chain):
                if f2 != fid and f2 < 0:
                    res = (f2, t2)
                    break
        self._board_section_cache[fid] = res
        return res

    # ---------- 动态版面定位（去掉 NGA_GAME_FID 映射表） ----------
    @staticmethod
    def _norm_name(s):
        """板块标题/游戏名归一化: 去空白与中英标点(全角冒号等), 只留汉字字母数字作包含比较。
        例: 崩坏：星穹铁道 == 崩坏星穹铁道。"""
        return re.sub(r'[\s:：;；、,，。.()（）\[\]【】/-]+', '', s or '')

    def _resolve_board_fid(self, game):
        """全站搜游戏名定位"主板/版区搜索锚"。改 2026-09-09(两段式):
          ① 独占正板(标题含游戏名且占比够)优先: 金铲铲510461/终末地846 走这。
             保留 09-08 修正: 只认正 fid(负哨兵会假主板)、多翻页累加、不取榜首(榜首常为
             招募/子版, 要"标题含游戏名"的)、伞形综合板(428等)标题不含游戏名自然排除。
          ② 占比不足 = 多子版游戏: 本体的帖散在各子版、子版标题又不带游戏名
             (明日方舟本体 = 版区 -34587507「罗德岛大使馆」, 下辖 735问答室/734酒吧/805图书馆...),
             全局搜 "明日方舟" 反被 846终末地 稀释到 11/38<0.30 而拒。
             此时从候选板面包屑取"标题含游戏名的负版区聚合页"作锚(版内搜在其下跨子版命中,
             实测 fid=-34587507&key=长草 返回 32 帖)。"""
        hits = []
        for page in (1, 2, 3, 4):
            hits += self._global_search(game, page)
            if len(hits) >= 60:
                break
            time.sleep(1)
        fids = [it.get('fid') for it in hits
                if isinstance(it.get('fid'), (int, float)) and it.get('fid') > 0]
        if len(fids) < BOARD_CONF_MIN_HITS:
            return None
        g_norm = self._norm_name(game)
        top = Counter(fids).most_common(8)
        # ① 独占正板: 版名与游戏名**完全同名**是最强信号, 不受占比门限。
        #    (泛用综合板 428/414 会把真主板在全局搜里的占比稀释到 0.30 之下: 实测"原神"
        #     全局搜 428 占 0.30 > 650 占 0.20, 650 被门限拒 → 误判"无专属主板" → 落全局搜,
        #     而全局搜对活跃板翻不到深窗, 2023 窗口假 0。同名判定一举解掉。)
        #    同名不成, 再退"标题含游戏名且占比够"(老口径)。
        exact = None                     # (fid, count) 版名 == 游戏名
        loose = None                     # (fid, count) 版名 含 游戏名
        for fid, count in top:
            title = self._board_title(fid)
            if not (title and g_norm):
                continue
            nt = self._norm_name(title)
            if nt == g_norm:
                exact = (int(fid), count)
                break
            if loose is None and g_norm in nt:
                loose = (int(fid), count)
        if exact is not None:
            return exact[0]
        if loose is not None and loose[1] / len(fids) >= BOARD_CONF_MIN_RATIO:
            return loose[0]
        # ② 版区聚合锚: 扫描候选板上级负区, 标题含游戏名的按子版帖数加权取最多
        sec = {}
        for fid, count in top:
            parent = self._section_of(int(fid))
            if not parent:
                continue
            sec_fid, sec_title = parent
            if g_norm and g_norm in self._norm_name(sec_title):
                sec[sec_fid] = sec.get(sec_fid, 0) + count
        if sec:
            return max(sec, key=sec.get)
        return None

    # ---------- 详情（playwright 渲染后 DOM，取真实用户名/时间/点赞） ----------
    def _extract_main(self, soup, tid):
        el = soup.select_one('#postcontent0') or soup.select_one('.postcontent')
        if not el:
            return ''
        return _collapse(el.get_text('\n'))

    def _extract_hot_replies_fixed(self, soup):
        out = []
        for ce in soup.select('div.comment_c'):
            au = ce.select_one('a.userlink') or ce.select_one('[id^="postauthor__"]')
            author = ''
            if au:
                badge = au.find('b', class_='block_txt')   # 等级首字徽章，剥掉留纯昵称
                if badge:
                    badge.extract()
                author = au.get_text(strip=True)
            co = ce.select_one('[id^="postcomment__"]')
            if not co:
                continue
            content = co.get_text('\n')
            # 去掉"引用上一楼"的头部：Reply to ... by [xx] (时间)，只留楼主的回应正文
            m = re.search(r'(?s)^\s*Reply\s+to\b.*?\(\d{4}-\d{2}-\d{2}.*?\)\s*', content)
            if m:
                content = content[m.end():]
            content = re.sub(r'(?m)\s*…+\s*\[原帖\]\s*$', '', content)   # 换行隔开的“……[原帖]”后缀
            content = content.replace('[原帖]', '').strip()
            if not content:
                continue
            tm = ce.select_one('span[title="reply time"]') or ce.select_one('.postdatec')
            lk = ce.select_one('.recommendvalue')
            like = re.sub(r'\D', '', lk.get_text(strip=True)) if lk else ''
            out.append({
                'author': author or '未知',
                'content': _collapse(content),
                'time': tm.get_text(strip=True) if tm else '',
                'like': int(like) if like else 0,
            })
        return out[:HOT_REPLIES_N]

    async def _parse_detail(self, page, tid):
        # 用户名/点赞由 postArg.proc 用初始数据就地渲染；实测 domcontentloaded+250ms 即够，
        # networkidle 是过度等待。单 Chrome 会话下 0.4s 间隔连续 12 帖无 302（实测）。
        await page.goto('%s/read.php?tid=%s&page=1' % (BASE, tid),
                        timeout=30000, wait_until='domcontentloaded')
        await page.wait_for_timeout(250)
        soup = BeautifulSoup(await page.content(), 'html.parser')
        return self._extract_main(soup, tid), self._extract_hot_replies_fixed(soup)

    # ---------- 出口：写 out/<目标>.json ----------
    def _save(self, query, mode, target, fid, posts, since=None, until=None, windows=None):
        fname = re.sub(r'[\\/:*?"<>|\s]+', '_', target)[:80] + '.json'
        path = os.path.join(self.output_dir, fname)
        result = {'query': query, 'crawled_at': bj_now().isoformat(),
                  'platform': 'nga', 'mode': mode, 'board_fid': fid,
                  'time_window': {'since': since or '', 'until': until or '',
                                  'windows': windows or []},
                  'window_capped': getattr(self, '_win_capped', False),
                  'posts': posts}
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        log('  已写入 %s (%d 帖)' % (path, len(posts)))
        return result

    @staticmethod
    def _req_windows(windows, since, until):
        """请求时间窗 -> [(lo, hi)] (unix 秒)。分段窗 windows=[[since,until],...] 优先;
        否则按 since/until 组单窗; 都没有 -> []。缺一端分别取 0 / 2^31-1(只给一端也算窗)。"""
        BIG = 2 ** 31 - 1
        out = []
        if isinstance(windows, list):
            for w in windows:
                try:
                    a, b = (w[0] or None), (w[1] or None)
                except Exception:
                    continue
                if not a and not b:
                    continue
                a_s, b_s = to_unix(a), to_unix(b)
                out.append((a_s if a_s is not None else 0,
                            b_s if b_s is not None else BIG))
        if not out:
            a_s, b_s = to_unix(since), to_unix(until)
            if a_s is not None or b_s is not None:
                out.append((a_s if a_s is not None else 0,
                            b_s if b_s is not None else BIG))
        return out

    async def _search_win(self, page, mode, target, lo, hi):
        """单段窗内取数(board/searchin/search 三模式)。返回 (items, fid); fid=None=无专属板/未定。
        与无窗路径的差别: 一切走 *_window(按末回降序翻页到窗口), 不走版面列表 ——
        版面列表按末回排序, 活跃版一页 45 帖只覆盖几小时(实测终末地版翻 12 页才退 6 天), 到不了窗口。"""
        if mode == 'board':
            fid = self._resolve_board_fid(target)
            log('  [board窗] 改用全站搜(游戏名)翻页到窗口%s' % (
                '（主板 fid=%s）' % fid if fid is not None else ''))
            return self._global_search_window(target, lo, hi), fid
        if mode == 'searchin':
            parts = [s.strip() for s in target.split('|', 1)]
            game, kw = parts[0], (parts[1] if len(parts) > 1 else '')
            if game and kw:
                g_fid = self._resolve_board_fid(game)   # 已保证专属(标题含游戏名); None=无专属板
                if g_fid is not None:
                    # 时间窗: 版内搜逐词翻页到窗口, 页内收落窗的帖(不做空格 AND)
                    log('[searchin] %s 主板 fid=%s 窗内逐词翻页' % (game, g_fid))
                    items = self._board_search_window(g_fid, kw, lo, hi)
                    if items:
                        log('  版内窗内命中 %d 帖' % len(items))
                        return items, g_fid
                    # 兜底改两段(09-11): 「游戏名+词」AND 全站搜命中差; 先拿游戏名锚定
                    # 翻到窗内再按话题词滤标题(冷门游戏帖多在标题带游戏名), 两段都空才算真没料。
                    log('  版内窗内无命中 -> 全站搜兜底（游戏名锚定+窗, 滤标题含话题词）')
                    items = self._global_search_window(game, lo, hi, contains=self._kw_tokens(kw))
                    if not items:
                        log('    再试「游戏名+词」AND 全站搜')
                        items = self._global_search_window('%s %s' % (game, kw), lo, hi)
                    return items, g_fid
                # 无专属主板(冷门/散落综合板): 锚定「游戏名+词」全站兜底, 不裸搜词(防跨游戏漂移);
                # fid 保持 None -> 落盘 board_fid=None, 调方识别"无主板兜底": 证据零散, 引用谨慎。
                log('  [searchin] %s 无专属主板, 锚定全站搜 "%s %s"（+窗）' % (game, game, kw))
                items = self._global_search_window(game, lo, hi, contains=self._kw_tokens(kw))
                if not items:
                    items = self._global_search_window('%s %s' % (game, kw), lo, hi)
                return items, None
            if kw:
                log('  [searchin] 缺游戏名, 只能裸搜词(易漂移)')
            return self._global_search_window(kw, lo, hi), None
        return self._global_search_window(target, lo, hi), None

    async def run_query(self, page, line, since=None, until=None, windows=None):
        line = line.strip()
        if not line or line.startswith('#'):
            return
        mode, target = 'search', line
        if '|' in line:
            mode, target = [s.strip() for s in line.split('|', 1)]
            if mode not in ('search', 'board', 'searchin'):
                mode = 'search'
        if not target:
            return None

        # 时间窗: 分段窗(对比题「今年…跟去年…比」)每段各翻一窗再按 tid 合并 —— 合成单窗会因
        # 翻页按末回降序只取到最新一截, 早那段全丢。单窗/无窗走老路, 行为不变。
        wins = self._req_windows(windows, since, until)
        win = bool(wins)
        self._win_capped = False          # 翻页被页数/预算截断(未确认到头) -> 落档标记, 0 命中别当"没讨论"
        if win:
            log('  时间窗 %s' % ' | '.join('%s..%s' % (
                fmt_unix(a)[:10] if a else '-',
                fmt_unix(b)[:10] if b < 2 ** 31 - 1 else '-') for a, b in wins))

        fid = None
        if win and len(wins) > 1:
            items, seen = [], set()
            for wlo, whi in wins:
                sub, f = await self._search_win(page, mode, target, wlo, whi)
                if f is not None:
                    fid = f
                for it in sub:
                    tid = it.get('tid')
                    if tid and tid not in seen:
                        seen.add(tid)
                        items.append(it)
            log('  分段窗合计 %d 帖(去重后)' % len(items))
        elif win:
            items, fid = await self._search_win(page, mode, target, wins[0][0], wins[0][1])
        elif mode == 'board':
            log('[board] %s：动态找版面 fid' % target)
            fid = self._resolve_board_fid(target)
            if fid is not None:
                log('  fid=%s（主流）' % fid)
                items = [it for it in self._board_list(fid)
                         if self._keep_item(it, fid)][:BOARD_N]
                if not items:
                    log('  版面为空，回退为全站搜索热帖')
                    mode = 'search'
                    items = self._dedup(self._global_search(target))[:SEARCH_N]
                    fid = None
            else:
                log('  版面不明确，回退为全站搜索热帖')
                mode = 'search'
                items = self._dedup(self._global_search(target))[:SEARCH_N]
                fid = None
        elif mode == 'searchin':
            parts = [s.strip() for s in target.split('|', 1)]
            game, kw = parts[0], (parts[1] if len(parts) > 1 else '')
            items = []
            if game and kw:
                g_fid = self._resolve_board_fid(game)   # 已保证专属(标题含游戏名); None=无专属板
                if g_fid is not None:
                    # 先定主板 fid, 再单个单个词搜合并(不做空格 AND): 多泛词 AND 标题几乎必 0
                    log('[searchin] %s 主板 fid=%s 逐词单搜' % (game, g_fid))
                    items = self._board_search_tokens(g_fid, kw)
                    fid = g_fid   # 板专属就记 fid(即便词 0 命中): 调方辨"板对但词无帖"
                    if not items:
                        # 词全落正文不在标题(长草/产能/复刻/节奏常如此): 主板最近帖正文 grep 兜底
                        log('  版内逐词 title 无命中 -> 主板最近帖正文兜底(%s)' % kw)
                        items = await self._board_body_fallback(page, g_fid, kw)
                        log('  正文兜底命中 %d 帖' % len(items))
                    else:
                        log('  版内逐词搜合并命中 %d 帖' % len(items))
                else:
                    # 无专属主板(冷门/散落综合板): 锚定「游戏名+词」全站兜底, 不裸搜词(防跨游戏漂移);
                    # fid 保持 None -> 落盘 board_fid=None, 调方识别"无主板兜底": 证据零散, 引用谨慎。
                    log('  [searchin] %s 无专属主板, 锚定全站搜 "%s %s"' % (game, game, kw))
                    items = self._dedup(self._global_search('%s %s' % (game, kw)))[:SEARCH_N]
            elif kw:
                log('  [searchin] 缺游戏名, 只能裸搜词(易漂移)')
                items = self._dedup(self._global_search(kw))[:SEARCH_N]
        else:
            items = self._dedup(self._global_search(target))[:SEARCH_N]

        if not items:
            log('  无结果，跳过')
            return None
        posts = []
        for it in items:
            tid = it.get('tid')
            if not tid:
                continue
            meta = {
                'tid': tid, 'fid': it.get('fid'),
                'title': (it.get('subject') or '').strip(),
                'author': it.get('author') or '',
                'reply_count': it.get('replies', 0),
                'post_time': fmt_unix(it.get('postdate')),
                'url': '%s/read.php?tid=%s' % (BASE, tid),
            }
            if 'content' in it:   # 正文兜底已抓过详情: 直接复用, 不再翻一次
                main, hot = it.get('content') or '', it.get('hot_replies') or []
            else:
                try:
                    main, hot = await self._parse_detail(page, tid)
                except Exception as e:
                    log('  详情失败 %s: %r' % (tid, e))
                    main, hot = '', []
                await page.wait_for_timeout(400 + (tid % 600))   # 帖间防节流间隔（实测 0.4s+ 即可）
            meta['content'] = main
            meta['hot_replies'] = hot
            posts.append(meta)
            log('  帖 %s | 回%s | 热评%s | %s' % (
                tid, meta['reply_count'], len(hot), meta['title'][:36]))
        result = self._save(line, mode, target, fid, posts, since, until, windows)
        return result

    @staticmethod
    def _dedup(items):
        seen, out = set(), []
        for it in items:
            tid = it.get('tid')
            if not tid or tid in seen:
                continue
            seen.add(tid)
            out.append(it)
        return out


async def _amain(cookies, outdir, extra_queries):
    qp = os.path.join(outdir, '..', 'queries.txt')
    lines = []
    with open(qp, encoding='utf-8') as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith('#'):
                lines.append(ln)
    lines += [q for q in (extra_queries or []) if q.strip()]
    log('queries=%d' % len(lines))
    crawler = NGASearchDemo(cookies, outdir)
    async with async_playwright() as p:
        browser = await crawler._launch_browser(p)
        ctx = await browser.new_context()
        await crawler._login(ctx)
        page = await ctx.new_page()
        for ln in lines:
            try:
                await crawler.run_query(page, ln)
            except Exception as e:
                log('  查询出错 %r' % e)
            await page.wait_for_timeout(2000)
        await ctx.close()
        await browser.close()
    log('全部完成')


def main():
    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
    os.makedirs(outdir, exist_ok=True)
    cookies = json.load(open(CONFIG_PATH, encoding='utf-8')).get('cookies', {})
    log('cookie 字段 %d' % len(cookies))
    extra = sys.argv[1:]
    asyncio.run(_amain(cookies, outdir, extra))


if __name__ == '__main__':
    main()
