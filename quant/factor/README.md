 # Factor Layer (Layer 2)

 Computes time-series and cross-sectional factors, evaluates their predictive power (IC/IR/decay), and synthesizes composite factors for the Alpha layer.

 ## Architecture

 ```
 factor/
 ├── compute/              # Factor computation functions (pure, vectorized)
 │   ├── price/            #   Price/volume factors (41 registered)
 │   │   ├── _momentum.py  #     Momentum, reversal, volatility, liquidity
 │   │   ├── _event.py     #     Event-driven (limit-up, LHB, margin)
 │   │   ├── _sentiment.py #     News sentiment factors
 │   │   └── _alternative.py #   Alternative data factors
 │   ├── fundamental.py    #   Fundamental factors (16 registered)
 │   ├── _registry.py      #   Registry loader (FactorRepo)
 │   ├── _dispatch.py      #   Factor dispatch engine
 │   └── _shared.py        #   Shared helpers
 ├── registry.py           # Factor state machine (active/monitoring/retired)
 ├── ic.py                 # IC computation and history
 ├── synth.py              # Factor synthesis (equal-weight / IC-weighted)
 ├── orchestrator.py       # Factor evaluation orchestrator
 ├── stats_cache.py        # Statistical cache layer
 ├── cards/                # Factor index cards (structured JSON per factor)
 └── README.md
 ```

 ## Factor lifecycle

 ```
 registered → candidate → active ⇄ monitoring → retired
                ↑            ↑          ↑              ↑
            (initial       (passes     (IC decay      (持续
             reg)          walk-fwd)   >30% vs 5d     ≥10天)
                                       rolling mean)
 ```

 State transitions are managed by `factor/registry.py` via `data/repos/factor_repo.py`.

**区分**：`using` (= active + monitoring) 是实盘交易的因子过滤器，两者均参与每日 08:30 信号生成。`active` = 认证通过；`monitoring` = 观察期（IC 衰减但仍可用）。15:30 attribution 每日检测 IC 衰减：active→monitoring（衰减 >30%），monitoring→retired（持续 ≥10 天衰减）。`retired` 因子不再参与任何交易。

 ## Adding a new factor

 1. Implement compute function in `compute/price/` or `compute/fundamental.py`
 2. Register in `_PRICE_FN_MAP` or `_FUNDAMENTAL_FN_MAP`
 3. Add parameter config in `config/config.yaml` under `factor.`
 4. Add factor card at `cards/{name}.json`
 5. Run `scripts/eval_standard.sh` for validation

 ## Key interfaces

 ```python
 # Compute factor values
 from factor.compute._registry import get_factor_names, load_active_price_factors
 factors = load_active_price_factors(status_filter="using")  # 实盘用 using (active + monitoring)

 # Evaluate
 from factor.ic import rank_ic, ic_summary
 ic = rank_ic(factor_values, forward_returns)

 # Synthesize
 from factor.synth import ic_weighted
 alpha = ic_weighted(factor_dict, ic_history)
 ```

 ## Related docs

 - [Evaluation pipeline](../docs/factors/evaluation-pipeline.md)
 - [ADR 007: Factor evaluation standard](../docs/adr/007-factor-evaluation-standard.md)
 - [ADR 026: Standard evaluation workflow](../docs/adr/026-standard-evaluation-workflow.md)
