# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/bilibili/core.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/2 18:44
# @Desc    : Bilibili Crawler

import asyncio
import json
import os
import random
import time
from asyncio import Task
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Union

import pandas as pd

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
    async_playwright,
)
from playwright._impl._errors import TargetClosedError

import config
from base.base_crawler import AbstractCrawler
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import bilibili as bilibili_store
from tools import utils

# 爬虫树根目录(落盘、queries.txt 都挂这儿)。原来这些路径写死成部署目录, 换机器/换目录后
# 就是个不存在的地址: 日志里打出来的落盘路径是假的, 用户照着去找只会扑空。
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.cdp_browser import CDPBrowserManager
from var import crawler_type_var, source_keyword_var

from .client import BilibiliClient
from .exception import DataFetchError
from .field import SearchOrderType
from .help import parse_video_info_from_url, parse_creator_info_from_url
from .login import BilibiliLogin


class BilibiliCrawler(AbstractCrawler):
    context_page: Page
    bili_client: BilibiliClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self):
        self.index_url = "https://www.bilibili.com"
        self.user_agent = utils.get_user_agent()
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh
        self.logged_in = None      # 最近一次登录态探测结果(初始化后有效); /health 报给前端

    async def _init_resources(self, playwright, playwright_proxy_format=None, httpx_proxy_format=None):
        """建 browser_context / context_page / bili_client（含 cookie 登录）。
        从 start() 抽出：batch demo 的 start() 与常驻 HTTP 服务共用同一套初始化，避免两处漂移。"""
        # Choose launch mode based on configuration
        if config.ENABLE_CDP_MODE:
            utils.logger.info("[BilibiliCrawler] Launching browser using CDP mode")
            self.browser_context = await self.launch_browser_with_cdp(
                playwright,
                playwright_proxy_format,
                self.user_agent,
                headless=config.CDP_HEADLESS,
            )
        else:
            utils.logger.info("[BilibiliCrawler] Launching browser using standard mode")
            # Launch a browser context.
            chromium = playwright.chromium
            self.browser_context = await self.launch_browser(chromium, None, self.user_agent, headless=config.HEADLESS)
            # stealth.min.js is a js script to prevent the website from detecting the crawler.
            await self.browser_context.add_init_script(path="libs/stealth.min.js")

        self.context_page = await self.browser_context.new_page()
        await self.context_page.goto(self.index_url)

        # Create a client to interact with the bilibili website.
        self.bili_client = await self.create_bilibili_client(httpx_proxy_format)
        self.logged_in = await self.bili_client.pong()
        if not self.logged_in:
            if config.LOGIN_TYPE == "cookie" and not (config.COOKIES or "").strip():
                # 无登录态(新装/还没扫码)时**不进** cookie 登录那条路: 它会在 check_login_state
                # 上阻塞重试 600 次(约 10 分钟)后才抛错, 服务根本起不来。改成直接以未登录状态启动,
                # 由 /health 报 login=false, 交给前端提示用户扫码。
                utils.logger.warning(
                    "[BilibiliCrawler] 无 COOKIES, 以未登录状态启动; 爬取样本会显著变少")
            else:
                login_obj = BilibiliLogin(
                    login_type=config.LOGIN_TYPE,
                    login_phone="",  # your phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES,
                )
                await login_obj.begin()
                await self.bili_client.update_cookies(browser_context=self.browser_context)
                self.logged_in = await self.bili_client.pong()

    async def start(self):
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            await self._init_resources(
                playwright, playwright_proxy_format, httpx_proxy_format
            )

            crawler_type_var.set(config.CRAWLER_TYPE)
            
            # 实现全量爬取和增量爬取
            if config.CRAWLER_TYPE == "search":
                # demo 模式：读 queries.txt（每行一次搜索），跑完即退
                await self.search_demo()
            elif config.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                await self.get_specified_videos(config.BILI_SPECIFIED_ID_LIST)
            elif config.CRAWLER_TYPE == "creator":
                if config.CREATOR_MODE:
                    for creator_url in config.BILI_CREATOR_ID_LIST:
                        try:
                            creator_info = parse_creator_info_from_url(creator_url)
                            utils.logger.info(f"[BilibiliCrawler.start] Parsed creator ID: {creator_info.creator_id} from {creator_url}")
                            await self.get_creator_videos(int(creator_info.creator_id))
                        except ValueError as e:
                            utils.logger.error(f"[BilibiliCrawler.start] Failed to parse creator URL: {e}")
                            continue
                else:
                    await self.get_all_creator_details(config.BILI_CREATOR_ID_LIST)
            else:
                pass
            utils.logger.info("[BilibiliCrawler.start] Bilibili Crawler finished ...")
    
    async def search_demo(self):
        """
        demo: 读 <爬虫树>/queries.txt（每行 = 一次搜索，行内空格原样保留），
        对每个 query 跑 search_one_query（综合排序前 15 视频 + 每视频前 10 条一级评论），
        结果落盘 <爬虫树>/out/<query>.json，跑完即退。
        """
        query_file = os.path.join(BASE_DIR, "queries.txt")
        if not os.path.exists(query_file):
            utils.logger.error(f"[BilibiliCrawler.search_demo] queries.txt not found: {query_file}")
            return
        with open(query_file, "r", encoding="utf-8") as f:
            queries = [line.strip() for line in f if line.strip()]
        if not queries:
            utils.logger.error("[BilibiliCrawler.search_demo] queries.txt is empty")
            return

        utils.logger.info(f"[BilibiliCrawler.search_demo] total {len(queries)} query(s): {queries}")
        for qi, query in enumerate(queries, 1):
            try:
                await self.search_one_query(query=query, qi=qi)
            except Exception as e:
                utils.logger.error(f"[BilibiliCrawler.search_demo] query failed for '{query}': {e}")

        utils.logger.info("[BilibiliCrawler.search_demo] all done")

    @staticmethod
    async def _req_windows(windows, since=None, until=None) -> List[Tuple[int, int]]:
        """分段窗 -> [(pubtime_begin_s, pubtime_end_s), ...]；无窗 -> []。
        windows 优先；否则 since/until 合成一段。半开(缺一端)时兜底: 起 2009-01-01(b站上线) / 止今天。"""
        def _pair(a, b):
            a = (a or "").strip() or "2009-01-01"
            b = (b or "").strip() or datetime.now().strftime("%Y-%m-%d")
            if a > b:
                a, b = b, a
            return a, b

        cand = []
        if isinstance(windows, list):
            for w in windows:
                try:
                    a, b = (w[0] or None), (w[1] or None)
                except Exception:
                    continue
                if not a and not b:
                    continue
                cand.append(_pair(a, b))
        if not cand and (since or until):
            cand.append(_pair(since, until))

        out = []
        for a, b in cand:
            try:
                b_s, e_s = await BilibiliCrawler.get_pubtime_datetime(start=a, end=b)
                out.append((int(b_s), int(e_s)))
            except Exception as e:
                utils.logger.warning(f"[BilibiliCrawler._req_windows] bad window {a}..{b}: {e}")
        return out

    async def search_one_query(self, query: str, qi: int = 0, max_videos: int = 15,
                               windows: Optional[List] = None, since: Optional[str] = None,
                               until: Optional[str] = None) -> Optional[Dict]:
        """
        demo 单 query 搜索逻辑：综合排序前 15 视频 + 每视频默认排序前 10 条一级评论，
        落盘 out/<query>.json 并返回结果 dict（含 videos）。
        （从 search_demo 批量循环抽出：batch demo 与常驻 HTTP 工具共用同一逻辑源）

        windows=[["2025-01-01","2025-12-31"], ...] 分段窗(对比题「今年 vs 去年」): 每段各按
        发布时间(pubtime_begin_s/end_s)搜一页, max_videos 预算摊到各段, 按 aid 去重合并 ——
        若合成单窗, 配额会被最新一截吃满, 早那段全丢。
        """
        utils.logger.info(f"[BilibiliCrawler.search_one_query] === query {qi}: {query}")
        source_keyword_var.set(query)
        wins = await self._req_windows(windows, since, until)

        result_list: List[Dict] = []
        if wins:
            per = max(2, max_videos // len(wins)) if len(wins) > 1 else max_videos
            seen_aid = set()
            for w_begin, w_end in wins:
                try:
                    search_res = await self.bili_client.search_video_by_keyword(
                        keyword=query, page=1, page_size=20, order=SearchOrderType.DEFAULT,
                        pubtime_begin_s=w_begin, pubtime_end_s=w_end,
                    )
                except Exception as e:
                    utils.logger.error(f"[BilibiliCrawler.search_one_query] search failed for '{query}' win {w_begin}-{w_end}: {e}")
                    # 风控拦截(412/403)且一条都还没捞到: 抛出去 -> 服务端回 risk -> 引擎把 bili 记成
                    # "被风控"并会话冷却。若照旧 continue, 它会装成"真没搜到", 于是不冷却、下一题接着撞。
                    if getattr(e, "risk", False) and not result_list:
                        raise
                    continue
                items = (search_res or {}).get("result") or []
                keep = 0
                for sitem in items[:per]:
                    aid = str(sitem.get("aid") or "")
                    if aid and aid not in seen_aid:
                        seen_aid.add(aid)
                        result_list.append(sitem)
                        keep += 1
                utils.logger.info(f"[BilibiliCrawler.search_one_query] '{query}' win {w_begin}-{w_end} got {len(items)}, keep {keep}")
            picks = result_list
        else:
            try:
                search_res = await self.bili_client.search_video_by_keyword(
                    keyword=query, page=1, page_size=20, order=SearchOrderType.DEFAULT
                )
            except Exception as e:
                utils.logger.error(f"[BilibiliCrawler.search_one_query] search failed for '{query}': {e}")
                if getattr(e, "risk", False):
                    raise                # 风控拦截: 同上, 空结果与"被挡住"是两回事, 别混成一个 None
                return None
            result_list = (search_res or {}).get("result") or []
            picks = result_list[: max_videos]
            utils.logger.info(f"[BilibiliCrawler.search_one_query] '{query}' got {len(result_list)} results, take top {len(picks)}")
        if not picks:
            return None

        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        search_by_aid = {}
        tasks = []
        for sitem in picks:
            search_by_aid[str(sitem.get("aid") or "")] = sitem
            tasks.append(self.get_video_info_task(aid=sitem.get("aid"), bvid="", semaphore=semaphore))
        video_items = await asyncio.gather(*tasks)

        video_docs = []
        for item in video_items:
            if not item:
                continue
            view = item.get("View") or {}
            aid = str(view.get("aid", ""))
            bvid = view.get("bvid", "")
            sitem = search_by_aid.get(aid) or {}
            raw_tags = sitem.get("tags")
            if not raw_tags:
                raw_tags = [t.strip() for t in str(sitem.get("tag") or "").split(",") if t.strip()]
            tags = [t for t in raw_tags if t] if isinstance(raw_tags, list) else []
            stat = view.get("stat") or {}
            doc = {
                "aid": aid,
                "bvid": bvid,
                "tags": tags,
                "title": view.get("title", ""),
                "desc": (view.get("desc") or "")[:200],
                "author": (view.get("owner") or {}).get("name", ""),
                "url": f"https://www.bilibili.com/video/{bvid}" if bvid else f"https://www.bilibili.com/video/av{aid}",
                "publish_time": datetime.fromtimestamp(view.get("pubdate", 0)).strftime("%Y-%m-%d %H:%M:%S") if view.get("pubdate") else "",
                "view_count": stat.get("view", 0),
                "danmaku_count": stat.get("danmaku", 0),
                "reply_count": stat.get("reply", 0),
                "like_count": stat.get("like", 0),
                "comments": [],
            }

            def make_comment_callback(doc_ref):
                async def _cb(video_id, comments):
                    doc_ref["comments"].extend(
                        {
                            "content": (c.get("content") or {}).get("message", ""),
                            "author": ((c.get("member") or {}).get("uname", "")),
                            "rpid": str(c.get("rpid") or ""),
                            "video_bvid": doc_ref.get("bvid", ""),
                            "like": c.get("like", 0),
                            "time": datetime.fromtimestamp(c.get("ctime", 0)).strftime("%Y-%m-%d %H:%M:%S") if c.get("ctime") else "",
                        }
                        for c in (comments or [])
                    )
                return _cb

            try:
                # 只取默认排序第一页前 10 条一级评论（max_count=10 达到即停，不翻页）
                # 单视频评论爬取 30s 封顶: 风控退避(5s/10s/20s)不应拖垮整 query
                await asyncio.wait_for(
                    self.bili_client.get_video_all_comments(
                        video_id=aid,
                        crawl_interval=0.2,
                        is_fetch_sub_comments=False,
                        callback=make_comment_callback(doc),
                        max_count=10,
                    ),
                    timeout=30,
                )
            except Exception as e:
                utils.logger.warning(f"[BilibiliCrawler.search_one_query] comments failed for {aid}: {e}")

            video_docs.append(doc)
            utils.logger.info(f"[BilibiliCrawler.search_one_query]   video {len(video_docs)}: {doc['title']} comments={len(doc['comments'])}")

        result = {"query": query, "crawled_at": datetime.now().isoformat(),
                  "time_windows": [list(w) for w in wins], "videos": video_docs}
        out_root = os.path.join(BASE_DIR, "out")
        os.makedirs(out_root, exist_ok=True)
        safe = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in query)
        if len(safe) > 60:
            safe = safe[:60]
        out_path = os.path.join(out_root, f"{safe}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        utils.logger.info(f"[BilibiliCrawler.search_one_query] saved: {out_path}")
        return result

    async def save_crawl_status(self, current_keyword=None, current_page=None, videos_crawled=0, current_order=None, status="completed"):
        """
        保存爬取状态和上次爬取时间
        """
        # 构建目录结构
        base_dir = config.SAVE_DATA_PATH + "/bili"
        os.makedirs(base_dir, exist_ok=True)
        
        # 保存爬取状态
        status_file = os.path.join(base_dir, "crawler_status.json")
        status_data = {
            "last_crawl_time": datetime.now().isoformat(),
            "status": status,
            "current_keyword": current_keyword,
            "current_page": current_page,
            "videos_crawled": videos_crawled,
            "current_order": current_order.value if current_order else None,
            "first_last_publish_video_id": getattr(self, "_first_last_publish_video_id", None),
            "timestamp": int(datetime.now().timestamp())
        }
        with open(status_file, 'w', encoding='utf-8') as f:
            json.dump(status_data, f, ensure_ascii=False, indent=2)
        
        # 保存上次爬取时间
        time_file = os.path.join(base_dir, "last_crawl_time.json")
        time_data = {
            "timestamp": int(datetime.now().timestamp()),
            "datetime": datetime.now().isoformat()
        }
        with open(time_file, 'w', encoding='utf-8') as f:
            json.dump(time_data, f, ensure_ascii=False, indent=2)
        
        utils.logger.info(f"[BilibiliCrawler.save_crawl_status] Crawl status saved successfully: keyword={current_keyword}, page={current_page}, videos_crawled={videos_crawled}, order={current_order.value if current_order else 'None'}, status={status}")

    def load_last_crawl_end_time(self):
        """
        加载上次爬取结束时间
        """
        base_dir = config.SAVE_DATA_PATH + "/bili"
        time_file = os.path.join(base_dir, "last_crawl_time.json")
        
        if not os.path.exists(time_file):
            utils.logger.info("[BilibiliCrawler.load_last_crawl_end_time] No last crawl time file found, starting fresh")
            return None
        
        try:
            with open(time_file, 'r', encoding='utf-8') as f:
                time_data = json.load(f)
            timestamp = time_data.get("timestamp")
            if timestamp:
                last_crawl_time = datetime.fromtimestamp(timestamp)
                utils.logger.info(f"[BilibiliCrawler.load_last_crawl_end_time] Last crawl time loaded: {last_crawl_time}")
                return last_crawl_time
        except Exception as e:
            utils.logger.error(f"[BilibiliCrawler.load_last_crawl_end_time] Error loading last crawl time: {e}")
        return None

    async def load_crawl_status(self):
        """
        加载爬取状态
        """
        # 构建目录结构
        base_dir = config.SAVE_DATA_PATH + "/bili"
        status_file = os.path.join(base_dir, "crawler_status.json")
        
        if not os.path.exists(status_file):
            utils.logger.info("[BilibiliCrawler.load_crawl_status] No crawl status file found, starting fresh")
            return None
        
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                status_data = json.load(f)
            utils.logger.info(f"[BilibiliCrawler.load_crawl_status] Crawl status loaded successfully: {status_data}")
            return status_data
        except Exception as e:
            utils.logger.error(f"[BilibiliCrawler.load_crawl_status] Error loading crawl status: {e}")
            return None

    async def search(self):
        """
        search bilibili video
        """
        # Search for video and retrieve their comment information.
        if config.BILI_SEARCH_MODE == "normal":
            await self.search_by_keywords()
        elif config.BILI_SEARCH_MODE == "all_in_time_range":
            await self.search_by_keywords_in_time_range(daily_limit=False)
        elif config.BILI_SEARCH_MODE == "daily_limit_in_time_range":
            await self.search_by_keywords_in_time_range(daily_limit=True)
        else:
            utils.logger.warning(f"Unknown BILI_SEARCH_MODE: {config.BILI_SEARCH_MODE}")

    @staticmethod
    async def get_pubtime_datetime(
        start: str = config.START_DAY,
        end: str = config.END_DAY,
    ) -> Tuple[str, str]:
        """
        Get bilibili publish start timestamp pubtime_begin_s and publish end timestamp pubtime_end_s
        ---
        :param start: Publish date start time, YYYY-MM-DD
        :param end: Publish date end time, YYYY-MM-DD

        Note
        ---
        - Search time range is from start to end, including both start and end
        - To search content from the same day, to include search content from that day, pubtime_end_s should be pubtime_begin_s plus one day minus one second, i.e., the last second of start day
            - For example, searching only 2024-01-05 content, pubtime_begin_s = 1704384000, pubtime_end_s = 1704470399
              Converted to readable datetime objects: pubtime_begin_s = datetime.datetime(2024, 1, 5, 0, 0), pubtime_end_s = datetime.datetime(2024, 1, 5, 23, 59, 59)
        - To search content from start to end, to include search content from end day, pubtime_end_s should be pubtime_end_s plus one day minus one second, i.e., the last second of end day
            - For example, searching 2024-01-05 - 2024-01-06 content, pubtime_begin_s = 1704384000, pubtime_end_s = 1704556799
              Converted to readable datetime objects: pubtime_begin_s = datetime.datetime(2024, 1, 5, 0, 0), pubtime_end_s = datetime.datetime(2024, 1, 6, 23, 59, 59)
        """
        # Convert start and end to datetime objects
        start_day: datetime = datetime.strptime(start, "%Y-%m-%d")
        end_day: datetime = datetime.strptime(end, "%Y-%m-%d")
        if start_day > end_day:
            raise ValueError("Wrong time range, please check your start and end argument, to ensure that the start cannot exceed end")
        elif start_day == end_day:  # Searching content from the same day
            end_day = (start_day + timedelta(days=1) - timedelta(seconds=1))  # Set end_day to start_day + 1 day - 1 second
        else:  # Searching from start to end
            end_day = (end_day + timedelta(days=1) - timedelta(seconds=1))  # Set end_day to end_day + 1 day - 1 second
        # Convert back to timestamps
        return str(int(start_day.timestamp())), str(int(end_day.timestamp()))

    async def search_by_keywords(self):
        """
        search bilibili video with keywords in normal mode (continuous incremental crawl)
        :return:
        """
        BILI_PAGE_SIZE = 20  # bilibili limit page fixed value
        # 指定的keyword列表
        keywords = ["手机游戏", "王者荣耀", "和平精英", "原神", "崩坏星穹铁道", "绝区零", "明日方舟", "崩坏三", "天涯明月刀", "无限暖暖", "英雄联盟手游", "金铲铲之战", "明日方舟终末地", "三角洲行动", "火影忍者手游", "燕云十六声", "逆水寒手游", "永劫无间手游", "光遇", "第五人格", "阴阳师", "鸣潮"]
        # 每个keyword爬取300个视频
        videos_per_keyword = 20
        # B站搜索API最多返回28页
        max_pages = 1
        
        # 连续爬取循环
        while True:
            cycle_start_time = datetime.now()
            utils.logger.info(f"\n========== [BilibiliCrawler.search_by_keywords] Begin new crawl cycle at {cycle_start_time} ==========")
            
            try:
                # 标记开始爬取
                await self.save_crawl_status(status="running")
                
                for i, keyword in enumerate(keywords):
                    source_keyword_var.set(keyword)
                    utils.logger.info(f"[BilibiliCrawler.search_by_keywords] Current search keyword: {keyword} (index: {i})")
                    
                    # 爬取最新发布排序视频（最近2小时）
                    utils.logger.info(f"[BilibiliCrawler.search_by_keywords] Incremental crawl: Searching for latest publish order videos for keyword: {keyword}")
                    result = await self._crawl_videos(
                        keyword=keyword,
                        order=SearchOrderType.LAST_PUBLISH,  # 最新发布排序
                        videos_per_keyword=videos_per_keyword,
                        start_page=1,
                        min_timestamp_factor=0,  # 不限制下限
                        max_timestamp_factor=2,  # 小于2小时
                        min_play_count=-1,  # 不限制播放量
                        record_first_video=True  # 记录第一个视频ID
                    )
                    
                    # 添加10-15秒的随机延迟，避免被检测
                    keyword_sleep_time = random.uniform(10, 15)
                    await asyncio.sleep(keyword_sleep_time)
                    utils.logger.info(f"[BilibiliCrawler.search_by_keywords] Sleeping for {keyword_sleep_time:.2f} seconds after keyword: {keyword}")
                
                # 本轮爬取完成，重置状态
                await self.save_crawl_status(None, 1, 0, status="completed")
                
                cycle_end_time = datetime.now()
                utils.logger.info(f"[BilibiliCrawler.search_by_keywords] Crawl cycle completed in {cycle_end_time - cycle_start_time}")
                
                # 直接进行下一轮爬取，不等待
                utils.logger.info("[BilibiliCrawler.search_by_keywords] Starting next crawl cycle immediately...")
                
            except Exception as e:
                utils.logger.error(f"[BilibiliCrawler.search_by_keywords] Error occurred during crawl: {e}")
                # 发生错误时保存状态为error
                await self.save_crawl_status(
                    current_keyword=keyword if 'keyword' in locals() else None,
                    current_page=1,
                    status="error"
                )
                raise
    
    async def _crawl_videos(self, keyword, order, videos_per_keyword, start_page, min_timestamp_factor, max_timestamp_factor, min_play_count, record_first_video=False):
        """
        内部方法：爬取视频
        :param keyword: 搜索关键词
        :param order: 排序方式
        :param videos_per_keyword: 每个关键词爬取的视频数量
        :param start_page: 开始页面
        :param min_timestamp_factor: 最小时间因子（小时）
        :param max_timestamp_factor: 最大时间因子（小时），0表示不限制
        :param min_play_count: 最小播放量
        :param record_first_video: 是否记录第一个视频ID（用于最新发布排序）
        :return: 爬取结果字典
        """
        BILI_PAGE_SIZE = 20
        page = 1
        videos_crawled = 0
        old_videos_count = 0
        max_pages = 1  # B站搜索API最多返回28页
        
        # 计算时间范围
        current_time = int(time.time())
        # 发布日期大于爬虫运行时指定小时数
        min_timestamp = current_time - min_timestamp_factor * 60 * 60
        # 发布日期小于指定小时数（如果有）
        if max_timestamp_factor > 0:
            max_timestamp = current_time - max_timestamp_factor * 60 * 60
        else:
            max_timestamp = 0
        
        # 初始化第一个视频ID（用于增量爬取时停止）
        first_video_id = None
        # 加载之前记录的第一个视频ID（用于增量爬取）
        if not record_first_video:
            crawl_status = await self.load_crawl_status()
            first_video_id = crawl_status.get("first_last_publish_video_id") if crawl_status else None
        
        utils.logger.info(f"[BilibiliCrawler._crawl_videos] Crawling videos for keyword: {keyword}, order: {order.value}, min_time: {min_timestamp_factor}h, max_time: {'unlimited' if max_timestamp_factor == 0 else f'{max_timestamp_factor}h'}, min_play: {min_play_count}")
        if first_video_id:
            utils.logger.info(f"[BilibiliCrawler._crawl_videos] Incremental crawl will stop when video {first_video_id} is encountered")
        
        # 加载上次爬取结束时间（增量爬取）
        last_crawl_end_time = self.load_last_crawl_end_time()
        if last_crawl_end_time:
            utils.logger.info(f"[BilibiliCrawler._crawl_videos] Incremental crawl: will crawl videos after {last_crawl_end_time}")
        else:
            utils.logger.info(f"[BilibiliCrawler._crawl_videos] First crawl: will crawl videos in last 12 hours")
        
        while videos_crawled < videos_per_keyword and page <= max_pages:
            if page < start_page:
                utils.logger.info(f"[BilibiliCrawler._crawl_videos] Skip page: {page}")
                page += 1
                continue

            utils.logger.info(f"[BilibiliCrawler._crawl_videos] search bilibili keyword: {keyword}, page: {page}, order: {order.value}")
            video_id_list: List[str] = []
            import gc
            
            # 设置时间范围：使用循环前加载的上次结束时间
            if last_crawl_end_time:
                pubtime_begin_s = int(last_crawl_end_time.timestamp())
            else:
                pubtime_begin_s = int(time.time()) - 12 * 60 * 60  # 首次爬取，爬取最近12小时
            pubtime_end_s = int(time.time())  # 当前时间
            
            videos_res = await self.bili_client.search_video_by_keyword(
                keyword=keyword,
                page=page,
                page_size=BILI_PAGE_SIZE,
                order=order,
                pubtime_begin_s=pubtime_begin_s,
                pubtime_end_s=pubtime_end_s,
            )
            video_list: List[Dict] = videos_res.get("result")

            if not video_list:
                utils.logger.info(f"[BilibiliCrawler._crawl_videos] No more videos for '{keyword}', moving to next keyword.")
                break

            semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
            task_list = []
            try:
                task_list = [self.get_video_info_task(aid=video_item.get("aid"), bvid="", semaphore=semaphore) for video_item in video_list]
            except Exception as e:
                utils.logger.warning(f"[BilibiliCrawler._crawl_videos] error in the task list. The video for this page will not be included. {e}")
            video_items = await asyncio.gather(*task_list)
            
            for video_item in video_items:
                if video_item:
                    view_data = video_item.get("View")
                    # 检查播放量和发布时间
                    play_count = view_data.get("stat", {}).get("view", 0)
                    create_time = view_data.get("pubdate", 0)
                    
                    # 检查条件
                    if min_timestamp_factor > 0:
                        time_condition = create_time > min_timestamp
                        if max_timestamp > 0:
                            time_condition = time_condition and create_time < max_timestamp
                    else:
                        # 不限制时间
                        time_condition = True
                    
                    play_condition = play_count > min_play_count
                    
                    if time_condition and play_condition:
                        # 符合条件的视频
                        video_id = view_data.get("aid")
                        
                        # 检查增量爬取停止条件：如果遇到第一次爬取的第一个视频就停止
                        if first_video_id and video_id == first_video_id:
                            utils.logger.info(f"[BilibiliCrawler._crawl_videos] Encountered first video {video_id} from previous crawl, stopping incremental crawl")
                            # 清空video_id_list以避免处理已爬过的视频
                            video_id_list = []
                            break
                        
                        video_id_list.append(video_id)
                        # 打印视频链接和播放量
                        video_url = f"https://www.bilibili.com/video/av{video_id}"
                        utils.logger.info(f"[BilibiliCrawler._crawl_videos] Found video: {video_url}, 播放量: {play_count}, 排序: {order.value}")
                        await bilibili_store.update_bilibili_video(video_item)
                        videos_crawled += 1
                        old_videos_count = 0
                        
                        # 记录第一个视频ID（仅在第一次爬取最新发布排序时）
                        if record_first_video and videos_crawled == 1:
                            self._first_last_publish_video_id = video_id
                            utils.logger.info(f"[BilibiliCrawler._crawl_videos] Recorded first video ID: {video_id} for incremental crawl stop condition")
                            # 保存第一个视频ID到状态
                            await self.save_crawl_status(keyword, page, videos_crawled, order)
                        else:
                            # 保存爬取状态
                            await self.save_crawl_status(keyword, page, videos_crawled, order)
                        
                        if videos_crawled >= videos_per_keyword:
                            break
                    elif min_timestamp_factor > 0 and create_time < min_timestamp:
                        # 发布时间小于指定时间的视频，不增加old_videos_count
                        utils.logger.info(f"[BilibiliCrawler._crawl_videos] Video {view_data.get('aid')} published within {min_timestamp_factor} hours, continuing to next video.")
                    else:
                        # 发布时间超过限制的视频，增加old_videos_count
                        old_videos_count += 1
                        if old_videos_count >= 50:
                            utils.logger.info(f"[BilibiliCrawler._crawl_videos] Too many old videos, moving to next keyword.")
                            break
            
            if old_videos_count >= 50:
                break
            
            page += 1

            # 添加3-4s随机延迟
            sleep_time = random.uniform(3, 4)
            await asyncio.sleep(sleep_time)
            utils.logger.info(f"[BilibiliCrawler._crawl_videos] Sleeping for {sleep_time:.2f} seconds after page {page-1}")

            if video_id_list:
                await self.batch_get_video_comments(video_id_list)
                # 限制video_id_list长度，避免内存累积
                if len(video_id_list) > 100:
                    video_id_list = video_id_list[-100:]
            
            # 显式清理内存
            del videos_res
            del video_list
            del task_list
            del video_items
            gc.collect()
            utils.logger.info(f"[BilibiliCrawler._crawl_videos] Memory cleaned after page {page}")
            
            # 保存爬取状态
            await self.save_crawl_status(keyword, page, videos_crawled, order)
        
        # 返回爬取结果
        return {
            "videos_crawled": videos_crawled,
            "page": page,
            "keyword": keyword,
            "order": order.value
        }

    async def search_by_keywords_in_time_range(self, daily_limit: bool):
        """
        Search bilibili video with keywords in a given time range.
        :param daily_limit: if True, strictly limit the number of notes per day and total.
        """
        utils.logger.info(f"[BilibiliCrawler.search_by_keywords_in_time_range] Begin search with daily_limit={daily_limit}")
        bili_limit_count = 20
        start_page = config.START_PAGE

        for keyword in config.KEYWORDS.split(","):
            source_keyword_var.set(keyword)
            utils.logger.info(f"[BilibiliCrawler.search_by_keywords_in_time_range] Current search keyword: {keyword}")
            total_notes_crawled_for_keyword = 0

            for day in pd.date_range(start=config.START_DAY, end=config.END_DAY, freq="D"):
                if (daily_limit and total_notes_crawled_for_keyword >= config.CRAWLER_MAX_NOTES_COUNT):
                    utils.logger.info(f"[BilibiliCrawler.search] Reached CRAWLER_MAX_NOTES_COUNT limit for keyword '{keyword}', skipping remaining days.")
                    break

                if (not daily_limit and total_notes_crawled_for_keyword >= config.CRAWLER_MAX_NOTES_COUNT):
                    utils.logger.info(f"[BilibiliCrawler.search] Reached CRAWLER_MAX_NOTES_COUNT limit for keyword '{keyword}', skipping remaining days.")
                    break

                pubtime_begin_s, pubtime_end_s = await self.get_pubtime_datetime(start=day.strftime("%Y-%m-%d"), end=day.strftime("%Y-%m-%d"))
                page = 1
                notes_count_this_day = 0

                while True:
                    if notes_count_this_day >= config.MAX_NOTES_PER_DAY:
                        utils.logger.info(f"[BilibiliCrawler.search] Reached MAX_NOTES_PER_DAY limit for {day.ctime()}.")
                        break
                    if (daily_limit and total_notes_crawled_for_keyword >= config.CRAWLER_MAX_NOTES_COUNT):
                        utils.logger.info(f"[BilibiliCrawler.search] Reached CRAWLER_MAX_NOTES_COUNT limit for keyword '{keyword}'.")
                        break
                    if (not daily_limit and total_notes_crawled_for_keyword >= config.CRAWLER_MAX_NOTES_COUNT):
                        break

                    try:
                        utils.logger.info(f"[BilibiliCrawler.search] search bilibili keyword: {keyword}, date: {day.ctime()}, page: {page}")
                        video_id_list: List[str] = []
                        videos_res = await self.bili_client.search_video_by_keyword(
                            keyword=keyword,
                            page=page,
                            page_size=bili_limit_count,
                            order=SearchOrderType.DEFAULT,
                            pubtime_begin_s=pubtime_begin_s,
                            pubtime_end_s=pubtime_end_s,
                        )
                        video_list: List[Dict] = videos_res.get("result")

                        if not video_list:
                            utils.logger.info(f"[BilibiliCrawler.search] No more videos for '{keyword}' on {day.ctime()}, moving to next day.")
                            break

                        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                        task_list = [self.get_video_info_task(aid=video_item.get("aid"), bvid="", semaphore=semaphore) for video_item in video_list]
                        video_items = await asyncio.gather(*task_list)

                        for video_item in video_items:
                            if video_item:
                                if (daily_limit and total_notes_crawled_for_keyword >= config.CRAWLER_MAX_NOTES_COUNT):
                                    break
                                if (not daily_limit and total_notes_crawled_for_keyword >= config.CRAWLER_MAX_NOTES_COUNT):
                                    break
                                if notes_count_this_day >= config.MAX_NOTES_PER_DAY:
                                    break
                                notes_count_this_day += 1
                                total_notes_crawled_for_keyword += 1
                                video_id_list.append(video_item.get("View").get("aid"))
                                await bilibili_store.update_bilibili_video(video_item)
                                await bilibili_store.update_up_info(video_item)
                                await self.get_bilibili_video(video_item, semaphore)

                        page += 1

                        # Sleep after page navigation
                        await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                        utils.logger.info(f"[BilibiliCrawler.search_by_keywords_in_time_range] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after page {page-1}")

                        await self.batch_get_video_comments(video_id_list)

                    except Exception as e:
                        utils.logger.error(f"[BilibiliCrawler.search] Error searching on {day.ctime()}: {e}")
                        break

    async def batch_get_video_comments(self, video_id_list: List[str]):
        """
        batch get video comments
        :param video_id_list:
        :return:
        """
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info(f"[BilibiliCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        utils.logger.info(f"[BilibiliCrawler.batch_get_video_comments] video ids:{video_id_list}")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for video_id in video_id_list:
            task = asyncio.create_task(self.get_comments(video_id, semaphore), name=video_id)
            task_list.append(task)
        await asyncio.gather(*task_list)
        
        # 显式清理内存
        del task_list
        import gc
        gc.collect()
        utils.logger.info(f"[BilibiliCrawler.batch_get_video_comments] Memory cleaned after processing {len(video_id_list)} videos")

    async def get_comments(self, video_id: str, semaphore: asyncio.Semaphore):
        """
        get comment for video id
        :param video_id:
        :param semaphore:
        :return:
        """
        async with semaphore:
            try:
                utils.logger.info(f"[BilibiliCrawler.get_comments] begin get video_id: {video_id} comments ...")
                # 添加3-4s随机延迟
                sleep_time = random.uniform(3, 4)
                await asyncio.sleep(sleep_time)
                utils.logger.info(f"[BilibiliCrawler.get_comments] Sleeping for {sleep_time:.2f} seconds after fetching comments for video {video_id}")
                
                # 评论抓取限制条件：原按"点赞数>1"过滤 —— 按需求已注释不使用，取评论原文
                async def filtered_comment_callback(video_id, comments):
                    if comments:
                        await bilibili_store.batch_update_bilibili_video_comments(video_id, comments)
                
                await self.bili_client.get_video_all_comments(
                    video_id=video_id,
                    crawl_interval=sleep_time,
                    is_fetch_sub_comments=config.ENABLE_GET_SUB_COMMENTS,
                    callback=filtered_comment_callback,
                    max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
                )

            except DataFetchError as ex:
                utils.logger.error(f"[BilibiliCrawler.get_comments] get video_id: {video_id} comment error: {ex}")
            except Exception as e:
                utils.logger.error(f"[BilibiliCrawler.get_comments] may be been blocked, err:{e}")
                # Propagate the exception to be caught by the main loop
                raise

    async def get_creator_videos(self, creator_id: int):
        """
        get videos for a creator
        :return:
        """
        ps = 30
        pn = 1
        while True:
            result = await self.bili_client.get_creator_videos(creator_id, pn, ps)
            video_bvids_list = [video["bvid"] for video in result["list"]["vlist"]]
            await self.get_specified_videos(video_bvids_list)
            if int(result["page"]["count"]) <= pn * ps:
                break
            await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
            utils.logger.info(f"[BilibiliCrawler.get_creator_videos] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after page {pn}")
            pn += 1

    async def get_specified_videos(self, video_url_list: List[str]):
        """
        get specified videos info from URLs or BV IDs
        :param video_url_list: List of video URLs or BV IDs
        :return:
        """
        utils.logger.info("[BilibiliCrawler.get_specified_videos] Parsing video URLs...")
        bvids_list = []
        for video_url in video_url_list:
            try:
                video_info = parse_video_info_from_url(video_url)
                bvids_list.append(video_info.video_id)
                utils.logger.info(f"[BilibiliCrawler.get_specified_videos] Parsed video ID: {video_info.video_id} from {video_url}")
            except ValueError as e:
                utils.logger.error(f"[BilibiliCrawler.get_specified_videos] Failed to parse video URL: {e}")
                continue

        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [self.get_video_info_task(aid=0, bvid=video_id, semaphore=semaphore) for video_id in bvids_list]
        video_details = await asyncio.gather(*task_list)
        video_aids_list = []
        for video_detail in video_details:
            if video_detail is not None:
                video_item_view: Dict = video_detail.get("View")
                video_aid: str = video_item_view.get("aid")
                if video_aid:
                    video_aids_list.append(video_aid)
                await bilibili_store.update_bilibili_video(video_detail)
                await bilibili_store.update_up_info(video_detail)
                await self.get_bilibili_video(video_detail, semaphore)
        await self.batch_get_video_comments(video_aids_list)

    async def get_video_info_task(self, aid: int, bvid: str, semaphore: asyncio.Semaphore) -> Optional[Dict]:
        """
        Get video detail task
        :param aid:
        :param bvid:
        :param semaphore:
        :return:
        """
        async with semaphore:
            try:
                result = await self.bili_client.get_video_info(aid=aid, bvid=bvid)

                # 提取视频信息并打印链接、播放量和标题
                if result:
                    view_data = result.get("View")
                    if view_data:
                        video_id = view_data.get("aid")
                        play_count = view_data.get("stat", {}).get("view", 0)
                        title = view_data.get("title", "")
                        video_url = f"https://www.bilibili.com/video/av{video_id}"
                        utils.logger.info(f"[BilibiliCrawler.get_video_info_task] Video URL: {video_url}, 标题: {title}, 播放量: {play_count}")

                # 添加1-1.5s随机延迟
                sleep_time = random.uniform(1, 1.5)
                await asyncio.sleep(sleep_time)
                utils.logger.info(f"[BilibiliCrawler.get_video_info_task] Sleeping for {sleep_time:.2f} seconds after fetching video details {bvid or aid}")

                return result
            except DataFetchError as ex:
                utils.logger.error(f"[BilibiliCrawler.get_video_info_task] Get video detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[BilibiliCrawler.get_video_info_task] have not fund note detail video_id:{bvid}, err: {ex}")
                return None

    async def get_video_play_url_task(self, aid: int, cid: int, semaphore: asyncio.Semaphore) -> Union[Dict, None]:
        """
        Get video play url
        :param aid:
        :param cid:
        :param semaphore:
        :return:
        """
        async with semaphore:
            try:
                result = await self.bili_client.get_video_play_url(aid=aid, cid=cid)
                return result
            except DataFetchError as ex:
                utils.logger.error(f"[BilibiliCrawler.get_video_play_url_task] Get video play url error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[BilibiliCrawler.get_video_play_url_task] have not fund play url from :{aid}|{cid}, err: {ex}")
                return None

    async def create_bilibili_client(self, httpx_proxy: Optional[str]) -> BilibiliClient:
        """
        create bilibili client
        :param httpx_proxy: httpx proxy
        :return: bilibili client
        """
        utils.logger.info("[BilibiliCrawler.create_bilibili_client] Begin create bilibili API client ...")
        cookie_str, cookie_dict = utils.convert_cookies(await self.browser_context.cookies())
        bilibili_client_obj = BilibiliClient(
            proxy=httpx_proxy,
            headers={
                "User-Agent": self.user_agent,
                "Cookie": cookie_str,
                "Origin": "https://www.bilibili.com",
                "Referer": "https://www.bilibili.com",
                "Content-Type": "application/json;charset=UTF-8",
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
        )
        return bilibili_client_obj

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """
        launch browser and create browser context
        :param chromium: chromium browser
        :param playwright_proxy: playwright proxy
        :param user_agent: user agent
        :param headless: headless mode
        :return: browser context
        """
        utils.logger.info("[BilibiliCrawler.launch_browser] Begin create browser context ...")
        if config.SAVE_LOGIN_STATE:
            # feat issue #14
            # we will save login state to avoid login every time
            user_data_dir = os.path.join(os.getcwd(), "browser_data", config.USER_DATA_DIR % config.PLATFORM)  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,  # type: ignore
                viewport={
                    "width": 1920,
                    "height": 1080
                },
                user_agent=user_agent,
                channel="chrome",  # Use system's stable Chrome version
            )
            return browser_context
        else:
            # type: ignore
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy, channel="chrome")
            browser_context = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=user_agent)
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """
        Launch browser using CDP mode
        """
        try:
            self.cdp_manager = CDPBrowserManager()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            # Display browser information
            browser_info = await self.cdp_manager.get_browser_info()
            utils.logger.info(f"[BilibiliCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[BilibiliCrawler] CDP mode launch failed, fallback to standard mode: {e}")
            # Fallback to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

    async def close(self):
        """Close browser context"""
        try:
            # If using CDP mode, special handling is required
            if self.cdp_manager:
                await self.cdp_manager.cleanup()
                self.cdp_manager = None
            elif self.browser_context:
                await self.browser_context.close()
                self.browser_context = None
            if hasattr(self, 'context_page'):
                self.context_page = None
            utils.logger.info("[BilibiliCrawler.close] Browser context closed ...")
        except TargetClosedError:
            utils.logger.warning("[BilibiliCrawler.close] Browser context was already closed.")
        except Exception as e:
            utils.logger.error(f"[BilibiliCrawler.close] An error occurred during close: {e}")
    
    async def __aenter__(self):
        """Async context manager enter"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()

    async def get_bilibili_video(self, video_item: Dict, semaphore: asyncio.Semaphore):
        """
        download bilibili video
        :param video_item:
        :param semaphore:
        :return:
        """
        if not config.ENABLE_GET_MEIDAS:
            utils.logger.info(f"[BilibiliCrawler.get_bilibili_video] Crawling image mode is not enabled")
            return
        video_item_view: Dict = video_item.get("View")
        aid = video_item_view.get("aid")
        cid = video_item_view.get("cid")
        result = await self.get_video_play_url_task(aid, cid, semaphore)
        if result is None:
            utils.logger.info("[BilibiliCrawler.get_bilibili_video] get video play url failed")
            return
        durl_list = result.get("durl")
        max_size = -1
        video_url = ""
        for durl in durl_list:
            size = durl.get("size")
            if size > max_size:
                max_size = size
                video_url = durl.get("url")
        if video_url == "":
            utils.logger.info("[BilibiliCrawler.get_bilibili_video] get video url failed")
            return

        content = await self.bili_client.get_video_media(video_url)
        await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
        utils.logger.info(f"[BilibiliCrawler.get_bilibili_video] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching video {aid}")
        if content is None:
            return
        extension_file_name = f"video.mp4"
        await bilibili_store.store_video(aid, content, extension_file_name)

    async def get_all_creator_details(self, creator_url_list: List[str]):
        """
        creator_url_list: get details for creator from creator URL list
        """
        utils.logger.info(f"[BilibiliCrawler.get_all_creator_details] Crawling the details of creators")
        utils.logger.info(f"[BilibiliCrawler.get_all_creator_details] Parsing creator URLs...")

        creator_id_list = []
        for creator_url in creator_url_list:
            try:
                creator_info = parse_creator_info_from_url(creator_url)
                creator_id_list.append(int(creator_info.creator_id))
                utils.logger.info(f"[BilibiliCrawler.get_all_creator_details] Parsed creator ID: {creator_info.creator_id} from {creator_url}")
            except ValueError as e:
                utils.logger.error(f"[BilibiliCrawler.get_all_creator_details] Failed to parse creator URL: {e}")
                continue

        utils.logger.info(f"[BilibiliCrawler.get_all_creator_details] creator ids:{creator_id_list}")

        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        try:
            for creator_id in creator_id_list:
                task = asyncio.create_task(self.get_creator_details(creator_id, semaphore), name=str(creator_id))
                task_list.append(task)
        except Exception as e:
            utils.logger.warning(f"[BilibiliCrawler.get_all_creator_details] error in the task list. The creator will not be included. {e}")

        await asyncio.gather(*task_list)

    async def get_creator_details(self, creator_id: int, semaphore: asyncio.Semaphore):
        """
        get details for creator id
        :param creator_id:
        :param semaphore:
        :return:
        """
        async with semaphore:
            creator_unhandled_info: Dict = await self.bili_client.get_creator_info(creator_id)
            creator_info: Dict = {
                "id": creator_id,
                "name": creator_unhandled_info.get("name"),
                "sign": creator_unhandled_info.get("sign"),
                "avatar": creator_unhandled_info.get("face"),
            }
        await self.get_fans(creator_info, semaphore)
        await self.get_followings(creator_info, semaphore)
        await self.get_dynamics(creator_info, semaphore)

    async def get_fans(self, creator_info: Dict, semaphore: asyncio.Semaphore):
        """
        get fans for creator id
        :param creator_info:
        :param semaphore:
        :return:
        """
        creator_id = creator_info["id"]
        async with semaphore:
            try:
                utils.logger.info(f"[BilibiliCrawler.get_fans] begin get creator_id: {creator_id} fans ...")
                await self.bili_client.get_creator_all_fans(
                    creator_info=creator_info,
                    crawl_interval=config.CRAWLER_MAX_SLEEP_SEC,
                    callback=bilibili_store.batch_update_bilibili_creator_fans,
                    max_count=config.CRAWLER_MAX_CONTACTS_COUNT_SINGLENOTES,
                )

            except DataFetchError as ex:
                utils.logger.error(f"[BilibiliCrawler.get_fans] get creator_id: {creator_id} fans error: {ex}")
            except Exception as e:
                utils.logger.error(f"[BilibiliCrawler.get_fans] may be been blocked, err:{e}")

    async def get_followings(self, creator_info: Dict, semaphore: asyncio.Semaphore):
        """
        get followings for creator id
        :param creator_info:
        :param semaphore:
        :return:
        """
        creator_id = creator_info["id"]
        async with semaphore:
            try:
                utils.logger.info(f"[BilibiliCrawler.get_followings] begin get creator_id: {creator_id} followings ...")
                await self.bili_client.get_creator_all_followings(
                    creator_info=creator_info,
                    crawl_interval=config.CRAWLER_MAX_SLEEP_SEC,
                    callback=bilibili_store.batch_update_bilibili_creator_followings,
                    max_count=config.CRAWLER_MAX_CONTACTS_COUNT_SINGLENOTES,
                )

            except DataFetchError as ex:
                utils.logger.error(f"[BilibiliCrawler.get_followings] get creator_id: {creator_id} followings error: {ex}")
            except Exception as e:
                utils.logger.error(f"[BilibiliCrawler.get_followings] may be been blocked, err:{e}")

    async def get_dynamics(self, creator_info: Dict, semaphore: asyncio.Semaphore):
        """
        get dynamics for creator id
        :param creator_info:
        :param semaphore:
        :return:
        """
        creator_id = creator_info["id"]
        async with semaphore:
            try:
                utils.logger.info(f"[BilibiliCrawler.get_dynamics] begin get creator_id: {creator_id} dynamics ...")
                await self.bili_client.get_creator_all_dynamics(
                    creator_info=creator_info,
                    crawl_interval=config.CRAWLER_MAX_SLEEP_SEC,
                    callback=bilibili_store.batch_update_bilibili_creator_dynamics,
                    max_count=config.CRAWLER_MAX_DYNAMICS_COUNT_SINGLENOTES,
                )

            except DataFetchError as ex:
                utils.logger.error(f"[BilibiliCrawler.get_dynamics] get creator_id: {creator_id} dynamics error: {ex}")
            except Exception as e:
                utils.logger.error(f"[BilibiliCrawler.get_dynamics] may be been blocked, err:{e}")
