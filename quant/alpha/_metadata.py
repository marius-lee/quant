class ModelMetadata:
    """模型元数据 — 记录训练信息，用于版本追踪和审计。"""
    train_date: str
    n_samples: int
    n_features: int
    feature_names: list[str]
    ic_mean: float         # 训练集截面 IC 均值
    ic_std: float          # 训练集截面 IC 标准差
    model_hash: str         # 模型文件 SHA256
    lgb_params: dict = field(default_factory=dict)
    # v423: OOS 验证指标 (业界标准 ICIR = IC_mean / IC_std)
    oos_ic_mean: float = 0.0      # OOS 逐日截面 IC 均值
    oos_ic_std: float = 0.0       # OOS 逐日截面 IC 标准差
    oos_icir: float = 0.0         # OOS ICIR (显著性: >0.3 可用, >1 强)
    oos_n_days: int = 0           # OOS 有效日数
    train_start: str = ""     # v423: 训练窗口起 (旧模型 JSON 无此字段 → 默认)
    train_end: str = ""       # v423: 训练窗口止


# ═══════════════════════════════════════════════════════════
# LightGBM 模型封装
# ═══════════════════════════════════════════════════════════

