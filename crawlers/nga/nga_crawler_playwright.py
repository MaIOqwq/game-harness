import asyncio
import json
import os
import random
import re
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
# kafka / mysql 这两条出口在本部署里是**关着**的(nga_search_demo 不走 super().__init__,
# 出口只有 out/<目标>.json)。所以按可选依赖处理: 装了照旧能用, 没装也能起 ——
# 不为了两条死腿逼用户装 confluent-kafka 这类二进制的重依赖。
try:
    from confluent_kafka import Producer
except ImportError:
    Producer = None
try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:
    pymysql = DictCursor = None
from contextlib import contextmanager


def _system_chrome_path():
    """按操作系统找系统 Chrome 的路径; 找不到返回 None(交给 Playwright 自带浏览器兜底)。
    原来写死 /usr/bin/google-chrome, 换到 Windows/macOS 就找不到(会掉到自带浏览器那条路)。"""
    if os.name == 'nt':
        cands = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")]
    else:
        cands = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                 "/usr/bin/chromium", "/usr/bin/chromium-browser",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    for p in cands:
        if os.path.exists(p):
            return p
    return None


class NGACrawlerPlaywright:
    """NGA论坛爬虫"""
    
    def __init__(self, config):
        """初始化爬虫"""
        self.config = config
        self.cookies = config.get('cookies', {})
        self.keywords = config.get('keywords', [])
        self.base_url = 'https://ngabbs.com'
        self.output_dir = './output'
        self.max_posts_per_keyword = 300
        self.status_file = os.path.join(self.output_dir, 'crawler_status.json')
        self.last_crawl_time_file = os.path.join(self.output_dir, 'last_crawl_time.json')
        self.last_crawl_end_time_file = os.path.join(self.output_dir, 'last_crawl_end_time.json')
        
        # 关键词到FID的映射
        self.NGA_GAME_FID = {
            "手机游戏": 863,                    
            "王者荣耀": 516, 
            "和平精英": 599, 
            "原神": 650, 
            "崩坏星穹铁道": 818, 
            "绝区零": 853, 
            "明日方舟": -34587507, 
            "崩坏三": 549, 
            "天涯明月刀": -23052020, 
            "无限暖暖": 510373, 
            "英雄联盟手游": 681, 
            "金铲铲之战": 510461, 
            "明日方舟终末地": 846, 
            "三角洲行动": 510489, 
            "火影忍者手游": -19317848, 
            "燕云十六声": 510527, 
            "逆水寒手游": 510407, 
            "永劫无间手游": -39735775, 
            "光遇": -22495125, 
            "第五人格": 607, 
            "阴阳师": 538,
            "鸣潮": 854,
            
        }
        
        # 初始化爬取状态
        self.current_status = {
            'current_keyword_index': 0,
            'current_page': 1,
            'posts_collected': 0,
            'mode': 'full',  # 'full' 或 'incremental'
            'last_crawl_time': None
        }
        
        # 创建输出目录
        self._ensure_directory(self.output_dir)
        
        # Kafka配置
        kafka_config = config.get('kafka', {})
        self.kafka_enabled = kafka_config.get('enabled', False)
        self.kafka_bootstrap_servers = kafka_config.get('bootstrap_servers', '')
        self.kafka_topic = kafka_config.get('topic', 'crawler_data')
        self.kafka_producer = None
        
        # 数据库配置
        self.db_config = config.get('database', {})
        self.db_enabled = self.db_config.get('enabled', False)
        self.db_connection = None
        self.db_cursor = None
    
    def _ensure_directory(self, directory):
        """确保目录存在"""
        if not os.path.exists(directory):
            os.makedirs(directory)
    
    def _init_kafka_producer(self):
        """初始化Kafka生产者"""
        if self.kafka_enabled:
            try:
                self.kafka_producer = Producer({
                    'bootstrap.servers': self.kafka_bootstrap_servers,
                    'client.id': 'nga-crawler',
                    'acks': 'all',
                    'retries': 3,
                    'batch.num.messages': 16384,
                    'linger.ms': 10,
                    'queue.buffering.max.messages': 100000
                })
                print(f"Kafka生产者初始化成功: {self.kafka_bootstrap_servers}")
            except Exception as e:
                print(f"Kafka生产者初始化失败: {e}")
                self.kafka_enabled = False
    
    def _init_database(self):
        """初始化数据库连接"""
        if self.db_enabled:
            try:
                self.db_connection = pymysql.connect(
                    host=self.db_config.get('host', 'localhost'),
                    port=self.db_config.get('port', 3306),
                    user=self.db_config.get('user', 'root'),
                    password=self.db_config.get('password', ''),
                    database=self.db_config.get('database', 'crawler_data'),
                    charset=self.db_config.get('charset', 'utf8mb4'),
                    autocommit=self.db_config.get('autocommit', True)
                )
                self.db_cursor = self.db_connection.cursor(DictCursor)
                print("数据库连接初始化成功")
            except Exception as e:
                print(f"数据库连接初始化失败: {e}")
                self.db_enabled = False
    
    def _close_database(self):
        """关闭数据库连接"""
        if self.db_cursor:
            try:
                self.db_cursor.close()
                print("数据库游标已关闭")
            except Exception as e:
                print(f"关闭数据库游标时出错: {e}")
        if self.db_connection:
            try:
                self.db_connection.close()
                print("数据库连接已关闭")
            except Exception as e:
                print(f"关闭数据库连接时出错: {e}")
    
    def _monitor_memory_usage(self):
        """监控内存使用情况"""
        try:
            import psutil
            import os
            process = psutil.Process(os.getpid())
            memory_info = process.memory_info()
            memory_mb = memory_info.rss / 1024 / 1024
            print(f"内存使用: {memory_mb:.2f} MB")
            return memory_mb
        except Exception as e:
            print(f"监控内存时出错: {e}")
            return 0
    
    def _insert_post_to_db(self, post_data):
        """将帖子数据插入到数据库"""
        if not self.db_enabled or not self.db_cursor:
            return
        
        try:
            # 构建插入SQL
            sql = '''
                INSERT INTO nga_posts (
                    thread_id, author, title, content, post_time, keyword, 
                    replies, view_count, like_count, floor, quote, is_hot_reply, 
                    author_level, author_post_count, board_name, has_image, has_video
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    author = VALUES(author),
                    title = VALUES(title),
                    content = VALUES(content),
                    post_time = VALUES(post_time),
                    keyword = VALUES(keyword),
                    replies = VALUES(replies),
                    view_count = VALUES(view_count),
                    like_count = VALUES(like_count),
                    floor = VALUES(floor),
                    quote = VALUES(quote),
                    is_hot_reply = VALUES(is_hot_reply),
                    author_level = VALUES(author_level),
                    author_post_count = VALUES(author_post_count),
                    board_name = VALUES(board_name),
                    has_image = VALUES(has_image),
                    has_video = VALUES(has_video)
            '''
            
            # 准备参数
            params = (
                post_data.get('thread_id'),
                post_data.get('author'),
                post_data.get('title'),
                post_data.get('content'),
                post_data.get('post_time'),
                post_data.get('keyword'),
                post_data.get('replies', 0),
                post_data.get('view_count', 0),
                post_data.get('like_count', 0),
                post_data.get('floor', 0),
                post_data.get('quote'),
                post_data.get('is_hot_reply', False),
                post_data.get('author_level', 0),
                post_data.get('author_post_count', 0),
                post_data.get('board_name'),
                post_data.get('has_image', False),
                post_data.get('has_video', False)
            )
            
            # 执行SQL
            self.db_cursor.execute(sql, params)
            self.db_connection.commit()
        except Exception as e:
            print(f"插入帖子数据到数据库失败: {e}")
    
    def _insert_author_stats_to_db(self, author_stats):
        """将作者统计数据插入到数据库"""
        if not self.db_enabled or not self.db_cursor:
            return
        
        try:
            # 构建插入SQL
            sql = '''
                INSERT INTO author_stats (
                    author, platform, avg_hot, avg_like, avg_comment, post_count, last_update
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    avg_hot = VALUES(avg_hot),
                    avg_like = VALUES(avg_like),
                    avg_comment = VALUES(avg_comment),
                    post_count = VALUES(post_count),
                    last_update = VALUES(last_update)
            '''
            
            # 准备参数
            params = (
                author_stats.get('author'),
                author_stats.get('platform'),
                author_stats.get('avg_hot', 0.0),
                author_stats.get('avg_like', 0.0),
                author_stats.get('avg_comment', 0.0),
                author_stats.get('post_count', 0),
                author_stats.get('last_update')
            )
            
            # 执行SQL
            self.db_cursor.execute(sql, params)
            self.db_connection.commit()
        except Exception as e:
            print(f"插入作者统计数据到数据库失败: {e}")
    
    def _send_to_kafka(self, data, topic):
        """发送数据到Kafka"""
        if self.kafka_enabled and self.kafka_producer:
            try:
                self.kafka_producer.produce(topic, value=json.dumps(data).encode('utf-8'))
                # 移除每次发送后的flush()，提高性能
                # self.kafka_producer.flush()
            except Exception as e:
                print(f"发送数据到Kafka失败: {e}")
    
    async def run(self):
        """运行爬虫（连续增量爬取）"""
        try:
            # 初始化Kafka生产者
            self._init_kafka_producer()
            
            # 初始化数据库连接
            self._init_database()
            
            # 连续爬取循环
            while True:
                cycle_start_time = datetime.now()
                print(f"\n========== 开始新一轮爬取，时间: {cycle_start_time} ==========")
                print(f"关键词数量: {len(self.keywords)}")
                
                # 加载上次爬取结束时间
                last_crawl_end_time = self.load_last_crawl_end_time()
                if last_crawl_end_time:
                    print(f"将只爬取 {last_crawl_end_time} 之后的帖子（增量爬取）")
                else:
                    print(f"首次爬取，将爬取最近12小时内的所有帖子")
                
                # 重置状态
                self.current_status['current_keyword_index'] = 0
                self.current_status['current_page'] = 1
                self.current_status['posts_collected'] = 0
                self.current_status['mode'] = 'incremental'
                
                # 每处理一定数量的关键词后重启浏览器
                max_keywords_per_browser = 5
                
                async with async_playwright() as p:
                    start_index = 0
                    while start_index < len(self.keywords):
                        # 启动新的浏览器实例
                        print("正在启动浏览器...")
                        browser = None
                        context = None
                        
                        try:
                            browser = await self._launch_browser(p)
                            context = await browser.new_context()
                            
                            if self.cookies:
                                await self._login(context)
                            
                            # 处理一批关键词
                            batch_end = min(start_index + max_keywords_per_browser, len(self.keywords))
                            print(f"处理关键词批次: {start_index} 到 {batch_end-1}")
                            
                            for i in range(start_index, batch_end):
                                keyword = self.keywords[i]
                                print(f"\n开始处理关键词: {keyword}")
                                
                                # 更新当前关键词索引
                                self.current_status['current_keyword_index'] = i
                                self.current_status['current_page'] = 1
                                self.current_status['posts_collected'] = 0
                                self.current_status['last_crawl_time'] = cycle_start_time.isoformat()
                                self.save_status()
                                
                                # 加载对应关键词的tid列表
                                recent_tids = self.load_recent_tids(keyword)
                                print(f"已加载 {len(recent_tids)} 个最近的tid")
                                
                                # 使用增量爬取模式
                                self.current_status['mode'] = 'incremental'
                                print("使用增量爬取模式")
                                
                                tids = await self.crawl_forum(context, keyword, cycle_start_time, recent_tids, last_crawl_end_time)
                                
                                # 保存对应关键词的tid列表
                                if tids:
                                    self.save_recent_tids(keyword, tids)
                                
                                # 保存状态
                                self.save_status()
                                
                                # 每处理5个帖子进行一次内存监控和清理
                                if i % 5 == 0:
                                    memory_usage = self._monitor_memory_usage()
                                    # 如果内存使用超过1GB，进行强制垃圾回收
                                    if memory_usage > 1000:
                                        print("内存使用过高，进行强制垃圾回收...")
                                        import gc
                                        gc.collect()
                                        print("垃圾回收完成")
                                        self._monitor_memory_usage()
                                
                                if i < len(self.keywords) - 1:
                                    print("切换关键词，添加延迟...")
                                    await self._random_delay(5, 10)
                            
                            # 更新起始索引
                            start_index = batch_end
                            
                        finally:
                            # 关闭当前浏览器
                            try:
                                if context:
                                    await context.close()
                                    print("已关闭浏览器上下文")
                                if browser:
                                    await browser.close()
                                    print("已关闭浏览器")
                            except Exception as e:
                                print(f"关闭浏览器时出错: {e}")
                            
                            # 如果还有关键词需要处理，添加重启延迟
                            if start_index < len(self.keywords):
                                print("重启浏览器以释放内存，添加延迟...")
                                await asyncio.sleep(3)
                
                # 保存最后爬取时间
                self.save_last_crawl_time()
                
                # 本轮爬取完成
                cycle_end_time = datetime.now()
                print(f"\n本轮爬取完成，耗时: {cycle_end_time - cycle_start_time}")
                
                # 保存本次爬取结束时间，用于下次增量爬取
                self.save_last_crawl_end_time(cycle_end_time)
                
                # 直接进行下一轮爬取，不等待
                print("直接进行下一轮爬取...")
                
        except Exception as e:
            # 出错时保存状态
            self.save_status()
            print(f"运行爬虫时出错: {e}")
        finally:
            # 关闭Kafka生产者
            if self.kafka_producer:
                try:
                    # 发送所有缓冲的消息
                    self.kafka_producer.flush()
                    print("已刷新Kafka生产者缓冲区")
                    self.kafka_producer.close()
                    print("已关闭Kafka生产者")
                except Exception as e:
                    print(f"关闭Kafka生产者时出错: {e}")
            
            # 关闭数据库连接
            self._close_database()
    
    async def _launch_browser(self, playwright):
        """启动浏览器，优先使用系统 Chrome（按操作系统找）"""
        try:
            browser = await playwright.chromium.launch(
                executable_path=_system_chrome_path(),
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-features=site-per-process",
                    "--js-flags=--max-old-space-size=512",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-translate",
                    "--disable-background-fetch",
                    "--disable-client-side-phishing-detection",
                    "--disable-component-extensions-with-background-pages",
                    "--disable-default-apps",
                    "--disable-features=TranslateUI",
                    "--disable-hang-monitor",
                    "--disable-ipc-flooding-protection",
                    "--disable-prompt-on-repost",
                    "--disable-sync",
                    "--disable-tab-for-desktop-share"
                ]
            )
            print("使用系统Chrome浏览器成功")
        except Exception as e:
            print(f"使用系统Chrome失败: {e}，尝试使用Playwright默认浏览器")
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--js-flags=--max-old-space-size=512"
                ]
            )
        return browser
    
    async def _login(self, context):
        """登录NGA论坛。返回 True=已验证登录 / False=无凭据或验证没过(服务据此报 /health)。"""
        cookie_list = []
        for key, value in self.cookies.items():
            if value:
                cookie_list.append({
                    'name': key,
                    'value': value,
                    'url': self.base_url
                })

        if not cookie_list:
            print("无 cookie, 以未登录状态运行")
            return False

        await context.add_cookies(cookie_list)
        print("登录中...")
        # 测试登录状态
        test_page = await context.new_page()
        await test_page.goto(f'{self.base_url}/')
        await test_page.wait_for_load_state('networkidle')
        test_content = await test_page.content()
        await test_page.close()

        # 登录判据: 首页里出现某个"只有登录后才在"的字串。原版写死了毕设演示账号名, 换个账号
        # 就永远判成"没登录" —— 所以做成可配: config.json 里设 "login_marker" 成你的用户名。
        marker = getattr(self, 'login_marker', None) or '<YOUR_NGA_USERNAME>'
        if marker in test_content:
            print("登录成功")
            return True
        print("登录失败(首页里没找到登录标志 %r)，尝试继续爬取" % marker)
        return False
    
    async def crawl_forum(self, context, keyword, start_time, recent_tids, last_crawl_end_time=None):
        """直接爬取对应板块的帖子（支持增量爬取）
        
        Args:
            context: 浏览器上下文
            keyword: 关键词
            start_time: 开始时间（未使用，保留兼容性）
            recent_tids: 最近爬取的tid列表
            last_crawl_end_time: 上次爬取结束时间，用于增量爬取
        """
        try:
            # 获取对应的FID
            fid = self.NGA_GAME_FID.get(keyword)
            if not fid:
                print(f"未找到关键词 '{keyword}' 对应的FID，跳过")
                return []
            
            found_posts = self.current_status.get('posts_collected', 0)
            page_num = self.current_status.get('current_page', 1)
            collected_tids = []
            
            # 创建时间戳文件夹（精度为天）
            crawl_time = datetime.now().strftime('%Y-%m-%d')
            context_dir, comment_dir = self._create_output_directories(crawl_time)
            
            consecutive_empty_pages = 0
            max_consecutive_empty_pages = 3
            
            while found_posts < self.max_posts_per_keyword:
                # 构建板块URL
                forum_url = self._build_forum_url(keyword, fid, page_num)
                
                print(f"访问板块: {keyword} (FID: {fid})，第 {page_num} 页")
                print(f"访问URL: {forum_url}")
                
                page = None
                try:
                    page = await context.new_page()
                    print("正在访问板块页面...")
                    await page.goto(forum_url)
                    print("等待页面加载完成...")
                    await page.wait_for_load_state('networkidle')
                    print("页面加载完成")
                    
                    content = await page.content()
                finally:
                    if page:
                        await page.close()
                        print("已关闭板块页面")
                
                soup = BeautifulSoup(content, 'html.parser')
                
                # 检查是否为错误页面
                if '你必须登录才能查询更早的结果' in content:
                    print("遇到登录限制，停止翻页")
                    # 显式清理soup对象
                    del soup
                    import gc
                    gc.collect()
                    break
                
                # 找到所有帖子链接
                all_links = soup.find_all('a', href=re.compile(r'tid=\d+'))
                print(f"找到 {len(all_links)} 个包含tid的链接")
                
                # 如果没有找到链接，说明已经到最后一页
                if not all_links:
                    print("未找到更多帖子，停止翻页")
                    # 显式清理soup对象
                    del soup
                    import gc
                    gc.collect()
                    break
                
                # 收集当前页的帖子
                current_page_posts = []
                
                for link in all_links:
                    try:
                        # 提取标题文本
                        title = link.get_text(strip=True)
                        # 跳过纯数字的标题，这些可能是回复数或其他数字
                        if title.isdigit():
                            continue
                        # 跳过空标题
                        if not title:
                            continue
                        # 跳过非帖子链接（确保是read.php链接）
                        href = link.get('href', '')
                        if 'read.php' not in href:
                            continue
                        # 跳过包含stid的链接（通常是板块链接）
                        if 'stid=' in href:
                            continue
                        
                        thread_url = href
                        
                        # 提取thread_id
                        thread_id_match = re.search(r'tid=(\d+)', thread_url)
                        if not thread_id_match:
                            continue
                        thread_id = thread_id_match.group(1)
                        
                        # 检查是否已经添加过
                        if any(post['thread_id'] == thread_id for post in current_page_posts):
                            continue
                        
                        # 构建完整的帖子URL
                        full_thread_url = self._build_post_url(keyword, thread_url)
                        
                        # 尝试从父元素中提取作者、回复数和发布时间
                        author, replies, post_date_str = self._extract_post_metadata(link)
                        
                        # 确保标题不包含URL
                        if 'http' in title:
                            # 尝试从纯文本中提取标题
                            title = re.sub(r'https?://[^\s]+', '', title).strip()
                        
                        # 检查帖子是否在12小时以内，且在last_crawl_end_time之后
                        is_within_12_hours = self._is_within_12_hours(post_date_str, last_crawl_end_time)
                        
                        if is_within_12_hours:
                            current_page_posts.append({
                                'title': title,
                                'thread_id': thread_id,
                                'url': full_thread_url,
                                'author': author,
                                'replies': replies,
                                'post_date': post_date_str
                            })
                            collected_tids.append(thread_id)
                            # 限制collected_tids列表长度，只保留最近100个，避免内存累积
                            if len(collected_tids) > 100:
                                collected_tids = collected_tids[-100:]
                            found_posts += 1
                            print(f"找到帖子: {title} (ID: {thread_id})")
                            
                            if found_posts >= self.max_posts_per_keyword:
                                break
                        else:
                            print(f"跳过超过12小时的帖子: {title} (ID: {thread_id})")
                    except Exception as e:
                        print(f"处理帖子链接时出错: {e}")
                        continue
                
                # 显式清理soup对象
                del soup
                import gc
                gc.collect()
                
                # 如果没有找到新帖子，增加连续空页面计数
                if not current_page_posts:
                    consecutive_empty_pages += 1
                    print(f"连续空页面数: {consecutive_empty_pages}/{max_consecutive_empty_pages}")
                    if consecutive_empty_pages >= max_consecutive_empty_pages:
                        print("连续多个页面没有找到新帖子，停止翻页")
                        break
                else:
                    consecutive_empty_pages = 0
                    print(f"当前页找到 {len(current_page_posts)} 个帖子，开始处理...")
                    
                    # 立即处理当前页的帖子
                    for i, post in enumerate(current_page_posts, 1):
                        print(f"处理第 {i} 个帖子: {post['title']}")
                        if post['thread_id']:
                            try:
                                # 检查是否为增量爬取
                                is_incremental = post['thread_id'] in recent_tids
                                if is_incremental:
                                    print(f"帖子 {post['thread_id']} 在最近tid列表中，执行增量爬取")
                                else:
                                    print(f"帖子 {post['thread_id']} 不在最近tid列表中，执行全量爬取")
                                
                                post_detail = await self.get_post_detail(context, post, fid)
                                
                                if post_detail:
                                    context_item = {
                                        'thread_id': post['thread_id'],
                                        'title': post['title'],
                                        'author': post['author'],
                                        'replies': post['replies'],
                                        'post_date': post.get('post_date', '未知'),
                                        'url': post['url'],
                                        'keyword': keyword
                                    }
                                    
                                    main_post_item = {
                                        'thread_id': post['thread_id'],
                                        'author': post_detail.get('author', '未知'),
                                        'content': post_detail.get('content', ''),
                                        'type': 'main',
                                        'keyword': keyword,
                                        'post_date': post.get('post_date', '未知')
                                    }
                                    
                                    # 获取时间戳，如果post_date为空或'未知'则使用当前时间
                                    post_date = post.get('post_date', '未知')
                                    timestamp = post_date if post_date and post_date != '未知' else datetime.now().isoformat()
                                    
                                    # 发送到Kafka - 主帖数据
                                    kafka_post_data = {
                                        "platform": "nga",
                                        "type": "post",
                                        "raw_id": post['thread_id'],
                                        "author": post['author'],
                                        "title": post['title'],
                                        "content": post_detail.get('content', ''),
                                        "publish_time": self._format_publish_time(post.get('post_date', '')),
                                        "keyword": keyword,
                                        "view_count": post.get('view_count', 0),
                                        "like_count": post_detail.get('like_count', 0),
                                        "comment_count": int(post.get('replies', 0)) if str(post.get('replies', 0)).isdigit() else 0,
                                        "coin_count": 0,
                                        "favorite_count": 0,
                                        "share_count": 0,
                                        "danmaku_count": 0,
                                        "is_hot_reply": False,
                                        "author_fans": 0,
                                        "author_level": post_detail.get('author_level', 0),
                                        "author_post_count": post_detail.get('author_post_count', 0),
                                        "has_image": post_detail.get('has_image', False),
                                        "has_video": post_detail.get('has_video', False),
                                        "board_name": keyword
                                    }
                                    self._send_to_kafka(kafka_post_data, self.kafka_topic)
                                    
                                    # 保存到文件（使用高效的JSON追加方式）
                                    context_file = os.path.join(context_dir, f'{keyword}.json')
                                    try:
                                        # 检查文件是否存在
                                        file_exists = os.path.exists(context_file)
                                        
                                        if not file_exists:
                                            # 文件不存在，创建并写入开头
                                            with open(context_file, 'w', encoding='utf-8') as f:
                                                f.write('[\n')
                                            is_first_item = True
                                        else:
                                            # 文件存在，检查是否为空
                                            with open(context_file, 'r', encoding='utf-8') as f:
                                                content = f.read().strip()
                                            is_first_item = content == '['
                                        
                                        # 使用追加模式写入数据
                                        with open(context_file, 'a', encoding='utf-8') as f:
                                            if not is_first_item:
                                                f.write(',\n')
                                            json.dump(context_item, f, ensure_ascii=False, indent=2)
                                    except Exception as e:
                                        print(f"保存帖子基本信息时出错: {e}")
                                    
                                    comment_file = os.path.join(comment_dir, f'{keyword}.json')
                                    try:
                                        # 检查文件是否存在
                                        file_exists = os.path.exists(comment_file)
                                        
                                        if not file_exists:
                                            # 文件不存在，创建并写入开头
                                            with open(comment_file, 'w', encoding='utf-8') as f:
                                                f.write('[\n')
                                            is_first_item = True
                                        else:
                                            # 文件存在，检查是否为空
                                            with open(comment_file, 'r', encoding='utf-8') as f:
                                                content = f.read().strip()
                                            is_first_item = content == '['
                                        
                                        # 使用追加模式写入主帖内容
                                        with open(comment_file, 'a', encoding='utf-8') as f:
                                            if not is_first_item:
                                                f.write(',\n')
                                            json.dump(main_post_item, f, ensure_ascii=False, indent=2)
                                    except Exception as e:
                                        print(f"保存帖子详细信息时出错: {e}")
                                    
                                    replies = post_detail.get('replies', [])
                                    for reply in replies:
                                        reply_item = {
                                            'thread_id': post['thread_id'],
                                            'author': reply.get('author', '未知'),
                                            'content': reply.get('content', ''),
                                            'type': 'reply',
                                            'keyword': keyword,
                                            'post_date': reply.get('post_date', '')
                                        }
                                        
                                        # 获取回复时间戳
                                        reply_date = reply.get('post_date', '')
                                        reply_timestamp = reply_date if reply_date and reply_date != '' else datetime.now().isoformat()
                                        
                                        # 发送回复到Kafka
                                        kafka_reply_data = {
                                            "platform": "nga",
                                            "type": "reply",
                                            "raw_id": post['thread_id'],
                                            "author": reply.get('author', '未知'),
                                            "title": post['title'],
                                            "content": reply.get('content', ''),
                                            "publish_time": self._format_publish_time(reply.get('post_date', '')),
                                            "keyword": keyword,
                                            "view_count": 0,
                                            "like_count": 0,
                                            "comment_count": 0,
                                            "coin_count": 0,
                                            "favorite_count": 0,
                                            "share_count": 0,
                                            "danmaku_count": 0,
                                            "is_hot_reply": False,
                                            "author_fans": 0,
                                            "author_level": 0,
                                            "author_post_count": 0,
                                            "has_image": False,
                                            "has_video": False,
                                            "board_name": keyword
                                        }
                                        self._send_to_kafka(kafka_reply_data, self.kafka_topic)
                                        
                                        # 保存回复到文件
                                    try:
                                        # 检查文件是否存在
                                        file_exists = os.path.exists(comment_file)
                                        
                                        if not file_exists:
                                            # 文件不存在，创建并写入开头
                                            with open(comment_file, 'w', encoding='utf-8') as f:
                                                f.write('[\n')
                                            is_first_item = True
                                        else:
                                            # 文件存在，检查是否为空
                                            with open(comment_file, 'r', encoding='utf-8') as f:
                                                content = f.read().strip()
                                            is_first_item = content == '['
                                        
                                        # 使用追加模式写入回复
                                        with open(comment_file, 'a', encoding='utf-8') as f:
                                            if not is_first_item:
                                                f.write(',\n')
                                            json.dump(reply_item, f, ensure_ascii=False, indent=2)
                                    except Exception as e:
                                        print(f"保存回复时出错: {e}")
                                
                                print(f"处理第 {i} 个帖子完成")
                            except Exception as e:
                                print(f"处理第 {i} 个帖子时出错: {e}")
                                continue
                    
                    # 清空当前页的帖子列表，释放内存
                    current_page_posts.clear()
                    print(f"已清空当前页帖子列表，释放内存")
                
                # 保存状态
                self.current_status['current_page'] = page_num
                self.current_status['posts_collected'] = found_posts
                self.save_status()
                
                # 检查是否达到最大帖子数
                if found_posts >= self.max_posts_per_keyword:
                    print(f"已达到最大帖子数 {self.max_posts_per_keyword}，停止爬取")
                    break
                
                # 翻页
                page_num += 1
                # 添加延迟（2-4秒）
                await self._random_delay(2, 4)
            
            print(f"爬取完成，共收集 {found_posts} 个帖子")
            
            # 关闭所有JSON文件，添加结束符
            self._close_json_files(context_dir, comment_dir, keyword)
            
            return collected_tids
        except Exception as e:
            print(f"爬取板块时出错: {e}")
            return []
    
    def _build_forum_url(self, keyword, fid, page_num):
        """构建板块URL"""
        if keyword == "明日方舟":
            return f'https://bbs.nga.cn/thread.php?fid={fid}&page={page_num}'
        else:
            if fid > 0:
                return f'{self.base_url}/thread.php?fid={fid}&page={page_num}'
            else:
                return f'{self.base_url}/thread.php?stid={abs(fid)}&page={page_num}'
    
    def _build_post_url(self, keyword, thread_url):
        """构建完整的帖子URL"""
        if keyword == "明日方舟":
            if thread_url.startswith('/'):
                return f'https://bbs.nga.cn{thread_url}'
            elif not thread_url.startswith('http'):
                return f'https://bbs.nga.cn/{thread_url}'
            else:
                return thread_url
        else:
            if thread_url.startswith('/'):
                return f'{self.base_url}{thread_url}'
            elif not thread_url.startswith('http'):
                return f'{self.base_url}/{thread_url}'
            else:
                return thread_url
    
    def _extract_post_metadata(self, link):
        """从链接父元素中提取作者、回复数和发布时间"""
        author = '未知'
        replies = '0'
        post_date_str = ''
        
        parent = link.parent
        while parent:
            author_elem = parent.select_one('.author') or parent.select_one('.username') or parent.select_one('.user')
            if author_elem:
                author = author_elem.get_text(strip=True)
            
            reply_elem = parent.select_one('.replies') or parent.select_one('.reply_count')
            if reply_elem:
                replies = reply_elem.get_text(strip=True)
            
            postdate_elem = parent.select_one('.postdate') or parent.select_one('.date')
            if postdate_elem:
                post_date_str = postdate_elem.get_text(strip=True)
            
            # 如果已经找到所有信息，就停止
            if author != '未知' and replies != '0' and post_date_str:
                break
            
            parent = parent.parent
        
        return author, replies, post_date_str
    
    def _is_within_12_hours(self, post_date_str, start_from=None):
        """检查帖子是否在12小时以内，且在start_from之后（避免重复爬取）
        
        Args:
            post_date_str: 帖子发布日期字符串
            start_from: 上次爬取结束时间，如果为None则只检查是否在12小时内
        """
        if not post_date_str:
            return False
        
        try:
            now = datetime.now()
            twelve_hours_ago = now - timedelta(hours=12)
            
            # 解析帖子发布时间
            post_datetime = None
            
            if '今天' in post_date_str:
                today = now
                hour_match = re.search(r'(\d+)小时前', post_date_str)
                if hour_match:
                    hours = int(hour_match.group(1))
                    post_datetime = today - timedelta(hours=hours)
                else:
                    time_match = re.search(r'(\d{2}):(\d{2})', post_date_str)
                    if time_match:
                        post_datetime = today.replace(hour=int(time_match.group(1)), minute=int(time_match.group(2)), second=0)
                    else:
                        post_datetime = now  # 无法解析，默认现在
            elif '昨天' in post_date_str:
                yesterday = now - timedelta(days=1)
                hour_match = re.search(r'(\d+)小时前', post_date_str)
                if hour_match:
                    hours = int(hour_match.group(1))
                    post_datetime = yesterday - timedelta(hours=hours)
                else:
                    time_match = re.search(r'(\d{2}):(\d{2})', post_date_str)
                    if time_match:
                        post_datetime = yesterday.replace(hour=int(time_match.group(1)), minute=int(time_match.group(2)), second=0)
                    else:
                        post_datetime = yesterday
            elif '前天' in post_date_str:
                post_datetime = now - timedelta(days=2)
            elif '小时前' in post_date_str:
                hour_match = re.search(r'(\d+)小时前', post_date_str)
                if hour_match:
                    hours = int(hour_match.group(1))
                    post_datetime = now - timedelta(hours=hours)
            elif '分钟前' in post_date_str or '秒前' in post_date_str:
                post_datetime = now  # 几分钟或几秒前，默认现在
            elif re.match(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}', post_date_str):
                post_datetime = datetime.strptime(post_date_str, '%Y-%m-%d %H:%M')
            elif re.match(r'\d{4}-\d{2}-\d{2}', post_date_str):
                post_datetime = datetime.strptime(post_date_str, '%Y-%m-%d')
            elif re.match(r'\d{2}-\d{2}', post_date_str):
                current_year = now.year
                post_datetime = datetime.strptime(f'{current_year}-{post_date_str}', '%Y-%m-%d')
            
            if post_datetime is None:
                return False
            
            # 检查是否在12小时以内
            is_within_12h = post_datetime >= twelve_hours_ago
            
            # 如果有start_from，检查是否在start_from之后
            if start_from is not None:
                is_after_start = post_datetime > start_from
                return is_within_12h and is_after_start
            
            return is_within_12h
            
        except Exception as e:
            print(f"解析日期时出错: {e}")
            return False
    
    def _format_publish_time(self, date_str):
        """格式化发布时间为 yyyy-MM-dd HH:mm:ss 格式"""
        if not date_str or date_str == '未知':
            return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            # 处理今天、昨天、前天
            if '今天' in date_str:
                today = datetime.now()
                time_part = re.search(r'\d{2}:\d{2}', date_str)
                if time_part:
                    time_str = time_part.group()
                    return f'{today.strftime("%Y-%m-%d")} {time_str}:00'
                return today.strftime('%Y-%m-%d %H:%M:%S')
            elif '昨天' in date_str:
                yesterday = datetime.now() - timedelta(days=1)
                time_part = re.search(r'\d{2}:\d{2}', date_str)
                if time_part:
                    time_str = time_part.group()
                    return f'{yesterday.strftime("%Y-%m-%d")} {time_str}:00'
                return yesterday.strftime('%Y-%m-%d %H:%M:%S')
            elif '前天' in date_str:
                day_before_yesterday = datetime.now() - timedelta(days=2)
                time_part = re.search(r'\d{2}:\d{2}', date_str)
                if time_part:
                    time_str = time_part.group()
                    return f'{day_before_yesterday.strftime("%Y-%m-%d")} {time_str}:00'
                return day_before_yesterday.strftime('%Y-%m-%d %H:%M:%S')
            # 处理 yyyy-MM-dd 格式
            elif re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                try:
                    date = datetime.strptime(date_str, '%Y-%m-%d')
                    return date.strftime('%Y-%m-%d 00:00:00')
                except:
                    pass
            # 处理 MM-dd 格式
            elif re.match(r'\d{2}-\d{2}', date_str):
                current_year = datetime.now().year
                try:
                    date = datetime.strptime(f'{current_year}-{date_str}', '%Y-%m-%d')
                    return date.strftime('%Y-%m-%d 00:00:00')
                except:
                    pass
            # 处理 MM-dd HH:mm 格式
            elif re.match(r'\d{2}-\d{2} \d{2}:\d{2}', date_str):
                current_year = datetime.now().year
                try:
                    date = datetime.strptime(f'{current_year}-{date_str}', '%Y-%m-%d %H:%M')
                    return date.strftime('%Y-%m-%d %H:%M:00')
                except:
                    pass
            # 处理 HH:mm 格式
            elif re.match(r'\d{2}:\d{2}', date_str):
                today = datetime.now()
                try:
                    date = datetime.strptime(f'{today.strftime("%Y-%m-%d")} {date_str}', '%Y-%m-%d %H:%M')
                    return date.strftime('%Y-%m-%d %H:%M:00')
                except:
                    pass
        except Exception as e:
            print(f"格式化日期时出错: {e}")
        
        # 所有格式都解析失败，返回当前时间
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    def _verify_post_fid(self, soup, expected_fid):
        """验证帖子是否属于指定的版面"""
        try:
            # 尝试从页面中提取FID
            # 方式1: 从URL中提取
            fid_elem = soup.find('a', href=re.compile(r'fid=(\d+)'))
            if fid_elem:
                href = fid_elem.get('href', '')
                fid_match = re.search(r'fid=(\d+)', href)
                if fid_match:
                    page_fid = int(fid_match.group(1))
                    if page_fid == abs(expected_fid):
                        return True
            
            # 方式2: 从页面内容中查找版面信息
            # 查找包含版面信息的元素
            forum_info = soup.find('div', class_='forum_info') or soup.find('div', class_='forum')
            if forum_info:
                # 尝试提取FID
                text = forum_info.get_text()
                fid_match = re.search(r'fid[=:]\s*(\d+)', text, re.IGNORECASE)
                if fid_match:
                    page_fid = int(fid_match.group(1))
                    if page_fid == abs(expected_fid):
                        return True
            
            # 方式3: 检查页面标题或面包屑导航
            breadcrumb = soup.find('div', class_='h') or soup.find('div', class_='breadcrumb')
            if breadcrumb:
                # 如果找到了面包屑，说明页面结构正常，可能FID匹配
                # 这里我们假设如果页面能正常加载，就可能是正确的版面
                # 但为了安全，我们还是需要更严格的验证
                pass
            
            # 方式4: 从script标签或meta标签中提取
            scripts = soup.find_all('script')
            for script in scripts:
                script_text = script.string if script.string else ''
                fid_match = re.search(r'fid[=:]\s*(\d+)', script_text)
                if fid_match:
                    page_fid = int(fid_match.group(1))
                    if page_fid == abs(expected_fid):
                        return True
            
            # 如果以上方式都未能验证，默认返回True（保持向后兼容）
            # 但会打印警告信息
            print(f"警告: 无法验证帖子版面FID，期望: {expected_fid}")
            return True
        except Exception as e:
            print(f"验证版面FID时出错: {e}")
            # 出错时默认允许，避免漏掉有效帖子
            return True
    
    def _create_output_directories(self, crawl_time):
        """创建输出目录"""
        keyword_output_dir = os.path.join(self.output_dir, crawl_time)
        context_dir = os.path.join(keyword_output_dir, 'context')
        comment_dir = os.path.join(keyword_output_dir, 'comment')
        
        for dir_path in [keyword_output_dir, context_dir, comment_dir]:
            self._ensure_directory(dir_path)
        
        return context_dir, comment_dir
    
    def _save_batch_data(self, context_dir, comment_dir, keyword, context_data, comment_data, batch_start):
        """保存批次数据"""
        # 发送数据到Kafka
        for item in context_data:
            kafka_data = {
                "platform": "nga",
                "type": "post",
                "raw_id": item.get('thread_id', ''),
                "author": item.get('author', ''),
                "title": item.get('title', ''),
                "content": item.get('content', ''),
                "publish_time": self._format_publish_time(item.get('post_date', '')),
                "keyword": keyword,
                "view_count": item.get('view_count', 0),
                "like_count": item.get('like_count', 0),
                "comment_count": int(item.get('replies', 0)) if str(item.get('replies', 0)).isdigit() else 0,
                "coin_count": 0,
                "favorite_count": 0,
                "share_count": 0,
                "danmaku_count": 0,
                "is_hot_reply": False,
                "author_fans": 0,
                "author_level": item.get('author_level', 0),
                "author_post_count": item.get('author_post_count', 0),
                "has_image": item.get('has_image', False),
                "has_video": item.get('has_video', False),
                "board_name": keyword
            }
            self._send_to_kafka(kafka_data, self.kafka_topic)
        
        for item in comment_data:
            kafka_data = {
                "platform": "nga",
                "type": "reply",
                "raw_id": item.get('thread_id', ''),
                "author": item.get('author', '未知'),
                "title": item.get('title', ''),
                "content": item.get('content', ''),
                "publish_time": self._format_publish_time(item.get('post_date', '')),
                "keyword": keyword,
                "view_count": 0,
                "like_count": 0,
                "comment_count": 0,
                "coin_count": 0,
                "favorite_count": 0,
                "share_count": 0,
                "danmaku_count": 0,
                "is_hot_reply": False,
                "author_fans": 0,
                "author_level": 0,
                "author_post_count": 0,
                "has_image": False,
                "has_video": False,
                "board_name": keyword
            }
            self._send_to_kafka(kafka_data, self.kafka_topic)
        
        # 保存数据到文件
        context_file = os.path.join(context_dir, f'{keyword}.json')
        try:
            if batch_start == 0:
                with open(context_file, 'w', encoding='utf-8') as f:
                    json.dump(context_data, f, ensure_ascii=False, indent=2)
            else:
                with open(context_file, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
                existing_data.extend(context_data)
                with open(context_file, 'w', encoding='utf-8') as f:
                    json.dump(existing_data, f, ensure_ascii=False, indent=2)
            print(f"帖子基本信息已保存到: {context_file}")
        except Exception as e:
            print(f"保存帖子基本信息时出错: {e}")
        
        comment_file = os.path.join(comment_dir, f'{keyword}.json')
        try:
            if batch_start == 0:
                with open(comment_file, 'w', encoding='utf-8') as f:
                    json.dump(comment_data, f, ensure_ascii=False, indent=2)
            else:
                with open(comment_file, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
                existing_data.extend(comment_data)
                with open(comment_file, 'w', encoding='utf-8') as f:
                    json.dump(existing_data, f, ensure_ascii=False, indent=2)
            print(f"帖子详细信息已保存到: {comment_file}")
        except Exception as e:
            print(f"保存帖子详细信息时出错: {e}")
    
    async def get_post_detail(self, context, post, fid=None):
        """获取帖子详情和回复（支持翻页）"""
        thread_id = post['thread_id']
        page = None
        
        try:
            page = await context.new_page()
            replies = []
            page_num = 1
            has_next_page = True
            main_content = f"帖子内容（ID: {thread_id}）"
            max_pages = 80
            max_replies = 1000  # 限制回复数量，避免内存累积
            
            while has_next_page and page_num <= max_pages and len(replies) < max_replies:
                # 构建帖子详情URL
                full_url = self._build_post_detail_url(post, thread_id, page_num)
                print(f"访问帖子详情页 {page_num}: {full_url}")
                
                await page.goto(full_url, timeout=30000)
                await page.wait_for_load_state('networkidle', timeout=30000)
                
                content = await page.content()
                
                # 检查帖子是否被锁定
                if '此帖子被锁定' in content:
                    print(f"帖子 {thread_id} 被锁定，跳过")
                    await page.close()
                    return self._create_post_data(thread_id, post, f"帖子被锁定（ID: {thread_id}）", [])
                
                soup = BeautifulSoup(content, 'html.parser')
                
                # 第一页验证版面、时间并提取主贴内容
                if page_num == 1:
                    # 验证帖子是否属于指定的版面
                    if fid is not None:
                        is_valid_fid = self._verify_post_fid(soup, fid)
                        if not is_valid_fid:
                            print(f"帖子 {thread_id} 不属于指定的版面 (FID: {fid})，跳过")
                            await page.close()
                            return None
                    
                    # 验证帖子发布时间是否在12小时以内（双重保险）
                    post_time_elem = soup.find(class_='postdate')
                    if post_time_elem:
                        post_time_str = post_time_elem.get_text(strip=True)
                        if not self._is_within_12_hours(post_time_str):
                            print(f"帖子 {thread_id} 发布时间超过12小时，跳过")
                            await page.close()
                            return None
                    
                    main_content = self._extract_main_content(soup, thread_id)
                
                # 提取回复
                print(f"开始提取第 {page_num} 页回复...")
                
                # 提取热点回复（只在第一页）
                if page_num == 1:
                    replies.extend(self._extract_hot_replies(soup))
                
                # 提取普通回复
                normal_replies = self._extract_normal_replies(soup, page_num)
                # 检查是否会超过回复数量限制
                remaining_slots = max_replies - len(replies)
                if remaining_slots > 0:
                    replies.extend(normal_replies[:remaining_slots])
                
                # 检查是否有下一页
                has_next_page = self._has_next_page(soup)
                
                # 清理soup对象，释放内存
                del soup
                import gc
                gc.collect()
                
                if not has_next_page:
                    print("没有更多回复页")
                    break
                
                page_num += 1
                
                if page_num > max_pages:
                    print(f"已达到最大页面数 {max_pages}，停止爬取")
                    break
                
                if len(replies) >= max_replies:
                    print(f"已达到最大回复数 {max_replies}，停止爬取")
                    break
                    
                # 添加延迟
                await self._random_delay(1, 2)
            
            print(f"总共找到 {len(replies)} 个回复")
            
            return self._create_post_data(thread_id, post, main_content, replies)
        except Exception as e:
            print(f"获取帖子详情时出错: {e}")
            # 如果获取失败，返回基本信息
            return self._create_post_data(thread_id, post, f"帖子内容（ID: {thread_id}）", [])
        finally:
            # 确保页面被关闭
            if page:
                await page.close()
                print(f"已关闭帖子详情页: {thread_id}")
    
    def _build_post_detail_url(self, post, thread_id, page_num):
        """构建帖子详情URL"""
        if 'bbs.nga.cn' in post.get('url', ''):
            return f'https://bbs.nga.cn/read.php?tid={thread_id}&page={page_num}'
        else:
            return f'{self.base_url}/read.php?tid={thread_id}&page={page_num}'
    
    def _extract_main_content(self, soup, thread_id):
        """提取主贴内容"""
        content_elem = soup.select_one('.t_f')
        if not content_elem:
            content_elem = soup.select_one('.postcontent')
        
        if content_elem:
            main_content = content_elem.get_text(strip=True)
            print(f"提取到主贴内容，长度: {len(main_content)}")
        else:
            main_content = f"帖子内容（ID: {thread_id}）"
            print("未找到主贴内容，使用占位符")
        
        return main_content
    
    def _extract_hot_replies(self, soup):
        """提取热点回复（只保留点赞数>1的）"""
        replies = []
        comment_elems = soup.find_all('div', class_='comment_c')
        print(f"方式1 - 找到 {len(comment_elems)} 个热点回复元素")
        
        for comment_elem in comment_elems:
            try:
                author = '未知'
                author_elem = comment_elem.find('a', class_='userlink')
                if author_elem:
                    author = author_elem.get_text(strip=True)
                
                reply_content = ''
                content_elem = comment_elem.find(class_='ubbcode')
                if content_elem:
                    reply_content = content_elem.get_text(strip=True)
                    # 移除最后的"…… [原帖]"
                    reply_content = reply_content.replace('…… [原帖]', '').strip()
                
                # 提取回复时间
                reply_time = ''
                time_elem = comment_elem.find(class_='postdate')
                if time_elem:
                    reply_time = time_elem.get_text(strip=True)
                
                # 提取点赞数
                like_count = 0
                like_elem = comment_elem.find(class_='thumbsup')
                if like_elem:
                    like_text = like_elem.get_text(strip=True)
                    like_match = re.search(r'\d+', like_text)
                    if like_match:
                        like_count = int(like_match.group())
                
                # 只保留点赞数>1的回复
                if reply_content and like_count > 1:
                    replies.append({
                        'author': author,
                        'content': reply_content,
                        'post_date': reply_time,
                        'like_count': like_count
                    })
            except Exception as e:
                print(f"提取热点回复时出错: {e}")
                continue
        
        return replies
    
    def _extract_normal_replies(self, soup, page_num):
        """提取普通回复（只保留点赞数>1的）"""
        replies = []
        post_elems = soup.find_all('tr', class_='postrow')
        print(f"方式2 - 找到 {len(post_elems)} 个普通回复元素")
        
        for i, post_elem in enumerate(post_elems, 1):
            # 跳过主贴（通常是第一个）
            if page_num == 1 and i == 1:
                continue
            
            try:
                author = '未知'
                author_elem = post_elem.find('a', class_='userlink')
                if author_elem:
                    author = author_elem.get_text(strip=True)
                
                reply_content = ''
                content_elem = post_elem.find(class_='postcontent')
                if not content_elem:
                    content_elem = post_elem.find(class_='ubbcode')
                
                if content_elem:
                    reply_content = content_elem.get_text(strip=True)
                
                # 提取回复时间
                reply_time = ''
                time_elem = post_elem.find(class_='postdate')
                if time_elem:
                    reply_time = time_elem.get_text(strip=True)
                
                # 提取点赞数
                like_count = 0
                like_elem = post_elem.find(class_='thumbsup')
                if like_elem:
                    like_text = like_elem.get_text(strip=True)
                    like_match = re.search(r'\d+', like_text)
                    if like_match:
                        like_count = int(like_match.group())
                
                # 只保留点赞数>1的回复
                if reply_content and like_count > 1:
                    replies.append({
                        'author': author,
                        'content': reply_content,
                        'post_date': reply_time,
                        'like_count': like_count
                    })
            except Exception as e:
                print(f"提取普通回复时出错: {e}")
                continue
        
        return replies
    
    def _has_next_page(self, soup):
        """检查是否有下一页"""
        # 方式1: 查找文本为"下一页"的链接
        next_page = soup.find('a', text='下一页')
        if next_page:
            print(f"找到下一页链接（方式1）: {next_page.get('href', '')}")
            return True
        
        # 方式2: 查找包含"page="的链接
        page_links = soup.find_all('a', href=re.compile(r'page=\d+'))
        for link in page_links:
            link_text = link.get_text(strip=True)
            if '下一页' in link_text or '>' in link_text:
                print(f"找到下一页链接（方式2）: {link.get('href', '')}")
                return True
        
        # 方式3: 查找分页区域
        pagination = soup.find('div', class_='pages')
        if pagination:
            page_links = pagination.find_all('a')
            for link in page_links:
                link_text = link.get_text(strip=True)
                if '下一页' in link_text or '>' in link_text:
                    print(f"找到下一页链接（方式3）: {link.get('href', '')}")
                    return True
        
        return False
    
    def _create_post_data(self, thread_id, post, content, replies):
        """创建帖子数据"""
        return {
            'thread_id': thread_id,
            'title': post['title'],
            'author': post['author'],
            'content': content,
            'replies': replies,
            'post_time': post.get('post_date', ''),
            'keyword': post.get('keyword', ''),
            'view_count': post.get('view_count', 0),
            'like_count': post.get('like_count', 0),
            'floor': 1,  # 主楼
            'quote': '',  # 引用内容，需要从页面提取
            'is_hot_reply': False,  # 主楼不是回复
            'author_level': post.get('author_level', 0),
            'author_post_count': post.get('author_post_count', 0),
            'board_name': post.get('board_name', ''),
            'has_image': post.get('has_image', False),
            'has_video': post.get('has_video', False)
        }
    
    async def _random_delay(self, min_delay, max_delay):
        """随机延迟，避免被反爬虫"""
        delay = random.uniform(min_delay, max_delay)
        await asyncio.sleep(delay)
    
    def load_recent_tids(self, keyword):
        """加载最近的tid列表（按关键词）"""
        tid_file = os.path.join(self.output_dir, f'recent_tids_{keyword}.json')
        if os.path.exists(tid_file):
            try:
                with open(tid_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get('recent_tids', [])
            except Exception as e:
                print(f"加载tid列表时出错: {e}")
        return []
    
    def _close_json_files(self, context_dir, comment_dir, keyword):
        """关闭所有JSON文件，添加结束符"""
        # 关闭context文件
        context_file = os.path.join(context_dir, f'{keyword}.json')
        if os.path.exists(context_file):
            try:
                with open(context_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if content and not content.endswith(']'):
                    with open(context_file, 'a', encoding='utf-8') as f:
                        f.write('\n]')
                print(f"已关闭context文件: {context_file}")
            except Exception as e:
                print(f"关闭context文件时出错: {e}")
        
        # 关闭comment文件
        comment_file = os.path.join(comment_dir, f'{keyword}.json')
        if os.path.exists(comment_file):
            try:
                with open(comment_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if content and not content.endswith(']'):
                    with open(comment_file, 'a', encoding='utf-8') as f:
                        f.write('\n]')
                print(f"已关闭comment文件: {comment_file}")
            except Exception as e:
                print(f"关闭comment文件时出错: {e}")
    
    def save_recent_tids(self, keyword, tids):
        """保存最近的tid列表，最多保存50个（按关键词）"""
        recent_tids = tids[-50:]  # 只保留最近50个tid
        tid_file = os.path.join(self.output_dir, f'recent_tids_{keyword}.json')
        try:
            with open(tid_file, 'w', encoding='utf-8') as f:
                json.dump({'recent_tids': recent_tids}, f, ensure_ascii=False, indent=2)
            print(f"已保存 {len(recent_tids)} 个最近的tid")
        except Exception as e:
            print(f"保存tid列表时出错: {e}")
    
    def save_last_crawl_time(self):
        """保存上次爬取时间"""
        try:
            last_crawl_time = datetime.now().isoformat()
            with open(self.last_crawl_time_file, 'w', encoding='utf-8') as f:
                json.dump({'last_crawl_time': last_crawl_time}, f, ensure_ascii=False, indent=2)
            print(f"已保存上次爬取时间: {last_crawl_time}")
        except Exception as e:
            print(f"保存上次爬取时间时出错: {e}")
    
    def save_status(self):
        """保存爬取状态"""
        try:
            with open(self.status_file, 'w', encoding='utf-8') as f:
                json.dump(self.current_status, f, ensure_ascii=False, indent=2)
            print(f"已保存爬取状态: {self.current_status}")
        except Exception as e:
            print(f"保存状态时出错: {e}")
    
    def load_status(self):
        """加载爬取状态"""
        if os.path.exists(self.status_file):
            try:
                with open(self.status_file, 'r', encoding='utf-8') as f:
                    self.current_status = json.load(f)
                print(f"已加载爬取状态: {self.current_status}")
                return True
            except Exception as e:
                print(f"加载状态时出错: {e}")
        return False
    
    def load_last_crawl_end_time(self):
        """加载上次爬取结束时间"""
        if os.path.exists(self.last_crawl_end_time_file):
            try:
                with open(self.last_crawl_end_time_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    end_time_str = data.get('end_time')
                    if end_time_str:
                        end_time = datetime.strptime(end_time_str, '%Y-%m-%d %H:%M:%S')
                        print(f"已加载上次爬取结束时间: {end_time}")
                        return end_time
            except Exception as e:
                print(f"加载上次爬取结束时间时出错: {e}")
        return None
    
    def save_last_crawl_end_time(self, end_time):
        """保存本次爬取结束时间"""
        try:
            data = {'end_time': end_time.strftime('%Y-%m-%d %H:%M:%S')}
            with open(self.last_crawl_end_time_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"已保存爬取结束时间: {end_time}")
        except Exception as e:
            print(f"保存爬取结束时间时出错: {e}")

def load_config():
    """加载配置文件"""
    config_path = 'config.json'
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    else:
        return {
            "cookies": {},
            "keywords": ["魔兽世界"]
        }

async def main():
    try:
        config = load_config()
        crawler = NGACrawlerPlaywright(config)
        await crawler.run()
    except Exception as e:
        print(f"运行爬虫时出错: {e}")

if __name__ == "__main__":
    asyncio.run(main())