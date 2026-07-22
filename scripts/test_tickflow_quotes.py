from tickflow import TickFlow
from quant.config.constants import _require_cfg

api_key = _require_cfg('data.tickflow_api_key')
tf = TickFlow(api_key=api_key)

quotes = tf.quotes.get(symbols=["600000.SH", "600009.SH","600007.SH","600004.SH","600006.SH", "000001.SZ"])
for q in quotes:
    print(f"{q['symbol']}: {q['last_price']}")
