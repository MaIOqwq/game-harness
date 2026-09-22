# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/words.py
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


import asyncio
import json
import logging
import os
from collections import Counter

import aiofiles
import jieba
import matplotlib.pyplot as plt
from wordcloud import WordCloud

import config
from tools import utils

plot_lock = asyncio.Lock()


def _resource_candidates(name: str, configured: str) -> list:
    """词云可选资源(停用词表/中文字体)的真实候选路径, 按顺序找:
    配置里给的 -> 本包旁的 docs/ -> 发行包根的 docs/。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/words.py -> 包根
    out = []
    for path in (configured,
                 os.path.join(here, "docs", name),
                 os.path.join(here, os.pardir, os.pardir, "docs", name)):
        path = os.path.normpath(path) if path else path
        if path and path not in out:
            out.append(path)
    return out


def _find_resource(name: str, configured: str):
    """找到返回路径, 一处都没有返回 None。词云是可选功能: 资源缺了只跳过, 不该把采集带走。"""
    for path in _resource_candidates(name, configured):
        if path and os.path.exists(path):
            return path
    return None


def _resource_hint(name: str, configured: str) -> str:
    """打印用: 找过哪些地方。"""
    return " | ".join(_resource_candidates(name, configured))


class AsyncWordCloudGenerator:
    def __init__(self):
        logging.getLogger('jieba').setLevel(logging.WARNING)
        self.stop_words_file = _find_resource("hit_stopwords.txt", config.STOP_WORDS_FILE)
        self.font_path = _find_resource("STZHONGS.TTF", config.FONT_PATH)
        self.lock = asyncio.Lock()
        self.stop_words = self.load_stop_words()
        self.custom_words = config.CUSTOM_WORDS
        for word, group in self.custom_words.items():
            jieba.add_word(word)

    def load_stop_words(self):
        """停用词表找不到就返回空表并打印原因(本部署 docs/ 下确实没有这个文件), 不抛 FileNotFoundError。"""
        if not self.stop_words_file:
            utils.logger.warning(
                "[words] 没找到中文停用词文件, 停用词按空表处理(找过: %s)"
                % _resource_hint("hit_stopwords.txt", config.STOP_WORDS_FILE))
            return set()
        with open(self.stop_words_file, 'r', encoding='utf-8') as f:
            return set(f.read().strip().split('\n'))

    async def generate_word_frequency_and_cloud(self, data, save_words_prefix):
        all_text = ' '.join(item['content'] for item in data)
        words = [word for word in jieba.lcut(all_text) if word not in self.stop_words and len(word.strip()) > 0]
        word_freq = Counter(words)

        # Save word frequency to file
        freq_file = f"{save_words_prefix}_word_freq.json"
        async with aiofiles.open(freq_file, 'w', encoding='utf-8') as file:
            await file.write(json.dumps(word_freq, ensure_ascii=False, indent=4))

        # 中文字体缺了就出不了图(画出来全是方块/直接报错), 跳过词云并打印一句原因, 不抛异常
        if not self.font_path:
            utils.logger.warning(
                "[words] 跳过词云: 没找到中文字体文件(找过: %s)"
                % _resource_hint("STZHONGS.TTF", config.FONT_PATH))
            return

        # Try to acquire the plot lock without waiting
        if plot_lock.locked():
            utils.logger.info("Skipping word cloud generation as the lock is held.")
            return

        await self.generate_word_cloud(word_freq, save_words_prefix)

    async def generate_word_cloud(self, word_freq, save_words_prefix):
        await plot_lock.acquire()
        top_20_word_freq = {word: freq for word, freq in
                            sorted(word_freq.items(), key=lambda item: item[1], reverse=True)[:20]}
        wordcloud = WordCloud(
            font_path=self.font_path,
            width=800,
            height=400,
            background_color='white',
            max_words=200,
            stopwords=self.stop_words,
            colormap='viridis',
            contour_color='steelblue',
            contour_width=1
        ).generate_from_frequencies(top_20_word_freq)

        # Save word cloud image
        plt.figure(figsize=(10, 5), facecolor='white')
        plt.imshow(wordcloud, interpolation='bilinear')

        plt.axis('off')
        plt.tight_layout(pad=0)
        plt.savefig(f"{save_words_prefix}_word_cloud.png", format='png', dpi=300)
        plt.close()

        plot_lock.release()
