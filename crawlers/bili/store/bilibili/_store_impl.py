# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/bilibili/_store_impl.py
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
# @Author  : persist1@126.com
# @Time    : 2025/9/5 19:34
# @Desc    : Bilibili storage implementation class
import asyncio
import csv
import json
import os
import pathlib
from typing import Dict

import aiofiles
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import config
from base.base_crawler import AbstractStore
from database.db_session import get_session
from database.models import BilibiliVideoComment, BilibiliVideo, BilibiliUpInfo, BilibiliUpDynamic, BilibiliContactInfo
from tools.async_file_writer import AsyncFileWriter
from tools import utils, words
from var import crawler_type_var
from database.mongodb_store_base import MongoDBStoreBase


class BiliCsvStoreImplement(AbstractStore):
    def __init__(self):
        self.file_writer = AsyncFileWriter(
            crawler_type=crawler_type_var.get(),
            platform="bili"
        )

    async def store_content(self, content_item: Dict):
        """
        content CSV storage implementation
        Args:
            content_item:

        Returns:

        """
        await self.file_writer.write_to_csv(
            item=content_item,
            item_type="videos"
        )

    async def store_comment(self, comment_item: Dict):
        """
        comment CSV storage implementation
        Args:
            comment_item:

        Returns:

        """
        await self.file_writer.write_to_csv(
            item=comment_item,
            item_type="comments"
        )

    async def store_creator(self, creator: Dict):
        """
        creator CSV storage implementation
        Args:
            creator:

        Returns:

        """
        await self.file_writer.write_to_csv(
            item=creator,
            item_type="creators"
        )

    async def store_contact(self, contact_item: Dict):
        """
        creator contact CSV storage implementation
        Args:
            contact_item: creator's contact item dict

        Returns:

        """
        await self.file_writer.write_to_csv(
            item=contact_item,
            item_type="contacts"
        )

    async def store_dynamic(self, dynamic_item: Dict):
        """
        creator dynamic CSV storage implementation
        Args:
            dynamic_item: creator's contact item dict

        Returns:

        """
        await self.file_writer.write_to_csv(
            item=dynamic_item,
            item_type="dynamics"
        )


class BiliDbStoreImplement(AbstractStore):
    async def store_content(self, content_item: Dict):
        """
        Bilibili content DB storage implementation
        Args:
            content_item: content item dict
        """
        video_id = int(content_item.get("video_id"))
        content_item["video_id"] = video_id
        content_item["user_id"] = int(content_item.get("user_id", 0) or 0)
        content_item["liked_count"] = int(content_item.get("liked_count", 0) or 0)
        content_item["create_time"] = int(content_item.get("create_time", 0) or 0)

        async with get_session() as session:
            result = await session.execute(select(BilibiliVideo).where(BilibiliVideo.video_id == video_id))
            video_detail = result.scalar_one_or_none()

            if not video_detail:
                content_item["add_ts"] = utils.get_current_timestamp()
                content_item["last_modify_ts"] = utils.get_current_timestamp()
                new_content = BilibiliVideo(**content_item)
                session.add(new_content)
            else:
                content_item["last_modify_ts"] = utils.get_current_timestamp()
                for key, value in content_item.items():
                    setattr(video_detail, key, value)
            await session.commit()

    async def store_comment(self, comment_item: Dict):
        """
        Bilibili comment DB storage implementation
        Args:
            comment_item: comment item dict
        """
        comment_id = int(comment_item.get("comment_id"))
        comment_item["comment_id"] = comment_id
        comment_item["video_id"] = int(comment_item.get("video_id", 0) or 0)
        comment_item["create_time"] = int(comment_item.get("create_time", 0) or 0)
        comment_item["like_count"] = str(comment_item.get("like_count", "0"))
        comment_item["sub_comment_count"] = str(comment_item.get("sub_comment_count", "0"))
        comment_item["parent_comment_id"] = str(comment_item.get("parent_comment_id", "0"))

        async with get_session() as session:
            result = await session.execute(select(BilibiliVideoComment).where(BilibiliVideoComment.comment_id == comment_id))
            comment_detail = result.scalar_one_or_none()

            if not comment_detail:
                comment_item["add_ts"] = utils.get_current_timestamp()
                comment_item["last_modify_ts"] = utils.get_current_timestamp()
                new_comment = BilibiliVideoComment(**comment_item)
                session.add(new_comment)
            else:
                comment_item["last_modify_ts"] = utils.get_current_timestamp()
                for key, value in comment_item.items():
                    setattr(comment_detail, key, value)
            await session.commit()

    async def store_creator(self, creator: Dict):
        """
        Bilibili creator DB storage implementation
        Args:
            creator: creator item dict
        """
        creator_id = int(creator.get("user_id"))
        creator["user_id"] = creator_id
        creator["total_fans"] = int(creator.get("total_fans", 0) or 0)
        creator["total_liked"] = int(creator.get("total_liked", 0) or 0)
        creator["user_rank"] = int(creator.get("user_rank", 0) or 0)
        creator["is_official"] = int(creator.get("is_official", 0) or 0)

        async with get_session() as session:
            result = await session.execute(select(BilibiliUpInfo).where(BilibiliUpInfo.user_id == creator_id))
            creator_detail = result.scalar_one_or_none()

            if not creator_detail:
                creator["add_ts"] = utils.get_current_timestamp()
                creator["last_modify_ts"] = utils.get_current_timestamp()
                new_creator = BilibiliUpInfo(**creator)
                session.add(new_creator)
            else:
                creator["last_modify_ts"] = utils.get_current_timestamp()
                for key, value in creator.items():
                    setattr(creator_detail, key, value)
            await session.commit()

    async def store_contact(self, contact_item: Dict):
        """
        Bilibili contact DB storage implementation
        Args:
            contact_item: contact item dict
        """
        up_id = int(contact_item.get("up_id"))
        fan_id = int(contact_item.get("fan_id"))
        contact_item["up_id"] = up_id
        contact_item["fan_id"] = fan_id

        async with get_session() as session:
            result = await session.execute(
                select(BilibiliContactInfo).where(BilibiliContactInfo.up_id == up_id, BilibiliContactInfo.fan_id == fan_id)
            )
            contact_detail = result.scalar_one_or_none()

            if not contact_detail:
                contact_item["add_ts"] = utils.get_current_timestamp()
                contact_item["last_modify_ts"] = utils.get_current_timestamp()
                new_contact = BilibiliContactInfo(**contact_item)
                session.add(new_contact)
            else:
                contact_item["last_modify_ts"] = utils.get_current_timestamp()
                for key, value in contact_item.items():
                    setattr(contact_detail, key, value)
            await session.commit()

    async def store_dynamic(self, dynamic_item):
        """
        Bilibili dynamic DB storage implementation
        Args:
            dynamic_item: dynamic item dict
        """
        dynamic_id = int(dynamic_item.get("dynamic_id"))
        dynamic_item["dynamic_id"] = dynamic_id

        async with get_session() as session:
            result = await session.execute(select(BilibiliUpDynamic).where(BilibiliUpDynamic.dynamic_id == dynamic_id))
            dynamic_detail = result.scalar_one_or_none()

            if not dynamic_detail:
                dynamic_item["add_ts"] = utils.get_current_timestamp()
                dynamic_item["last_modify_ts"] = utils.get_current_timestamp()
                new_dynamic = BilibiliUpDynamic(**dynamic_item)
                session.add(new_dynamic)
            else:
                dynamic_item["last_modify_ts"] = utils.get_current_timestamp()
                for key, value in dynamic_item.items():
                    setattr(dynamic_detail, key, value)
            await session.commit()


class BiliJsonStoreImplement(AbstractStore):
    def __init__(self):
        # 按照要求的目录结构存储
        import config
        self.base_dir = config.SAVE_DATA_PATH + "/bili"
        import os
        import logging
        logging.info(f"[BiliJsonStoreImplement.__init__] Creating base directory: {self.base_dir}")
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            logging.info(f"[BiliJsonStoreImplement.__init__] Base directory created successfully")
        except Exception as e:
            logging.error(f"[BiliJsonStoreImplement.__init__] Failed to create base directory: {e}")

    async def store_content(self, content_item: Dict):
        """
        content JSON storage implementation
        Args:
            content_item:

        Returns:

        """
        import os
        import json
        from datetime import datetime
        import logging
        
        logging.info(f"[BiliJsonStoreImplement.store_content] Storing content item: {content_item.get('video_id')}")
        try:
            # 获取当前日期
            today = datetime.now().strftime("%Y-%m-%d")
            # 构建目录结构
            context_dir = os.path.join(self.base_dir, today, "context")
            logging.info(f"[BiliJsonStoreImplement.store_content] Creating context directory: {context_dir}")
            os.makedirs(context_dir, exist_ok=True)
            
            # 获取关键词
            keyword = content_item.get("source_keyword", "unknown")
            file_path = os.path.join(context_dir, f"{keyword}.json")
            logging.info(f"[BiliJsonStoreImplement.store_content] Storing to file: {file_path}")
            
            # 读取现有数据
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            else:
                existing_data = []
            
            # 添加新数据
            existing_data.append(content_item)
            
            # 写入文件（每次添加数据后立即写入，实现小波输出）
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=2)
            logging.info(f"[BiliJsonStoreImplement.store_content] Content stored successfully")
        except Exception as e:
            logging.error(f"[BiliJsonStoreImplement.store_content] Failed to store content: {e}")

    async def store_comment(self, comment_item: Dict):
        """
        comment JSON storage implementation
        Args:
            comment_item:

        Returns:

        """
        import os
        import json
        from datetime import datetime
        import logging
        
        logging.info(f"[BiliJsonStoreImplement.store_comment] Storing comment item: {comment_item.get('comment_id')}")
        try:
            # 获取当前日期
            today = datetime.now().strftime("%Y-%m-%d")
            # 构建目录结构
            comment_dir = os.path.join(self.base_dir, today, "comment")
            logging.info(f"[BiliJsonStoreImplement.store_comment] Creating comment directory: {comment_dir}")
            os.makedirs(comment_dir, exist_ok=True)
            
            # 获取关键词
            keyword = comment_item.get("source_keyword", "unknown")
            file_path = os.path.join(comment_dir, f"{keyword}.json")
            logging.info(f"[BiliJsonStoreImplement.store_comment] Storing to file: {file_path}")
            
            # 读取现有数据
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            else:
                existing_data = []
            
            # 添加新数据
            existing_data.append(comment_item)
            
            # 写入文件（每次添加数据后立即写入，实现小波输出）
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=2)
            logging.info(f"[BiliJsonStoreImplement.store_comment] Comment stored successfully")
        except Exception as e:
            logging.error(f"[BiliJsonStoreImplement.store_comment] Failed to store comment: {e}")

    async def store_creator(self, creator: Dict):
        """
        creator JSON storage implementation
        Args:
            creator:

        Returns:

        """
        import os
        import json
        from datetime import datetime
        import logging
        
        logging.info(f"[BiliJsonStoreImplement.store_creator] Storing creator item: {creator.get('user_id')}")
        try:
            # 获取当前日期
            today = datetime.now().strftime("%Y-%m-%d")
            # 构建目录结构
            creator_dir = os.path.join(self.base_dir, today, "creator")
            logging.info(f"[BiliJsonStoreImplement.store_creator] Creating creator directory: {creator_dir}")
            os.makedirs(creator_dir, exist_ok=True)
            
            # 构建文件路径
            file_path = os.path.join(creator_dir, "creators.json")
            logging.info(f"[BiliJsonStoreImplement.store_creator] Storing to file: {file_path}")
            
            # 读取现有数据
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            else:
                existing_data = []
            
            # 添加新数据
            existing_data.append(creator)
            
            # 写入文件
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=2)
            logging.info(f"[BiliJsonStoreImplement.store_creator] Creator stored successfully")
        except Exception as e:
            logging.error(f"[BiliJsonStoreImplement.store_creator] Failed to store creator: {e}")

    async def store_contact(self, contact_item: Dict):
        """
        creator contact JSON storage implementation
        Args:
            contact_item: creator's contact item dict

        Returns:

        """
        import os
        import json
        from datetime import datetime
        import logging
        
        logging.info(f"[BiliJsonStoreImplement.store_contact] Storing contact item")
        try:
            # 获取当前日期
            today = datetime.now().strftime("%Y-%m-%d")
            # 构建目录结构
            contact_dir = os.path.join(self.base_dir, today, "contact")
            logging.info(f"[BiliJsonStoreImplement.store_contact] Creating contact directory: {contact_dir}")
            os.makedirs(contact_dir, exist_ok=True)
            
            # 构建文件路径
            file_path = os.path.join(contact_dir, "contacts.json")
            logging.info(f"[BiliJsonStoreImplement.store_contact] Storing to file: {file_path}")
            
            # 读取现有数据
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            else:
                existing_data = []
            
            # 添加新数据
            existing_data.append(contact_item)
            
            # 写入文件
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=2)
            logging.info(f"[BiliJsonStoreImplement.store_contact] Contact stored successfully")
        except Exception as e:
            logging.error(f"[BiliJsonStoreImplement.store_contact] Failed to store contact: {e}")

    async def store_dynamic(self, dynamic_item: Dict):
        """
        creator dynamic JSON storage implementation
        Args:
            dynamic_item: creator's contact item dict

        Returns:

        """
        import os
        import json
        from datetime import datetime
        import logging
        
        logging.info(f"[BiliJsonStoreImplement.store_dynamic] Storing dynamic item: {dynamic_item.get('dynamic_id')}")
        try:
            # 获取当前日期
            today = datetime.now().strftime("%Y-%m-%d")
            # 构建目录结构
            dynamic_dir = os.path.join(self.base_dir, today, "dynamic")
            logging.info(f"[BiliJsonStoreImplement.store_dynamic] Creating dynamic directory: {dynamic_dir}")
            os.makedirs(dynamic_dir, exist_ok=True)
            
            # 构建文件路径
            file_path = os.path.join(dynamic_dir, "dynamics.json")
            logging.info(f"[BiliJsonStoreImplement.store_dynamic] Storing to file: {file_path}")
            
            # 读取现有数据
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    existing_data = json.load(f)
            else:
                existing_data = []
            
            # 添加新数据
            existing_data.append(dynamic_item)
            
            # 写入文件
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=2)
            logging.info(f"[BiliJsonStoreImplement.store_dynamic] Dynamic stored successfully")
        except Exception as e:
            logging.error(f"[BiliJsonStoreImplement.store_dynamic] Failed to store dynamic: {e}")



class BiliJsonlStoreImplement(AbstractStore):
    def __init__(self):
        self.file_writer = AsyncFileWriter(
            crawler_type=crawler_type_var.get(),
            platform="bili"
        )

    async def store_content(self, content_item: Dict):
        await self.file_writer.write_to_jsonl(
            item=content_item,
            item_type="contents"
        )

    async def store_comment(self, comment_item: Dict):
        await self.file_writer.write_to_jsonl(
            item=comment_item,
            item_type="comments"
        )

    async def store_creator(self, creator: Dict):
        await self.file_writer.write_to_jsonl(
            item=creator,
            item_type="creators"
        )

    async def store_contact(self, contact_item: Dict):
        await self.file_writer.write_to_jsonl(
            item=contact_item,
            item_type="contacts"
        )

    async def store_dynamic(self, dynamic_item: Dict):
        await self.file_writer.write_to_jsonl(
            item=dynamic_item,
            item_type="dynamics"
        )


class BiliSqliteStoreImplement(BiliDbStoreImplement):
    pass


class BiliMongoStoreImplement(AbstractStore):
    """Bilibili MongoDB storage implementation"""

    def __init__(self):
        self.mongo_store = MongoDBStoreBase(collection_prefix="bilibili")

    async def store_content(self, content_item: Dict):
        """
        Store video content to MongoDB
        Args:
            content_item: Video content data
        """
        video_id = content_item.get("video_id")
        if not video_id:
            return

        await self.mongo_store.save_or_update(
            collection_suffix="contents",
            query={"video_id": video_id},
            data=content_item
        )
        utils.logger.info(f"[BiliMongoStoreImplement.store_content] Saved video {video_id} to MongoDB")

    async def store_comment(self, comment_item: Dict):
        """
        Store comment to MongoDB
        Args:
            comment_item: Comment data
        """
        comment_id = comment_item.get("comment_id")
        if not comment_id:
            return

        await self.mongo_store.save_or_update(
            collection_suffix="comments",
            query={"comment_id": comment_id},
            data=comment_item
        )
        utils.logger.info(f"[BiliMongoStoreImplement.store_comment] Saved comment {comment_id} to MongoDB")

    async def store_creator(self, creator_item: Dict):
        """
        Store UP master information to MongoDB
        Args:
            creator_item: UP master data
        """
        user_id = creator_item.get("user_id")
        if not user_id:
            return

        await self.mongo_store.save_or_update(
            collection_suffix="creators",
            query={"user_id": user_id},
            data=creator_item
        )
        utils.logger.info(f"[BiliMongoStoreImplement.store_creator] Saved creator {user_id} to MongoDB")


class BiliExcelStoreImplement:
    """Bilibili Excel storage implementation - Global singleton"""

    def __new__(cls, *args, **kwargs):
        from store.excel_store_base import ExcelStoreBase
        return ExcelStoreBase.get_instance(
            platform="bilibili",
            crawler_type=crawler_type_var.get()
        )


class BiliKafkaStoreImplement(AbstractStore):
    """Bilibili Kafka storage implementation"""

    def __init__(self):
        import logging
        from confluent_kafka import Producer
        import config
        
        self.logger = logging.getLogger(__name__)
        self.kafka_config = {
            'bootstrap.servers': getattr(config, 'KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092'),
            'client.id': 'bilibili-crawler',
            'acks': 'all',
            'retries': 5,
            'batch.size': 16384,
            'linger.ms': 5,
            'compression.type': 'snappy'
        }
        
        self.logger.info(f"[BiliKafkaStoreImplement.__init__] Initializing Kafka producer with config: {self.kafka_config}")
        try:
            self.producer = Producer(self.kafka_config)
            self.logger.info(f"[BiliKafkaStoreImplement.__init__] Kafka producer initialized successfully with bootstrap servers: {self.kafka_config['bootstrap.servers']}")
        except Exception as e:
            self.logger.error(f"[BiliKafkaStoreImplement.__init__] Failed to initialize Kafka producer: {e}")
            self.producer = None

    def _delivery_report(self, err, msg):
        """Delivery report callback"""
        if err is not None:
            self.logger.error(f"[BiliKafkaStoreImplement._delivery_report] Message delivery failed: {err}")
        else:
            self.logger.info(f"[BiliKafkaStoreImplement._delivery_report] Message delivered to {msg.topic()} [{msg.partition()}] with offset {msg.offset()}")

    async def store_content(self, content_item: Dict):
        """
        Store video content to Kafka
        Args:
            content_item: Video content data
        """
        import json
        from datetime import datetime
        if not self.producer:
            self.logger.error("[BiliKafkaStoreImplement.store_content] Kafka producer not initialized")
            return
            
        try:
            topic = getattr(config, 'KAFKA_TOPIC_VIDEO_CONTENT', 'bilibili_video_content')
            video_id = content_item.get('raw_id', 'unknown')
            title = content_item.get('title', 'unknown')
            
            self.logger.info(f"[BiliKafkaStoreImplement.store_content] Preparing to send video {video_id} to Kafka topic: {topic}")
            
            # 直接使用传入的content_item作为消息内容
            message = json.dumps(content_item, ensure_ascii=False).encode('utf-8')
            
            self.producer.produce(
                topic=topic,
                value=message,
                callback=self._delivery_report
            )
            self.producer.poll(0)
            self.producer.flush()
            self.logger.info(f"[BiliKafkaStoreImplement.store_content] Video {video_id} sent to Kafka topic: {topic}, title: {title[:50]}...")
        except Exception as e:
            self.logger.error(f"[BiliKafkaStoreImplement.store_content] Failed to send video content to Kafka: {e}")

    async def store_comment(self, comment_item: Dict):
        """
        Store comment to Kafka
        Args:
            comment_item: Comment data
        """
        import json
        from datetime import datetime
        if not self.producer:
            self.logger.error("[BiliKafkaStoreImplement.store_comment] Kafka producer not initialized")
            return
            
        try:
            topic = getattr(config, 'KAFKA_TOPIC_COMMENT', 'bilibili_comment')
            comment_id = comment_item.get('comment_id', 'unknown')
            video_id = comment_item.get('raw_id', 'unknown')
            
            self.logger.info(f"[BiliKafkaStoreImplement.store_comment] Preparing to send comment {comment_id} for video {video_id} to Kafka topic: {topic}")
            
            # 直接使用传入的comment_item作为消息内容
            message = json.dumps(comment_item, ensure_ascii=False).encode('utf-8')
            
            self.producer.produce(
                topic=topic,
                value=message,
                callback=self._delivery_report
            )
            self.producer.poll(0)
            self.producer.flush()
            self.logger.info(f"[BiliKafkaStoreImplement.store_comment] Comment {comment_id} sent to Kafka topic: {topic}")
        except Exception as e:
            self.logger.error(f"[BiliKafkaStoreImplement.store_comment] Failed to send comment to Kafka: {e}")

    async def store_creator(self, creator: Dict):
        """
        Store creator to Kafka - disabled
        Args:
            creator: Creator data
        """
        self.logger.info("[BiliKafkaStoreImplement.store_creator] Creator storage is disabled")
        return

    async def store_contact(self, contact_item: Dict):
        """
        Store contact to Kafka - disabled
        Args:
            contact_item: Contact data
        """
        self.logger.info("[BiliKafkaStoreImplement.store_contact] Contact storage is disabled")
        return

    async def store_dynamic(self, dynamic_item: Dict):
        """
        Store dynamic to Kafka - disabled
        Args:
            dynamic_item: Dynamic data
        """
        self.logger.info("[BiliKafkaStoreImplement.store_dynamic] Dynamic storage is disabled")
        return
