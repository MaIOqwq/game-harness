# -*- coding: utf-8 -*-
"""把自带工具跑成一个 MCP 服务: python -m harness.mcp

在支持 MCP 的客户端里填的就是这一行(名字随便起, 例如"游戏社区工具"):
    {"command": "python", "args": ["-m", "harness.mcp"], "cwd": "<装这个产品的目录>"}
"""
import os
import sys


def main():
    # 中文进出必须走 UTF-8: Windows 默认按 GBK 编解码, 协议里出现中文就会乱码/报错。
    for stream, kw in ((sys.stdin, {"errors": "replace"}),
                       (sys.stdout, {"newline": "\n"}),
                       (sys.stderr, {"errors": "replace"})):
        try:
            stream.reconfigure(encoding="utf-8", **kw)
        except Exception:
            pass
    # 与网页后端同一条理由: 产品形态默认真爬, 不打开就是拿编造的样本作答。
    os.environ.setdefault("HARNESS_LIVE", "1")
    from .server import serve_stdio
    serve_stdio()


if __name__ == "__main__":
    main()
