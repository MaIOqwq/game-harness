# 打标 / NLP / 评估

真源：`PROJECT_MEMORY.md §2`；`PLAN.md §10.1`(9/5,9/6)；`nlp/`、`labeler/`。本文件存结论 + 口径铁律。

## 现状
- LoRA StructBERT 双头（emotion 连续分 + sarcasm 可疑门）；产物 `nlp/out/structbert-lora-gold002/`；`eval_gold400.py` / `measure_teacher_noise.py`。
- 评测：gold400 情绪 Spearman **0.389**；反讽 F1 **0.368@th0.6**（gold001-only，>teacher 0.303）。
- 蒸馏流水线：DeepSeek 蒸馏打标 3594 → 人工 gold001(200 随机) / gold002(200 聚焦反讽)。

## 结论（根因 = 标注噪声，不是方法）
- teacher 自身反讽仅 F1 0.458 → **蒸馏自噪是主瓶颈**；gold002（干净反讽正例）并训后反超 teacher。
- 领土 = **gacha（二游抽卡域）**，不做全游戏（瓦/黑猴不在）。反讽可疑门 `th=0.6`；judge 导流阈 `0.4`，`cap 40`，temp0 + few-shot 覆写；服务挂 → 降级 keyword 不炸。

## 口径铁律
- **09-10 M2 改写（正源 PLAN §10.20/§10.21）**：**不再扩标/复核/追小模型指标**（gold001/002 各 200 只作验证切片；反讽 F1 0.368 / Spearman 0.389 定格为历史记录）。
- **定位分离（用户 09-10 点明「NLP 告诉 LLM 每条爬回数据是正面还是负面」）**：小模型 StructBERT 的章 = **每条样本软信号（senti 负/中/正 + sarcasm 可疑门→judge 覆写 + topic_tags），随 crawl/search evidence 行喂主链 LLM 判向**；engine 系统提示带【软信号提示】解读（sarcasm=1 别当采信/senti=负 提示负面，判向以原文为准）。情绪分布另喂图表 argmax 负面。**不追求 F1、不接硬判定闸、不因不准摘除（不准恰是"软"）**。舆情纵深 determiner（黑话 referent/反串阴阳/自基线**最终判定**）= **主链 LLM 引原文判定**，判不了明说。
- 指标/图表用 **argmax 负面/概率**，绝不看分数符号。
- 标签质量不高宁可不给 LLM（错误标签比原文更有害）——软信号≠金标，喂给 LLM 时明文标注是初筛。
