# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/bilibili/__init__.py
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
# @Time    : 2024/1/14 19:34
# @Desc    :

from typing import Dict, List

import config
from var import source_keyword_var
from tools import utils
from datetime import datetime

from ._store_impl import *
from .bilibilli_store_media import *


class BiliStoreFactory:
    STORES = {
        "csv": BiliCsvStoreImplement,
        "db": BiliDbStoreImplement,
        "postgres": BiliDbStoreImplement,
        "json": BiliJsonStoreImplement,
        "jsonl": BiliJsonlStoreImplement,
        "sqlite": BiliSqliteStoreImplement,
        "mongodb": BiliMongoStoreImplement,
        "excel": BiliExcelStoreImplement,
        "kafka": BiliKafkaStoreImplement,
    }

    @staticmethod
    def create_store() -> AbstractStore:
        """创建存储实例"""
        store_class = BiliStoreFactory.STORES.get(config.SAVE_DATA_OPTION)
        if not store_class:
            raise ValueError("[BiliStoreFactory.create_store] Invalid save option only supported csv or db or json or sqlite or mongodb or excel ...")
        return store_class()

    @staticmethod
    def create_stores() -> list:
        """创建多个存储实例（支持同时存储到文件和Kafka）"""
        stores = []
        
        # 检查是否启用了同时存储
        enable_kafka = getattr(config, 'ENABLE_KAFKA_STORAGE', False)
        utils.logger.info(f"[BiliStoreFactory.create_stores] ENABLE_KAFKA_STORAGE: {enable_kafka}, SAVE_DATA_OPTION: {config.SAVE_DATA_OPTION}")
        
        # 创建主存储实例
        main_store = BiliStoreFactory.create_store()
        stores.append(main_store)
        utils.logger.info(f"[BiliStoreFactory.create_stores] Created main store: {type(main_store).__name__}")
        
        # 如果启用了Kafka存储且主存储不是Kafka，则添加Kafka存储
        if enable_kafka and config.SAVE_DATA_OPTION != "kafka":
            utils.logger.info("[BiliStoreFactory.create_stores] Creating Kafka store...")
            kafka_store = BiliKafkaStoreImplement()
            stores.append(kafka_store)
            utils.logger.info(f"[BiliStoreFactory.create_stores] Created Kafka store: {type(kafka_store).__name__}")
        elif not enable_kafka:
            utils.logger.info("[BiliStoreFactory.create_stores] Kafka storage is disabled")
        else:
            utils.logger.info("[BiliStoreFactory.create_stores] Main store is Kafka, no need to create additional Kafka store")
        
        utils.logger.info(f"[BiliStoreFactory.create_stores] Total stores created: {len(stores)}")
        return stores


async def update_bilibili_video(video_item: Dict):
    video_item_view: Dict = video_item.get("View")
    video_user_info: Dict = video_item_view.get("owner")
    video_item_stat: Dict = video_item_view.get("stat")
    video_id = str(video_item_view.get("aid"))
    bvid = video_item_view.get("bvid", "")
    save_content_item = {
        "platform": "bilibili",
        "type": "video",
        "raw_id": bvid,
        # 落库(BiliDbStoreImplement)按 video_id 查列, 这里给的是 aid —— 键名以落库那份为准
        "video_id": int(video_id),
        "author": video_user_info.get("name", ""),
        "title": video_item_view.get("title", "")[:500],
        "content": video_item_view.get("desc", "")[:500],
        "publish_time": datetime.fromtimestamp(video_item_view.get("pubdate")).strftime('%Y-%m-%d %H:%M:%S') if video_item_view.get("pubdate") else "",
        "keyword": source_keyword_var.get(),
        "view_count": video_item_stat.get("view", 0),
        "like_count": video_item_stat.get("like", 0),
        "comment_count": video_item_stat.get("reply", 0),
        "coin_count": video_item_stat.get("coin", 0),
        "favorite_count": video_item_stat.get("favorite", 0),
        "share_count": video_item_stat.get("share", 0),
        "danmaku_count": video_item_stat.get("danmaku", 0),
        "is_hot_reply": False,
        "author_fans": 0,  # 需要从其他接口获取
        "author_level": 0,  # 需要从其他接口获取
        "author_post_count": 0,  # 需要从其他接口获取
        "has_image": True,  # 封面可视为有图
        "has_video": True,  # 固定为True
        "board_name": ""
    }
    utils.logger.info(f"[store.bilibili.update_bilibili_video] bilibili video id:{video_id}, title:{save_content_item.get('title')}")
    
    # 获取所有存储实例并同时存储
    stores = BiliStoreFactory.create_stores()
    for store in stores:
        await store.store_content(content_item=save_content_item)


async def update_up_info(video_item: Dict):
    video_item_card_list: Dict = video_item.get("Card")
    video_item_card: Dict = video_item_card_list.get("card")
    saver_up_info = {
        "user_id": str(video_item_card.get("mid")),
        "nickname": video_item_card.get("name"),
        "sex": video_item_card.get("sex"),
        "sign": video_item_card.get("sign"),
        "avatar": video_item_card.get("face"),
        "last_modify_ts": utils.get_current_timestamp(),
        "total_fans": video_item_card.get("fans"),
        "total_liked": video_item_card_list.get("like_num"),
        "user_rank": video_item_card.get("level_info").get("current_level"),
        "is_official": video_item_card.get("official_verify").get("type"),
    }
    utils.logger.info(f"[store.bilibili.update_up_info] bilibili user_id:{video_item_card.get('mid')}")
    
    # 获取所有存储实例并同时存储
    stores = BiliStoreFactory.create_stores()
    for store in stores:
        await store.store_creator(creator=saver_up_info)


async def batch_update_bilibili_video_comments(video_id: str, comments: List[Dict]):
    if not comments:
        return
    for comment_item in comments:
        await update_bilibili_video_comment(video_id, comment_item)


async def update_bilibili_video_comment(video_id: str, comment_item: Dict):
    comment_id = str(comment_item.get("rpid"))
    content: Dict = comment_item.get("content")
    user_info: Dict = comment_item.get("member")
    like_count: int = comment_item.get("like", 0)
    save_comment_item = {
        "platform": "bilibili",
        "type": "comment",
        "raw_id": video_id,  # 使用视频ID作为raw_id
        # 落库(BiliDbStoreImplement)按 comment_id 查列、video_id 存所属视频 —— 原字典只有 raw_id,
        # 那边取到 None 直接 int(None) TypeError, 所以这两个键必须给
        "comment_id": int(comment_id),
        "video_id": int(video_id or 0),
        "author": user_info.get("uname", ""),
        "title": "",  # 评论没有标题
        "content": content.get("message", ""),
        "publish_time": datetime.fromtimestamp(comment_item.get("ctime")).strftime('%Y-%m-%d %H:%M:%S') if comment_item.get("ctime") else "",
        "keyword": source_keyword_var.get(),
        "view_count": 0,
        "like_count": like_count,
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
        "board_name": ""
    }
    utils.logger.info(f"[store.bilibili.update_bilibili_video_comment] Bilibili video comment: {comment_id}, content: {save_comment_item.get('content')}")
    
    # 获取所有存储实例并同时存储
    stores = BiliStoreFactory.create_stores()
    for store in stores:
        await store.store_comment(comment_item=save_comment_item)


async def store_video(aid, video_content, extension_file_name):
    """
    video video storage implementation
    Args:
        aid:
        video_content:
        extension_file_name:
    """
    await BilibiliVideo().store_video({
        "aid": aid,
        "video_content": video_content,
        "extension_file_name": extension_file_name,
    })


async def batch_update_bilibili_creator_fans(creator_info: Dict, fans_list: List[Dict]):
    if not fans_list:
        return
    for fan_item in fans_list:
        fan_info: Dict = {
            "id": fan_item.get("mid"),
            "name": fan_item.get("uname"),
            "sign": fan_item.get("sign"),
            "avatar": fan_item.get("face"),
        }
        await update_bilibili_creator_contact(creator_info=creator_info, fan_info=fan_info)


async def batch_update_bilibili_creator_followings(creator_info: Dict, followings_list: List[Dict]):
    if not followings_list:
        return
    for following_item in followings_list:
        following_info: Dict = {
            "id": following_item.get("mid"),
            "name": following_item.get("uname"),
            "sign": following_item.get("sign"),
            "avatar": following_item.get("face"),
        }
        await update_bilibili_creator_contact(creator_info=following_info, fan_info=creator_info)


async def batch_update_bilibili_creator_dynamics(creator_info: Dict, dynamics_list: List[Dict]):
    if not dynamics_list:
        return
    for dynamic_item in dynamics_list:
        dynamic_id: str = dynamic_item["id_str"]
        dynamic_text: str = ""
        if dynamic_item["modules"]["module_dynamic"].get("desc"):
            dynamic_text = dynamic_item["modules"]["module_dynamic"]["desc"]["text"]
        dynamic_type: str = dynamic_item["type"].split("_")[-1]
        dynamic_pub_ts: str = dynamic_item["modules"]["module_author"]["pub_ts"]
        dynamic_stat: Dict = dynamic_item["modules"]["module_stat"]
        dynamic_comment: int = dynamic_stat["comment"]["count"]
        dynamic_forward: int = dynamic_stat["forward"]["count"]
        dynamic_like: int = dynamic_stat["like"]["count"]
        dynamic_info: Dict = {
            "dynamic_id": dynamic_id,
            "text": dynamic_text,
            "type": dynamic_type,
            "pub_ts": dynamic_pub_ts,
            "total_comments": dynamic_comment,
            "total_forwards": dynamic_forward,
            "total_liked": dynamic_like,
        }
        await update_bilibili_creator_dynamic(creator_info=creator_info, dynamic_info=dynamic_info)


async def update_bilibili_creator_contact(creator_info: Dict, fan_info: Dict):
    save_contact_item = {
        "up_id": creator_info["id"],
        "fan_id": fan_info["id"],
        "up_name": creator_info["name"],
        "fan_name": fan_info["name"],
        "up_sign": creator_info["sign"],
        "fan_sign": fan_info["sign"],
        "up_avatar": creator_info["avatar"],
        "fan_avatar": fan_info["avatar"],
        "last_modify_ts": utils.get_current_timestamp(),
    }

    # 获取所有存储实例并同时存储
    stores = BiliStoreFactory.create_stores()
    for store in stores:
        await store.store_contact(contact_item=save_contact_item)


async def update_bilibili_creator_dynamic(creator_info: Dict, dynamic_info: Dict):
    save_dynamic_item = {
        "dynamic_id": dynamic_info["dynamic_id"],
        "user_id": creator_info["id"],
        "user_name": creator_info["name"],
        "text": dynamic_info["text"],
        "type": dynamic_info["type"],
        "pub_ts": dynamic_info["pub_ts"],
        "total_comments": dynamic_info["total_comments"],
        "total_forwards": dynamic_info["total_forwards"],
        "total_liked": dynamic_info["total_liked"],
        "last_modify_ts": utils.get_current_timestamp(),
    }

    # 获取所有存储实例并同时存储
    stores = BiliStoreFactory.create_stores()
    for store in stores:
        await store.store_dynamic(dynamic_item=save_dynamic_item)
