# V3.2 阶段 B 接入验收

日期为 2026 年 11 月 24 日。MarketData 已配置并启用，Token 仅保存在服务端敏感配置。Massive 保存原配置并停用。开发前快照在 ../backups/v32-before-B-20261124。原有业务表和记录保留，runtime 新增 marketdata_requests 与 marketdata_cache，启动时幂等创建。

## 实际结果

使用真实账户发出 5 次有界请求，共由 X-Api-Ratelimit-Consumed 确认消耗 5 积分。获取 SPY 可用到期日、指定 2026-11-27 到期和 720—725 执行价范围的一张 Call、该 OCC 单合约的最近可得报价、11 月 20 日历史日终报价、11 月 19—20 日历史日终序列。合约为 SPY261127C00720000。期权链返回 Bid 2.89、Ask 3.00 美元每份，updated 为 2026-11-20T21:00:00Z，属于旧报价。两日历史查询返回两行。重复请求和浏览器到期日查询命中本地缓存。

最后一次响应报告额度上限 10,000、剩余 9,995，保存了原始消耗、剩余、重置时间响应头。这是当次响应报告的周期额度，后续以新响应为准。试用到期日尚未通过账户 API 核验。已按官方说明将至少 24 小时延迟作为试用提示，报价实际时间逐条保留。

本地默认每日预算 80 积分，按 America/New_York 的 09:30 划分周期。请求前通过 SQLite 写事务预留保守成本，实际有消耗响应头时对账，缺失时保留估算。网络结果未知时保留预留。链限定一个到期日、方向、执行价范围及最多 10 档，默认 3 档。历史范围单次最多 30 天间隔，保守预留 31 积分。最近可得查询缓存 15 分钟，明确历史日期缓存持久保留。不同端点、Token 和查询参数隔离缓存。缓存读取不重复消耗积分。并发中的预算预留会一起计入上限。

统一记录来源、合约、方向、到期、执行价、Bid/Ask/Last、交易量、未平仓量、IV/Greeks、标的参考价、报价时间、获取时间、币种、单位和原始响应。缺失字段保留未知。合约乘数、标准交割核验和当前执行资格尚未取得可靠支持，所有此阶段记录 execution_eligible=false。报价年龄单独计算，供应商确切延迟秒数未知。

## 使用方法

打开 http://127.0.0.1:8765/#settings，找到 MarketData 配置卡。可以启停、修改积分预算、保存、清除密钥及测试。测试使用 SPY 到期日接口，并受积分预算控制。

展开“查询期权数据”，可以选择到期日、限定期权链、单合约。填写标的 SPY 后查询到期日。链查询需填写到期日和执行价上下限。单合约填写 SPY261127C00720000，可选历史单日，或者历史起止日。重复上面实测条件可读取缓存。页面展示自然中文数据状态和报价时间。

接口为 POST /api/marketdata/query，operation 支持 expirations、chain、quotes。字段见 app/marketdata.py 的 Query。GET /api/marketdata/budget 返回本地预算及最近账户额度响应。Base URL 包含 /v1，实际路径从 /options 开始。灵石区 API Key 留空保存保留已有值。

## 测试与边界

原有加新增测试共 32 项通过。新增测试覆盖鉴权、路径、历史数据标记、缺失字段、缓存、预算拒绝、参数验证、禁用、权限失败、脱敏和失败积分。修改后的 MarketData 专项测试另通过。浏览器已验证启用状态、80 积分预算和缓存查询结果。

本阶段模型调用为 0，研究 Token 为 0。只接入数据层。下一阶段 C 将这些报价接入共同赔率计算和搜索降级，并处理延迟参考方案。当前仍没有用 MarketData 产生模拟买入决策。

官方依据包括 [期权链](https://www.marketdata.app/docs/api/options/chain/)、[合约报价](https://www.marketdata.app/docs/api/options/quotes/)、[到期日](https://www.marketdata.app/docs/api/options/expirations/)与[积分和限流](https://www.marketdata.app/docs/api/rate-limiting/)。Starter Trial 无法使用 mode=cached，采用本地缓存。

启动命令为项目目录下的 .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765。已有实例运行时直接访问页面。

