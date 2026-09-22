# -*- coding: utf-8 -*-
"""MOCK 爬虫 provider: 稳定返回 NGA 形状的 payload(帖子/回复)。
SEAM: 真爬虫(NGA 127.0.0.1:8770 / B站 127.0.0.1:8771, 常驻底座)接入点 = 同此 crawl() 接口,
只换实现不动调用方。mock 的 dedup_key 对同一 query 稳定, 用于测去重。
POOL 故意混入: 中性/黑泥/反串/强度meta, 让本地度量有得打。"""
import hashlib

POOL = [
    ("reply", "", "强度是真膨胀了,新C落地就把老C踩死,答辩角色还有人洗"),
    ("post", "新版本流水帖", "这版本抽卡体验如何?都抽到啥了"),
    ("reply", "", "你说保值就保值吧,真香,孝子接着洗"),
    ("reply", "", "数值抬得还行,没到膨胀,再观望观望"),
    ("reply", "", "骂归骂,晚上还是猛肝,我真是典"),
    ("reply", "", "退坑了,越出越离谱,不如去玩隔壁"),
    ("reply", "", "策划脑子有坑?老角色直接退役?"),
    ("reply", "", "楼上一看就没玩,这叫正常迭代,强度焦虑过头了"),
    ("reply", "", "好活,这波节奏我是乐子人,经典回旋镖"),
    ("reply", "", "反正我抽了,爽到,值"),
]


def crawl(query, game, platform, limit=8, since=None, until=None, windows=None):
    # since/until/windows 是真爬 provider 的时间窗入参; mock 无真实时间轴, 收下不用(保持接口同形)
    seed = int(hashlib.md5((game + "|" + platform + "|" + query).encode("utf-8")).hexdigest(), 16)
    items = []
    for i in range(limit):
        kind, title, text = POOL[(seed + i) % len(POOL)]
        items.append({
            "kind": kind, "title": title, "text": text,
            "author": "u_%d" % ((seed + i) % 97),
            "raw_id": "%s_%d" % (platform, (seed + i) % 1000),
            "url": "mock://%s/%d" % (platform, (seed + i) % 1000),
            "published_at": "2026-09-06T%02d:%02d:%02d" % (8 + (seed + i) % 12, (seed + i) % 60, 0),
            "dedup_key": "%s:%s_%d" % (platform, platform, (seed + i) % 1000),
        })
    return items
