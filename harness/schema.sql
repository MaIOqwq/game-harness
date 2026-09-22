-- 游戏社区问答 Agent 记忆框架 DDL
-- 本地开发 SQLite; 线上 MariaDB 同构(utf8mb4), 迁移时改类型: MEDIUMTEXT/TEXT 通用,
--   INTEGER 主键在 MariaDB 为 BIGINT AUTO_INCREMENT。
-- L3 存档: 一次爬取 = 一条 crawl_run + 逐条 observation (append-only, verbatim, 永不遗忘)
-- scope = 出处标记: 'prod'=真实会话(CLI); 'web:<会话号>'=网页会话; 'exp:<tag>:<case>'=实验/标定。
--   **记忆**(session_claim/checkpoint)按 scope 严格隔离, 读写都过滤, 互不可见;
--   **证据**(crawl_run/observation)只按"池"隔离(见 evidence.py): exp:* 各自成池, 其余同一台机器
--   共享一池 —— 会话之间不串结论/上下文, 但先前爬到的料换个会话仍可检索可引用。
--   dedup_key 前缀落的是**池名**, 故同一池内跨会话重复爬到同一条只算 dup, 不会存两份。
CREATE TABLE IF NOT EXISTS crawl_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT,
  game_key TEXT,
  platform TEXT,
  started_at TEXT,
  finished_at TEXT,
  row_count INTEGER,
  raw_payload TEXT,       -- 原始返回 verbatim, 永不改写
  scope TEXT NOT NULL DEFAULT 'prod'
);
CREATE TABLE IF NOT EXISTS observation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  crawl_id INTEGER REFERENCES crawl_run(id),
  game_key TEXT,
  platform TEXT,
  kind TEXT,
  title TEXT,
  text TEXT NOT NULL,
  author TEXT,
  raw_id TEXT,
  url TEXT,
  published_at TEXT,
  crawled_at TEXT,
  source_query TEXT,
  dedup_key TEXT UNIQUE,  -- 去重(值已含 scope 前缀 => 按 scope 隔离)
  senti_arg INTEGER,      -- 本地度量缓存(情绪 0/1/2), 训练好模型后填充, 不每答重算
  sarcasm INTEGER,
  topic_tags TEXT,
  view_count INTEGER,     -- 量化热度(采集当时快照, 老行拿不回=空): 播放量(bili 视频, NGA 无)
  like_count INTEGER,     -- 点赞数(bili 视频/评论, NGA 热评)
  reply_count INTEGER,    -- 回复数(NGA 主帖, bili 视频)
  danmaku_count INTEGER,  -- 弹幕数(bili 视频)
  video_tags TEXT,        -- bili 视频自带标签(逗号拼接); 与 NLP 口径的 topic_tags 区分开
  sub_text TEXT,          -- bili 视频 AI 字幕全文(高热度视频才取; 空=没取到/没取). 不走 text 的截断口径
  scope TEXT NOT NULL DEFAULT 'prod'
);
CREATE INDEX IF NOT EXISTS idx_obs_game_time ON observation(game_key, published_at);
CREATE INDEX IF NOT EXISTS idx_obs_scope ON observation(scope, published_at);
-- 前台出题卡要按"这一问爬的 run"取样本(webview.rows_of_runs), 每次开页面/切会话都要走这条
CREATE INDEX IF NOT EXISTS idx_obs_crawl ON observation(crawl_id);

-- L1/L2 会话侧结论日志: 翻篇结论从状态卡移入, 离上下文, 按需回捞
-- cid 主键值落库时前缀 scope(scoped::cid) => 跨 scope 永不撞主键
CREATE TABLE IF NOT EXISTS session_claim (
  cid TEXT PRIMARY KEY,
  q TEXT,
  concl TEXT,
  ev TEXT,
  moved_at INTEGER,
  topic TEXT,
  scope TEXT NOT NULL DEFAULT 'prod'
);

-- L4 注册表: 黑话/别名 -> 游戏, 版本化账本 supersede-not-overwrite。
-- "生效" = 该 alias 最新(id 最大)行; 旧行留在账本不 overwrite, 语义由 id 序替代, 不用 UPDATE valid_until 关旧。
CREATE TABLE IF NOT EXISTS alias_resolution (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alias TEXT NOT NULL,
  game_id INTEGER NOT NULL,
  confidence REAL DEFAULT 0.5,
  hits INTEGER DEFAULT 0,
  last_confirmed_at TEXT,
  confirmed_by TEXT,
  source TEXT,
  valid_from TEXT NOT NULL,
  valid_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_alias_active ON alias_resolution(alias, valid_until);

-- L2 检查点: 会话状态卡跨进程续接(scope 主键, 每 scope 一份"当前进度")。
-- 只增契约下 save/clear 都走 INSERT OR REPLACE(不留 DELETE); 会话层可软删, 物理清除是前台删除按钮的独立任务。
CREATE TABLE IF NOT EXISTS checkpoint (
  scope TEXT PRIMARY KEY,
  card TEXT NOT NULL,        -- StateCard.snapshot() + engine 编号 的 JSON
  saved_at INTEGER NOT NULL
);

-- 垃圾桶: 前台「删除这个对话」的落点。删 = 把这一份记忆**整段搬进来**(行数/流水原文都原样存),
-- 活的那一侧(checkpoint/session_claim/flow)当场清空 —— 所以删完再进这个对话, 是从零开始, 不是接着旧卡答。
-- 7 天内可以整段搬回去(restore); 过期由 sweep 真删这一行, 那才是物理清除。
-- 存的是**会话记忆**(用户自己删的东西), 与证据池无关: crawl_run/observation 一行不碰。
CREATE TABLE IF NOT EXISTS trash (
  scope TEXT PRIMARY KEY,
  title TEXT,                -- 前台那一行显示的标题(库这侧不知道, 由页面带过来), 只为垃圾桶里认得出
  deleted_at INTEGER NOT NULL,
  expire_at INTEGER NOT NULL,
  card TEXT,                 -- checkpoint 那一行的 card; 没内容则 NULL
  claims TEXT,               -- session_claim 各行(JSON 数组); 没内容则 NULL
  flow TEXT                  -- 流水文件原文(jsonl 文本); 无条件保留, 恢复后答案全文还在
);
CREATE INDEX IF NOT EXISTS idx_trash_expire ON trash(expire_at);
