"""配置加载器 — 从 config.yaml 读取，导出为命名空间。支持 ${ENV_VAR} 环境变量替换。

支持热更新: 每次 get() 检查 config.yaml 修改时间，文件变更后自动重新加载。
用 getmtime 系统调用（~1μs），零性能影响。
"""
import os
import re
import yaml

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
