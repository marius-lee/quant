"""配置加载器 — 从 config.yaml 读取，导出为命名空间"""
import os
import yaml

_config = None


def load(path: str = None) -> dict:
    """加载配置（单例），返回完整配置 dict"""
    global _config
    if _config is not None:
        return _config
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(path) as f:
        _config = yaml.safe_load(f)
    return _config


def get(key: str, default=None):
    """点号路径取值: get('backtest.commission') → 0.0003"""
    cfg = load()
    for part in key.split("."):
        if isinstance(cfg, dict):
            cfg = cfg.get(part)
        else:
            return default
    return cfg if cfg is not None else default
