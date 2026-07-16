"""Factor compute package — backward-compatible with old factor.compute module."""

from quant.factor.compute._shared import _market_db_path  # noqa: F401
from quant.factor.registry import _cs_zscore, _FIN_FACTORS, _db_connect, _shared_limit_conn  # re-exported for bw compat

from quant.factor.compute.price import _PRICE_FN_MAP  # noqa: F401
from quant.factor.compute.fundamental import _FUNDAMENTAL_FN_MAP  # noqa: F401

# Also re-export all public symbols from sub-modules
from quant.factor.compute.price import *  # noqa: F401, F403
from quant.factor.compute.fundamental import *  # noqa: F401, F403
from quant.factor.compute._registry import (  # noqa: F401
    load_active_price_factors,
    load_active_fundamental_factors,
    update_factor_evaluation,
    get_factor_names,
)
from quant.factor.compute._dispatch import compute_all_factors  # noqa: F401
from quant.factor.compute._primitives import precompute_primitives  # noqa: F401
