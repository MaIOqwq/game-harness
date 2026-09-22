# 记忆系统 & scope 隔离

真源：`PROJECT_MEMORY.md §2 记忆框架` + `§3 9/8 补记`；`docs/ARCHITECTURE.md`；`harness/db.py`、`state_card.py`、`archive.py`、`session_log.py`、`schema.sql`。本文件只存跨主题结论，不复述。

## 结论
- L1 状态卡 materialize-then-prune（证据/结论不在可裁剪区）✅；L2 checkpointer（`harness/checkpoint.py`，09-09 转活：每 scope 一份卡 JSON，Session 每轮落/冷启动续接，save/clear 全 INSERT OR REPLACE）✅；L3 archive = crawl_run(verbatim) + observation(dedup append-only) + session_claim(翻篇日志) ✅；L4 alias registry（`harness/registry.py`，09-09 转活：黑话→规范名账本式，最新行 id 序生效，Session 播种 + engine `_alias_context`）。单 SQLite `harness/data/harness_dev.db`（`HARNESS_DB` 可换）。真源含 `checkpoint.py`/`registry.py`。
- 本机向量记忆库（对话侧 add_turn）与 harness 会话记忆是**两套系统**，别混。

## 事故（为什么要隔离）
live run2 双进程并发打同库/同隧道 → **串题**（Q2 搜归档能见 Q1 爬的样本）+ run1/run2/冒烟残留全堆 prod → 结论只随翻篇落库、出了岔无法归因到具体某次标定。

## 修法（scope 键，逐层）
① 三表各加 `scope` 列（db.py 运行时 ALTER 迁移旧库，先补列再跑 schema）② 键隔离：session_claim 主键前缀 `scope::`、observation.dedup 前缀 `scope|` → 跨 scope 永不撞 PK/UNIQUE ③ 读写全按 scope 过滤（工具经 `ctx["scope"]`）④ 边界提交：`set_scope()` 先 `_flush_active()` 把旧 scope 活跃结论翻篇进**旧**日志；平台冷却 `_plats` 保留跨题沿用 ⑤ 标定逐题 `exp:calib:<tag>:qN` + 收尾 flush ⑥ 旧残留移 `exp:legacy:*`，主库 prod 空。

## 教训
实验/标定数据与真实会话必须隔 namespace，否则结论污染不可归因。验证：`_tmp/test_scope_iso.py` 25/25。
