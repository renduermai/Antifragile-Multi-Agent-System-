# 阶段 5 案例区

案例 TXT 支持 UTF-8 文本粘贴和上传。每个标记单独成行，时间需要时区。下面是只用于说明格式的合成片段。

```text
@symbol TEST
@round 2025-02-07T16:00:00-05:00
@evidence notice-1 2025-02-07T15:45:00-05:00 https://example.invalid/notice-1
TEST 第一轮公开材料原文。
@round 2025-02-10T09:30:00-05:00
@evidence notice-2 2025-02-09T13:00:00-05:00 https://example.invalid/notice-2
TEST 第二轮新增材料原文。
```

- `@symbol` 可省略。要对照标的或期权时，填写实际标的代码。首轮至少有一条新闻或材料。各轮可只写新增内容，系统会纳入前轮可见材料。
- `@round` 指当时的决策截断时间。各轮严格递增。`@evidence` 后依次填写唯一 ID、材料版本的公开时间和原始来源地址。多行原文延续到下一个标记。
- 如同一篇文章后来被改写，使用新版本的公开时间和新证据 ID。预览时核对引文、时间和来源。发表时间无法核实时，请先补齐来源或移出该轮。
- 可从选定的单轮启动。继续同一运行会从原案例版本读取下一轮，并保存新的预测 ID。改用新 TXT、模型或规则时新建运行，旧快照可以复查。相同验证版本和评分规则下的总分适合对照。
- 模型每轮最多调用一次，和灵石区的大语言模型探针及机会扫描共用每日预算。评分由 Python 执行。

## 可选归档期权

每条 `@quote` 的格式与 `@evidence` 相同，正文填写一个 JSON 对象。模板字段如下。报价实际发生时点需要早于或等于公开时间和轮次截断时间。归档无法确认 NBBO 或标准交割时，保留缺口或填写实际已知状态。报价按美元每股。

```text
@quote option-1 2025-02-07T15:59:00-05:00 https://example.invalid/archive-option
{"underlying":"TEST","observed_at":"2025-02-07T15:59:00-05:00","quote_kind":"NBBO","standard_deliverable":true,"snapshot":{"details":{"ticker":"O:TEST250314C00100000","contract_type":"call","exercise_style":"american","expiration_date":"2025-03-14","strike_price":100,"shares_per_contract":100},"last_quote":{"bid":0.9,"ask":1.0,"bid_size":25,"ask_size":30},"open_interest":160}}
```

归档报价材料在独立概率判断已保存后才进入 Python 期权估值。历史 Bid/Ask 只用于条件模拟。案例运行不会回补当前网页，也不会读取机会猎场的新闻、行情或记忆。

## 验证 TXT

使用 JSON 格式的 TXT。每个待评测时点填写一个条目，截断时间与预测中的时点对应。不同的时区写法只要代表同一时刻即可。仅填写有可核对依据的可选字段。验证原文可保存所有轮次，A12 对每份预测只提取对应条目。

```json
{
  "rounds": [
    {
      "cutoff": "2025-02-07T16:00:00-05:00",
      "outcome": {
        "summary": "TEST 实际结果摘要",
        "branch": "up",
        "event": true,
        "occurred_at": "2025-02-09T10:00:00-05:00",
        "published_at": "2025-02-09T13:00:00-05:00",
        "symbol": "TEST",
        "terminal_price": 120.0,
        "terminal_at": "2025-03-14T16:00:00-04:00",
        "price_url": "https://example.invalid/archived-close"
      },
      "reference": {
        "evidence_ids": ["notice-1"],
        "conflict_ids": [],
        "notes": "TEST 参考分析与可追溯的评分依据",
        "corrections": []
      }
    }
  ]
}
```

- `outcome` 记录观察到的实际结果。`branch` 可为 `base`、`up` 或 `down`，按该预测已锁定的情景含义填写。`event` 指该预测联合概率中的催化事件。`terminal_price` 对照同一标的未复权美元结算价，并配同一记录的标的、时间、来源。
- `reference` 独立记录参考答案。关键证据和矛盾只评估当轮可见的证据，未提供给当轮的材料单列。纠错可填写 `corrections` 数组，每项提供 `forecast_text`（预测中的逐字原文）、`actual_fact` 和 `url`。评测会标明引文、可追踪的纠错和未匹配的参考条目。
- 同一条目还可提供 `exit_quote`，包含 `ticker`、`observed_at`、`bid`、`bid_size` 和 `url`。系统将归档 Ask 与归档 Bid 合成报价口径收益估算。可选 `actual_trade_pnl_usd` 独立记录用户提供的真实交易净损益。
- 点击该轮已锁定预测下的「开始验证」。可核对的分项等权汇总，缺少基准的一项显示未评分。单条 Brier 分数用于核对一次概率判断，跨案例才可评估校准。空仓按当时证据和风险条件评估。
- 真实材料的公开时间需要人工核对。测试可证明给模型的输入隔离，模型预训练已知信息仍需要结合引文和人工复核检查。

## 数据和复验

案例原文、预测快照、验证原文和评测结果分别由阶段 5 的案例输入、预测和验证模块访问。案例 TXT 位于 `cases.sqlite3`，预测在 `predictions.sqlite3`，验证 TXT 和评测结果在 `validation.sqlite3`。正式三个案例沿用现有档案，合成测试仅写入临时数据目录。

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_stage5.py
```

