"""兼容层 → web/state_broker (deprecated). 保留用于旧 import 路径."""
from web.state_broker import broker as _broker

get_state = _broker.get
update_state = _broker.update
