# -*- coding: utf-8 -*-
"""SQLite 连接 + schema 初始化。线上切 MariaDB 时换驱动, 表结构见 schema.sql。"""
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
# 空串必须当"没设": harness.env 里 HARNESS_DB= 就是空的(分发版默认留空), 而
# os.environ.get(k, 默认) 在 k 存在但为空时返回的是空串 —— 空路径 dirname 也是空,
# makedirs("") 直接 FileNotFoundError。2026-09-16 在服务器上真踩到(web.log 里那句
# "垃圾桶清扫跳过(开不了库)"), 后果不只是扫地: 建会话也走这里, 整站会连库都开不了。
DEFAULT_DB = (os.environ.get("HARNESS_DB") or "").strip() or os.path.join(
    HERE, "data", "harness_dev.db")


def connect(path=DEFAULT_DB):
    """开一条连接。

    **必须 check_same_thread=False**: 网页后端把 Session 按会话缓存起来、而**每一题都开一条新线程**
    去跑(webapp._run_ask), 所以"建连接的那条线程"和"用它的那条线程"天然不是同一条。
    默认的 check_same_thread=True 会让**同一会话的第二题**直接 ProgrammingError ——
    第一题好好的、第二题必挂, 用户看到的就是"一个会话答不了两句"。
    放开检查是安全的: 本机 sqlite3.threadsafety=3(串行模式), 语句级并发由 SQLite 自己串行化,
    且同一会话的提问在 webapp 那侧本来就有按会话的锁串着(见 _session_lock)。
    2026-09-15 真跑捉到, 见 _syscheck/verify_threaded_session.py。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_RUNTIME_COLS = [  # (table, col, ddl): CREATE TABLE IF NOT EXISTS 不补已存在库的列, 需运行时 ALTER
    ("crawl_run", "scope", "TEXT NOT NULL DEFAULT 'prod'"),
    ("observation", "scope", "TEXT NOT NULL DEFAULT 'prod'"),
    ("session_claim", "scope", "TEXT NOT NULL DEFAULT 'prod'"),
    # 09-12: 量化热度字段回补(画图表/当论据用)。老行无值=NULL, 不编不填(采集当时快照拿不回)。
    ("observation", "view_count", "INTEGER"),
    ("observation", "like_count", "INTEGER"),
    ("observation", "reply_count", "INTEGER"),
    ("observation", "danmaku_count", "INTEGER"),
    ("observation", "video_tags", "TEXT"),
    # 09-17: bili 视频 AI 字幕全文(只对取过字幕的高热度视频有值; 空=没取到/没取)
    ("observation", "sub_text", "TEXT"),
]


def _ensure_column(conn, table, col, ddl):
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.OperationalError:
        return
    if col not in cols:
        try:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))
        except sqlite3.OperationalError:
            pass  # 空库: schema.sql 的 CREATE TABLE 已带该列, 这里建表前的预迁移无需补


def init(conn, schema_file=None):
    # 先补列再跑 schema: 旧库缺 scope 列时, schema 里 idx_obs_scope 建索引会先炸
    for table, col, ddl in _RUNTIME_COLS:
        _ensure_column(conn, table, col, ddl)
    schema_file = schema_file or os.path.join(HERE, "schema.sql")
    with open(schema_file, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
