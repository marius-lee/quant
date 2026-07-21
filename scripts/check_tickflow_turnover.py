"""诊断 tickflow 行情接口是否返回 turnover_rate 字段"""
from tickflow import TickFlow
from quant.config.constants import _require_cfg

tf = TickFlow(api_key=_require_cfg("data.tickflow_api_key"))
q = tf.quotes.get(symbols=['600519.SH'])
if q:
    print("字段列表:", list(q[0].keys()))
    print("turnover相关:", {k: v for k, v in q[0].items() if 'turnover' in k.lower() or 'volume' in k.lower()})
else:
    print("EMPTY")
