# 因子层全量代码审查报告 (2026-08-18)

范围: `quant/factor/` 全部 25 个文件 (compute 引擎/缓存 store/IC 评估/注册表/CLI) + `quant/data/repos/factor_repo.py`, 约 1.3 万行。
方法: 逐文件静态审查 (前视/PIT、双路径一致、NaN 传播、缓存一致性、零 fallback、状态机), 关键路径已 AST 验证 + 实测 DB schema/值域。
已排除: B2/B21/B26/B34/B35/B36 等已修复项; 物化走 shortcut 路径, 逐日 IC/golden 常走 raw 路径, 双路径分歧是本报告主线。

## P1 — 影响实盘/物化缓存正确性 (按修复优先级)

1. **`_primitives.py:507,1047-1052` + `materialize_segment.py:56-61` — market_beta_60d 物化恒死 (blocked-by-construction)**
   `_market_beta` 在 `zscore:market_beta_60d` 缺失时返回全 NaN; 而 zscore 面板仅在 precompute 时 `"benchmark_ret" in prims` 才构建, `benchmark_ret` 在 `precompute_primitives()` **之后**才加入 → 面板永不构建 → 物化全程 NaN → blocked。raw 路径 `missing.py:18-53` 另确认活前视 (`common_dates[-window:]` 未按 date 截断)。
   影响: 因子从物化池消失, 回归因子未贡献; 同机制下 residual_momentum_126d/idio_vol_126d 面板缺失但 shortcut 有 fallback, 不受影响。
   修复: precompute 前先注入 benchmark_ret (与 materialize_segment/ic.py 两处顺序统一), 或 `_market_beta` 补运行时 fallback 并加日志。

2. **`_preload.py:255-263` + `_dispatch.py` — analyst 快照 chunk 级复用 → 前视 + 顺序依赖**
   chunk 路径 analyst 取 `sync_date = MAX(sync_date) <= date_to` 整段复用, `slice_aux_for_date:381` 对 analyst **不按日期过滤** → chunk 内每个日期都用 chunk 末的分析师数据。受害因子: `compute_analyst_buy` (_event.py:421-453, iterrows last-wins **顺序依赖**, 重跑结果可能变)、`compute_analyst_consensus` (fundamental.py:167-183)。
   影响: 2020-2026 物化缓存中两因子的全部早期日期使用未来数据。
   修复: 与 fund_hold 同法, chunk 内按 sync_date ≤ date 切分; analyst_buy 改为按日期分组向量化。

3. **`store.py:1244-1330` — fundamentals 面板静态快照前视**
   total_mv/roe/eps/bvps 恒用 stocks **当前快照**; pe/pe_ttm/pb/market_cap 仅 daily_valuation 覆盖日 (约 2026-04 起) 后按日, 之前用当前快照。受害: size 系列、str/abn_turnover/ocfp/insider_increase/sue 的市值/股本中性化全部前视。
   影响: 2020-2026 全部历史日期市值类因子被污染; 回测选股隐含未来信息。
   修复: 市值/股本改用 daily_valuation.market_cap + `total_shares=market_cap/close` 按日重建, 或引入 stocks 历史快照表; v502 已为 industry 做过同款 (industry_history), 市值照抄该模式。

4. **`compute/price/_alternative.py:461-480` — compute_net_limit_ratio 恒死**
   全股票同值 → `_cs_zscore` (MAD 中位数) 恒 NaN。
   影响: 因子永远 blocked/NaN, 白占评估周期。
   修复: 删除或改为 circ_mv 归一化后做截面差。

5. **`compute/price/_alternative.py:487-516` — compute_trcf raw 活前视**
   `ts = turnover[sym].dropna()` 全 chunk + `ts.tail(w).mean()`, 未按 date 截断; shortcut `_trcf` (_primitives.py:764-775) 正确 → 双路径值分歧。
   影响: 逐日 IC/golden 与物化缓存值不同, 评估失真。
   修复: raw 路径改为 rolling 按 date 定位 (参照 compute_str B2 修复)。

6. **`missing.py:56-74,77-110` — overnight_gap_5d / vol_price_sync_20d raw 活前视**
   `.iloc[-1]`/`.iloc[-window:]` 取 chunk 尾部含未来行; 对应 shortcut 正确。同 5, 双路径分歧。

7. **`missing.py:119-229` — compute_piotroski_fscore aux 路径序错误**
   `_last_two` 假定 DESC 序取"最新两期", 但 aux 财务表 `ORDER BY stat_date` **ASC** (_preload.py:153/296, high_priority.py:136 注释确认) → 取**最旧两期**算 F-score, 值错; DB 路径 `ORDER BY stat_date DESC` 正确。
   影响: 物化缓存 F-score 值错误。
   修复: aux 路径按 stat_date 降序取最后两期, 或复用 DB 路径的排序参数。

8. **`_primitives.py:843-844` + `_alternative.py:176-236,239-330` — str/abn_turnover raw 市值中性化前视**
   shortcut 已移除 (`_str`/`_abn_turnover`), 走 raw 的 `compute_str`/`compute_abn_turnover` 用 `aux["stocks"]["total_mv"]` 当前快照中性化 → 同 3 的市值污染。
   修复: 中性化市值改为按日期重建 (同 3)。

9. **`compute/price/_momentum.py:550-580` vs `_primitives.py:671-680,316-317` — vol_price_corr 双路径量纲分歧**
   raw = Pearson(close 价格水平, volume 水平); shortcut = corr(pct_ret, vol_chg); doc 称 Spearman 实为 Pearson。三处不一致。
   影响: 物化缓存与 IC/golden 完全不同的因子定义。
   修复: 统一口径 (建议 pct_ret/vol_chg) 并修正 docstring。

10. **`store.py:490-500,922` — checkpoint resume 陈旧值 + data_hash 因子级**
    resume 丢弃 ≤ last_date 日期, 数据回填后旧日期不重算 (仅 failed_dates 重试); `_update_factor_meta` data_hash 因子级非日期级 → 回填后 hash 仍标记新鲜。
    影响: 增量物化后历史日期值永久陈旧且无感知。
    修复: hash 按 (factor, date) 粒度或回填时强制整段重算。

11. **`golden_test.py:145-150` — verify_strict 双路径校验形同虚设**
    两路 `compute_all_factors` 均未传 `primitives` → `_dispatch.py:113` 短路, 两路都走 raw → 从未对比 shortcut 与 raw。叠加 12 的 NameError, 该守卫完全失效。
    影响: 本次报告的 5/6/8/9 等双路径分歧全部漏检。
    修复: 传 `primitives=precompute_primitives(data)` + 注入 benchmark_ret, 且先修 12。

12. **`_preload.py:146,154` — preload_aux_data 单日路径必崩 (NameError)**
    `date_from`/`date_to` 未定义 (v523 重构残留)。调用方: golden_test 三命令、platform.py:619 (FactorTestRunner/CLI test/pipeline test stage)、任何不传 preloaded_aux_chunk 的 compute_all_factors 调用。
    影响: 因子回归/冒烟测试整套瘫痪; 单日评估路径全废。
    修复: 单日版改为 `date - financial_lookback_days`/`date`, 或直接删除单日版强制走 chunk。

13. **`fundamental.py:430-461,848-857,1077-1159` — insider_increase/sue/ocfp 市值股本快照前视 + 连接泄漏**
    total_mv/total_shares 取 stocks 当前快照 (同 3); `compute_ocfp:1138` count<30 时 `return raw` 前未 `conn.close()` → 连接泄漏; 且返回未命名未标准化原始序列。
    修复: 同 3 重建市值; 泄漏分支补 close (或统一用 with/aux)。

14. **`fundamental.py:1197-1225` — compute_insider_cluster 恒死 (值域不符, 已实测)**
    代码过滤 `direction IN ('增加','增持','买入')`, 实测 `holder_trade.direction` 仅 `'in'(21966)/'out'(59762)` → 恒空。
    修复: 改为 `direction='in'`; 并检查同类硬编码中文值域 (margin 表等)。

## P2 — 评估/工具链正确性或潜在污染

15. **`expr_compiler.py` — ts_rank/比较运算符静默全 NaN**: `_TS_FNS` 含 'rank' 但 evaluate 无分支; `>=/<=/!=` 解析后未处理。修复: 实现或显式报错 (零 fallback)。

16. **财报 pub_date 缺失**: `fundamental.py:191-208` 及 revenue/earnings/piotroski 用 `stat_date <= date` 无 `pub_date <= date` 过滤 (financial 表含 pub_date, store.py:121 指纹) → 边界季度公告前数据入因子。修复: 查询加 pub_date 过滤。

17. **`_primitives.py:58-62` — `_data_hash` 仅 shape/index/columns 不含内容**: 同区间数据内容修正不失效磁盘缓存 (stats 层), 与 10 叠加。

18. **IR 年化口径不统一**: `stats_cache.py:790` IR = mean/std×√(252/lookback) (lookback=120 时 ≈×1.45); `ic.py:330` IR = mean/std 不年化; `marginal.py:59` t=IR×√n_days 依赖前者口径。三者混用 → 因子排序/淘汰标准漂移。修复: 统一为同一公式并在调用处注明口径。

19. **`ic.py:194,192` — 死代码 + 每日重复开销**: `ds_prims` 算而不用; `get_fundamentals` 每日期查询未用。修复: 删除。

20. **`_alternative.py:93-121` — compute_ztd 缓存未命中静默全 NaN**: 零 fallback 违规, 任何未调 preload_ztd_cache 的调用方 (如单日评估) 得到全 NaN 因子无告警。修复: 缓存未命中时显式告警或回退 SQL。

21. **`_primitives.py:628-644` — compute_turnover_reversal 覆盖率检查双路径分歧**: raw 覆盖率<50 返回 None, shortcut 无此检查 → 低流动性日值分歧。

22. **`_momentum.py:46-73` vs `_primitives.py:719-742` — residual_momentum 基准分歧**: raw 用数据内 000300/截面均值 fallback, shortcut 用 DB benchmark_ret → 值分歧。修复: 统一注入 benchmark_ret。

23. **`platform.py:359-366,1008-1013` — CLI/PG 双缺陷**: (a) PG list 分支列错位 (category 起整体右移 1 位: source=category、dependencies=parameters…); (b) register 命令传 `direction=` 而 FactorMetadata 无此字段 → TypeError, register 必崩 (实测 grep 字段列表)。另 `--source` 与位置参数 source 重复定义。修复: 按 DDL 顺序修正映射; 移除 direction/去重 source。

24. **`marginal.py:102-113` — 边际贡献需 N≥3**: n=2 时 ic_others 空数组 @ 空矩阵报错被 LinAlgError 吞成"奇异" (误判)。修复: 提前判 n==2 退化为双因子情形。

## P3 — 低危/文档/性能

25. **compute_earnings_upgrade** (fundamental.py:1228-1261): doc 称"上调修正因子"实为水平因子, doc/代码不符; **compute_abn_turnover_resid** (_alternative.py:675-703): 实现用 volume 非 turnover, 与 doc 不符。
26. **`_dispatch.py:219-231`**: earnings decay 用 chunk 最新 stat_date (未来报告) → chunk 早期衰减低估。
27. **`_dispatch.py:167`**: financials_cache 键月粒度, 同月两次调用误命中。
28. **`_preload.py:444-446`**: `intraday_snapshot_days` 为 chunk 级总天数非 as-of-date, 门控在早 chunk 过松/晚 chunk 过严 (影响小: 空快照日返回 None 属正常)。
29. **`store.py:620`**: subprocess `PYTHONPATH=os.getcwd()` 依赖启动目录。
30. **`_event.py` LHB cutoff** 用日历日近似 (90d), 与交易日窗口定义不一致; **marginal_balance_chg** 0.0 中性约定。
31. **`intraday.py:78-81`** aux 路径 iterrows 构造 dict, 全 chunk 快照逐日重复扫描 (物化 250 日×5000 股) — 性能, 非正确性。
32. **`_huanfang.py`**: `compute_vp_divergence` 死代码 (map 内为编译表达式版)。
33. **`store_metadata.py:62-67`**: get_symbol_id/get_date_index 无缺键处理, KeyError 裸抛; symbol_dict.json 永不过期, 新股上市不体现。
34. **`marginal.py:49`**: `_require_cfg("factor.evaluation.n_days")` 与 ic.py 实际天数无校验, 传错配置 t 检验失效。
35. **`platform.py:749`**: `_stage_backtest` sharpe≥0.5 阈值无文献/数据来源依据, 违反"参数必须有依据"约定。

## 总体评价

因子计算核心 (shortcut/prims 体系) 设计优秀: 逐日截面 zscore 语义一致, windows/eff_days 无 chunk 边界缺陷, 大多数 raw 路径 B2 已修 PIT 正确。但存在三类系统性问题: **(1) 市值/股本类静态快照前视** (第 3/4/8/13 条) 污染全部历史物化缓存, 是本次最重问题, 回测结果需要重估; **(2) 双路径分歧** (5/6/8/9/21/22 条) 因 verify_strict 失效而全部漏检, 物化与评估两套口径并存; **(3) 两条"恒死"因子** (market_beta_60d、net_limit_ratio、insider_cluster) 和单日路径 NameError 说明 v523 引入的重构回归未被测试网捕获。建议按 1→14→12→11 顺序修复, 每项修完补一条双路径回归用例, 重建 verify_strict 使其真正生效。
