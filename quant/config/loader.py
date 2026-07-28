"""配置加载器 — 从 config.yaml 读取，导出为命名空间。支持 ${ENV_VAR} 环境变量替换。

支持热更新: 每次 get() 检查 config.yaml 修改时间，文件变更后自动重新加载。
用 getmtime 系统调用（~1μs），零性能影响。

进程内临时覆盖: override() 上下文管理器 (hyperopt/测试专用, test-v298)。
config.yaml 仍是唯一真相源 — override 只改进程内单例, 不写文件,
退出上下文即恢复。

凭证管理: import 时自动加载 config/.env → os.environ。
  config.yaml 中的 ${TUSHARE_TOKEN} 等占位符将从 os.environ 取值。
  config/.env 格式: KEY=VALUE, 一行一个, # 注释。
  config/.env 已在 .gitignore 中, 不会提交。
"""
import copy
import os
import re
import yaml
from contextlib import contextmanager

# ── Auto-load config/.env into os.environ ──
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
if os.path.isfile(_ENV_PATH):
    with open(_ENV_PATH) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if not _line or _line.startswith('#') or '=' not in _line:
                continue
            _key, _, _val = _line.partition('=')
            _key = _key.strip()
            _val = _val.strip().strip('"').strip("'")
            if _key and _key not in os.environ:
                os.environ[_key] = _val

_config = None
_config_mtime = 0
_config_path = None
_ENV_RE = re.compile(r'^\$\{(\w+)\}$')


def _find_path(path: str = None) -> str:
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "config.yaml")
    return path


def load(path: str = None) -> dict:
    """加载配置（支持热更新），返回完整配置 dict"""
    global _config, _config_mtime, _config_path
    cfg_path = _find_path(path)
    _config_path = cfg_path

    try:
        mtime = os.path.getmtime(cfg_path)
    except OSError:
        if _config is not None:
            return _config  # 文件不存在时用缓存
        raise

    if _config is not None and mtime <= _config_mtime:
        return _config  # 未修改，用缓存

    with open(cfg_path) as f:
        _config = yaml.safe_load(f)
    _config_mtime = mtime
    return _config


def validate() -> None:
    """启动时校验所有关键配置项的类型。不合规立即 raise TypeError。"""
    cfg = load()
    _check(cfg, 'web.port', int)
    _check_range(cfg, 'web.sse.queue_timeout', (int, float), min_val=1)
    _check(cfg, 'quant.scheduler.poll_interval', (int, float))
    _check(cfg, 'factor.evaluation.max_workers', int)
    _check(cfg, 'factor.evaluation.worker_timeout_sec', (int, float))
    _check(cfg, 'data.sqlite.timeout', (int, float))
    _check(cfg, 'data.sqlite.busy_timeout', int)
    _check(cfg, 'data.http_timeout.sina', (int, float))
    _check(cfg, 'data.http_timeout.tushare', (int, float))
    _check(cfg, 'data.http_timeout.tencent', (int, float))
    _check(cfg, 'data.http_timeout.sse', (int, float))
    _check(cfg, 'data.batch_size', int)
    _check(cfg, 'data.lookback_days', int)
    _check(cfg, 'data.stale_days', int)
    _check(cfg, 'data.fetch.max_lookback_days', int)
    _check(cfg, 'sync.daily_interval', (int, float))
    _check(cfg, 'execution.commission', (int, float))
    _check_range(cfg, 'execution.slippage', (int, float), min_val=0)
    _check(cfg, 'execution.stamp_tax', (int, float))
    _check(cfg, 'execution.impact_eta', (int, float))
    _check(cfg, 'execution.default_daily_vol', (int, float))
    _check(cfg, 'execution.min_commission', (int, float))
    _check(cfg, 'execution.quote.max_batch_workers', int)
    _check_range(cfg, 'risk.covariance.window', int, min_val=20)
    _check(cfg, 'risk.covariance.min_periods', int)
    _check(cfg, 'risk.max_positions', int)
    _check(cfg, 'risk.max_single_position', (int, float))
    _check(cfg, 'risk.max_sector_exposure', (int, float))
    _check(cfg, 'risk.min_price', (int, float))
    _check(cfg, 'risk.min_daily_amount', int)
    _check(cfg, 'risk.stop_loss_pct', (int, float))
    _check(cfg, 'monitor.alert.drawdown_critical', (int, float))
    _check(cfg, 'monitor.alert.drawdown_warning', (int, float))
    _check(cfg, 'alpha.weekly_weight', (int, float))
    _check(cfg, 'alpha.sector_rotation', bool)
    _check(cfg, 'optimizer.rebalance_freq', str)
    _check(cfg, 'optimizer.rebalance_weekday', int)
    _check(cfg, 'optimizer.kelly_fraction', (int, float))
    _check(cfg, 'recon.cash_drift_tolerance', (int, float))
    _check(cfg, 'factor.compute.zscore_min_count_dense', int)
    _check(cfg, 'factor.compute.zscore_min_count_sparse', int)
    _check_range(cfg, 'factor.stats.ic_min_periods', int, min_val=10)
    _check_range(cfg, 'factor.stats.min_valid_days', int, min_val=5)
    _check_range(cfg, 'factor.evaluation.min_abs_ic', (int, float), min_val=0, max_val=1)
    _check(cfg, 'factor.evaluation.t_threshold', (int, float))
    _check_range(cfg, 'factor.evaluation.min_icir', (int, float), min_val=0, max_val=5.0)

    _check(cfg, 'universe.exclude_st', bool)
    _check_range(cfg, 'universe.exclude_new_stock_days', int, min_val=0)
    _check_range(cfg, 'universe.min_price', (int, float), min_val=0)
    _check_range(cfg, 'universe.min_daily_amount', int, min_val=0)
    _check_range(cfg, 'universe.exclude_zero_turnover_days', int, min_val=0)
    _check_range(cfg, 'universe.sealed_limit_up_ratio', (int, float), min_val=0.01)


def _check(cfg: dict, key: str, expected: type | tuple[type, ...]) -> None:
    parts = key.split('.')
    val = cfg
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            val = None
            break
    if val is None:
        raise KeyError(f'config.yaml missing required key: {key} (or value is null)')
    if not isinstance(val, expected):
        exp_names = (
            ' | '.join(t.__name__ for t in expected)
            if isinstance(expected, tuple)
            else expected.__name__
        )
        raise TypeError(
            f'config.yaml [{key}] type error: expected {exp_names}, '
            f'got {type(val).__name__} (value={val!r})'
        )



def reload() -> dict:
    """强制重读配置文件，清除缓存。策略切换等场景使用。"""
    global _config, _config_mtime
    _config = None
    _config_mtime = 0
    return load()


def _set_nested(cfg: dict, key: str, value) -> None:
    """按点号路径写入嵌套 dict。key 路径必须已存在 (fail-fast 防笔误),
    禁止通过 override 新增配置项 — 新参数必须先入 config.yaml。
    """
    parts = key.split(".")
    node = cfg
    for p in parts[:-1]:
        if not isinstance(node, dict) or p not in node or not isinstance(node[p], dict):
            raise KeyError(f"config override: 路径 '{key}' 不存在 (中间键 '{p}')")
        node = node[p]
    leaf = parts[-1]
    if leaf not in node:
        raise KeyError(f"config override: key '{key}' 不存在, 禁止新增配置项")
    node[leaf] = value


@contextmanager
def override(mapping: dict):
    """进程内临时覆盖配置 (hyperopt / 测试专用, test-v298)。

    深拷贝当前单例 → 就地写入 mapping → yield; 退出时恢复原单例。
    覆盖期间将 mtime 钉为 inf, 防止热重载把 override 冲掉。
    config.yaml 文件本身不被修改。

    Args:
        mapping: {"alpha.top_fraction": 0.3, ...} 点号路径 → 新值。
                 所有 key 必须已存在于 config.yaml, 否则 KeyError。

    Example:
        with loader.override({"optimizer.rebalance_freq": "daily"}):
            run_backtest(...)
    """
    global _config, _config_mtime
    load()  # 确保单例已加载
    snapshot = _config
    snapshot_mtime = _config_mtime
    _config = copy.deepcopy(_config)
    for key, value in mapping.items():
        _set_nested(_config, key, value)
    _config_mtime = float("inf")
    try:
        yield _config
    finally:
        _config = snapshot
        _config_mtime = snapshot_mtime


def get(key: str, default=None):
    """点号路径取值: get('backtest.commission') → 0.0003
    支持 ${ENV_VAR} 环境变量替换。
    自动检测配置文件变更并热更新。
    """
    cfg = load()
    for part in key.split("."):
        if isinstance(cfg, dict):
            cfg = cfg.get(part)
        else:
            return default
    if cfg is not None and isinstance(cfg, str):
        m = _ENV_RE.match(cfg)
        if m:
            return os.environ.get(m.group(1), default)
    return cfg if cfg is not None else default
def _get_nested(cfg: dict, key: str):
    """Get nested dict value by dot-separated key."""
    parts = key.split('.')
    val = cfg
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            return None
    return val


def _check_range(cfg: dict, key: str, expected: type | tuple[type, ...],
                 min_val=None, max_val=None) -> None:
    """Validate config key type AND numeric range. Raise if out of bounds."""
    _check(cfg, key, expected)
    val = _get_nested(cfg, key)
    if val is None:
        return  # already caught by _check
    if min_val is not None and val < min_val:
        raise ValueError(
            f'config.yaml [{key}]={val} < min={min_val}')
    if max_val is not None and val > max_val:
        raise ValueError(
            f'config.yaml [{key}]={val} > max={max_val}')



# ── Module-level auto-validation: fires on first import.
# Set QUANT_SKIP_CONFIG_VALIDATE=1 to bypass (e.g. CI without config.yaml).
_VALIDATE_ON_IMPORT = os.environ.get("QUANT_SKIP_CONFIG_VALIDATE", "") != "1"
if _VALIDATE_ON_IMPORT:
    try:
        validate()
    except (KeyError, TypeError, ValueError) as _ve:
        import logging as _logging_config
        _logging_config.getLogger("quant.config").critical(
            "FATAL: config validation failed — %s", _ve)
        raise
