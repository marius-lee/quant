"""配置加载器 — 从 config.yaml 读取，导出为命名空间。支持 ${ENV_VAR} 环境变量替换。

支持热更新: 每次 get() 检查 config.yaml 修改时间，文件变更后自动重新加载。
用 getmtime 系统调用（~1μs），零性能影响。

凭证管理: import 时自动加载 config/.env → os.environ。
  config.yaml 中的 ${TUSHARE_TOKEN} 等占位符将从 os.environ 取值。
  config/.env 格式: KEY=VALUE, 一行一个, # 注释。
  config/.env 已在 .gitignore 中, 不会提交。
"""
import os
import re
import yaml

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
    _check(cfg, 'web.sse.queue_timeout', (int, float))
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
    _check(cfg, 'execution.slippage', (int, float))
    _check(cfg, 'execution.stamp_tax', (int, float))
    _check(cfg, 'execution.impact_eta', (int, float))
    _check(cfg, 'execution.default_daily_vol', (int, float))
    _check(cfg, 'execution.min_commission', (int, float))
    _check(cfg, 'execution.quote.max_batch_workers', int)
    _check(cfg, 'risk.covariance.window', int)
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
    _check(cfg, 'optimizer.min_holding_days', int)
    _check(cfg, 'optimizer.kelly_fraction', (int, float))
    _check(cfg, 'factor.compute.zscore_min_count_dense', int)
    _check(cfg, 'factor.compute.zscore_min_count_sparse', int)
    _check(cfg, 'factor.stats.ic_min_periods', int)
    _check(cfg, 'factor.stats.min_valid_days', int)
    _check(cfg, 'factor.evaluation.min_abs_ic', (int, float))
    _check(cfg, 'factor.evaluation.t_threshold', (int, float))
    _check(cfg, 'factor.evaluation.min_icir', (int, float))


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
        raise KeyError(f'config.yaml missing required key: {key}')
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
