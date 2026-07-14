# track-premarket-sell

A1-A5 开盘条件卖出覆盖所有 5 个 track。

## Requirements

- A1-A5 段遍历 `tracks` 字典（而非仅 `positions`/`capital`）
- 每个 track 独立检查退潮清仓、MA5 破位、时间止损、低开闪卖
- record_trade 传入正确的 `strategy` 参数
- chen track 的 trades_list 记录保持原有行为
