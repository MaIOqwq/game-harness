@echo off
rem ============================================================================
rem 把这份复制成 config.bat, 填上你自己的值。start.bat 会自动 call config.bat。
rem config.bat 里有密钥, 别外传、别提交到任何仓库。这个 .example 里没有任何真值。
rem ============================================================================

rem ---- 必填: 回答用的模型(没有它问不出答案) ----
set DEEPSEEK_API_KEY=

rem ---- 可选: 情感/反讽分析服务(nlp-tool) ----
rem 不填 = 本轮度量走「关键词降级」, 页面上会如实标出来。照样能出答案, 只是情感那条线粗一点。
rem 那个服务要额外下模型权重(很重), 分发包里没带。
rem set NLP_TOOL_URL=http://127.0.0.1:8772
rem set NLP_TOOL_TOKEN=
rem 或者指到它的 token 文件:
rem set NLP_TOOL_TOKEN_FILE=<nlp-tool 目录>\.tool_token

rem ---- 可选: 爬虫按需启停的旋钮 ----
rem 爬虫平时不在跑, 问到它才起、闲久了自动关(默认 600 秒)。调小 = 更省内存, 但隔一会儿再问就得
rem 重新等一次冷启动(要拉浏览器, 十几秒)。
rem set HARNESS_IDLE_SEC=600
rem 彻底关掉按需拉起(测试/排障用)。关掉之后爬虫得自己手工起, 网页上那两盏灯会一直是「未启动」:
rem set HARNESS_NO_LAZY=1

rem ---- 可选: 调试开关 ----
rem 关掉每题的流水落盘(默认写 harness\data\flow\*.jsonl), 测试时用:
rem set HARNESS_FLOW=0
rem 关掉反讽 judge 那一步(省 token, 可疑样本直接用模型标签):
rem set NLP_JUDGE=0

rem ============================================================================
rem 两个平台的登录凭据都**不在这里填**。起服务后到网页上点那盏红灯 →「去修复」→ 扫码
rem (NGA 用 NGA App 扫、B站用 B站 App 扫)。扫完灯自己转绿, 爬虫服务不用重启。
rem 实在扫不了码: NGA 的凭据在 crawlers\nga\config.json 的 "cookies" 段, 可以手工填;
rem bilibili 的存在 crawlers\bili\browser_data\ 整个目录里, 不如扫码省事。
rem ============================================================================
