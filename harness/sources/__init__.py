# -*- coding: utf-8 -*-
"""爬虫 provider 共享异常: live 真爬失败时 RAISE(区别于"确实没内容"的空返回),
让引擎层断路器/退避锁能分辨 "平台挂了" vs "没搜到"。mock provider 不抛。"""


class CrawlError(Exception):
    def __init__(self, msg, platform=None, throttle=False):
        super().__init__(msg)
        self.platform = platform
        self.throttle = throttle
