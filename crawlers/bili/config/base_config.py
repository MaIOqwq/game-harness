# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/config/base_config.py
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

import os

# Basic configuration
PLATFORM = "bili"  # 本包只做 B 站
KEYWORDS = "编程副业,编程兼职"  # Keyword search configuration, separated by English commas
LOGIN_TYPE = "cookie"  # qrcode or phone or cookie
COOKIES = ""
CRAWLER_TYPE = (
    "search"  # Crawling type, search (keyword search) | detail (post details) | creator (creator homepage data)
)
# Whether to enable IP proxy
ENABLE_IP_PROXY = False

# Number of proxy IP pools
IP_PROXY_POOL_COUNT = 2

# Proxy IP provider name
IP_PROXY_PROVIDER_NAME = "kuaidaili"  # kuaidaili | wandouhttp

# Setting to True will not open the browser (headless browser)
# Setting False will open a browser
# If Xiaohongshu keeps scanning the code to log in but fails, open the browser and manually pass the sliding verification code.
# If Douyin keeps prompting failure, open the browser and see if mobile phone number verification appears after scanning the QR code to log in. If it does, manually go through it and try again.
HEADLESS = True

# Whether to save login status
SAVE_LOGIN_STATE = True

# ==================== CDP (Chrome DevTools Protocol) Configuration ====================
# Whether to enable CDP mode - use the user's existing Chrome/Edge browser to crawl, providing better anti-detection capabilities
# Once enabled, the user's Chrome/Edge browser will be automatically detected and started, and controlled through the CDP protocol.
# This method uses the real browser environment, including the user's extensions, cookies and settings, greatly reducing the risk of detection.
ENABLE_CDP_MODE = False

# CDP debug port, used to communicate with the browser
# If the port is occupied, the system will automatically try the next available port
CDP_DEBUG_PORT = 9222

# Custom browser path (optional)
# If it is empty, the system will automatically detect the installation path of Chrome/Edge
# Windows example: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
# macOS example: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CUSTOM_BROWSER_PATH = ""

# Whether to enable headless mode in CDP mode
# NOTE: Even if set to True, some anti-detection features may not work well in headless mode
CDP_HEADLESS = True

# Browser startup timeout (seconds)
BROWSER_LAUNCH_TIMEOUT = 60

# Whether to automatically close the browser when the program ends
# Set to False to keep the browser running for easy debugging
AUTO_CLOSE_BROWSER = True

# Data saving type option configuration, supports: csv, db, json, jsonl, sqlite, excel, postgres. It is best to save to DB, with deduplication function.
SAVE_DATA_OPTION = "json"  # csv or db or json or jsonl or sqlite or excel or postgres

# Data saving path, if not specified by default, it will be saved to the data folder.
SAVE_DATA_PATH = "./data/"

# Browser file configuration cached by the user's browser
USER_DATA_DIR = "%s_user_data_dir"  # %s will be replaced by platform name

# The number of pages to start crawling starts from the first page by default
START_PAGE = 1

# Control the number of crawled videos/posts (B站搜索API最多返回972个视频)
CRAWLER_MAX_NOTES_COUNT = 972

# Controlling the number of concurrent crawlers
MAX_CONCURRENCY_NUM = 1

# Whether to enable crawling media mode (including image or video resources), crawling media is not enabled by default
ENABLE_GET_MEIDAS = False

# Whether to enable comment crawling mode. Comment crawling is enabled by default.
ENABLE_GET_COMMENTS = True

# Control the number of crawled first-level comments (single video/post)
CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 5000

# Whether to enable the mode of crawling second-level comments. By default, crawling of second-level comments is not enabled.
# If the old version of the project uses db, you need to refer to schema/tables.sql line 287 to add table fields.
ENABLE_GET_SUB_COMMENTS = False

# word cloud related
# Whether to enable generating comment word clouds
ENABLE_GET_WORDCLOUD = False
# Custom words and their groups
# Add rule: xx:yy where xx is a custom-added phrase, and yy is the group name to which the phrase xx is assigned.
CUSTOM_WORDS = {
    "零几": "年份",  # Recognize "zero points" as a whole
    "高频词": "专业术语",  # Example custom words
}

# 词云资源目录: 本包自己的 docs/(按文件位置算, 不用启动时的 cwd —— 换个目录跑就找不着)。
# 本部署这个目录下**没有**下面这两个文件: 缺失时 tools/words.py 会跳过词云并打印一句原因, 不再抛 FileNotFoundError。
DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")

# Deactivate (disabled) word file path
STOP_WORDS_FILE = os.path.join(DOCS_DIR, "hit_stopwords.txt")

# Chinese font file path
FONT_PATH = os.path.join(DOCS_DIR, "STZHONGS.TTF")

# Crawl interval
CRAWLER_MAX_SLEEP_SEC = 2

# ==================== Kafka Configuration ====================
# Kafka bootstrap servers (comma separated)
KAFKA_BOOTSTRAP_SERVERS = ""   # 本部署不用 Kafka, 见下面 ENABLE_KAFKA_STORAGE

# Kafka topic names
KAFKA_TOPIC_VIDEO_CONTENT = "crawler_data"
KAFKA_TOPIC_COMMENT = "crawler_data"

# Whether to enable Kafka storage (enable simultaneous storage to file and Kafka)
# 本地跑关掉: 打开会去连 KAFKA_BOOTSTRAP_SERVERS(服务器 IP), 本机没这个服务。
ENABLE_KAFKA_STORAGE = False

# 本包只做 B 站: 其余六个平台(小红书/抖音/快手/微博/贴吧/知乎)的配置与代码都已摘掉。
from .bilibili_config import *
