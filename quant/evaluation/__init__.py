"""因子评估包 — 五阶段标准流程 (CPCV + PBO + walk-forward).

模块:
  cpcv.py          —  Purged Walk-Forward Cross-Validation (De Prado 2018 Ch.7)
  pbo.py           —  Probability of Backtest Overfitting (De Prado 2018 Ch.8)
  phase1_data.py   —  Stage 1: 数据准备
  phase2_single.py —  Stage 2: 单因子 IC/t/ICIR/half-life 检验
  phase3_oos.py    —  Stage 3: CPCV OOS 检验 + PBO
  phase4_costs.py  —  Stage 4: 交易成本后验证
  phase5_monitor.py—  Stage 5: 持续监控 (因子拥挤度/衰减/换手率/容量)
"""
