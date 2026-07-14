"""Smoke tests: verify factor registry integrity after P69 refactoring.

Covers:
- All 65 factor functions can be imported without NameError
- _PRICE_FN_MAP and _FUNDAMENTAL_FN_MAP have no duplicate keys
- No function is defined more than once
"""

import pytest
import re


def test_all_maps_resolvable():
    """Verify all factor function references in both maps are importable."""
    from quant.factor.compute import _PRICE_FN_MAP, _FUNDAMENTAL_FN_MAP

    for name, (fn_obj, window) in _PRICE_FN_MAP.items():
        assert callable(fn_obj), f"_PRICE_FN_MAP['{name}'] fn not callable: {fn_obj}"

    for name, (cat, fn_obj) in _FUNDAMENTAL_FN_MAP.items():
        assert callable(fn_obj), f"_FUNDAMENTAL_FN_MAP['{name}'] fn not callable: {fn_obj}"


def test_no_duplicate_keys_in_maps():
    """Verify no key appears in both maps."""
    from quant.factor.compute import _PRICE_FN_MAP, _FUNDAMENTAL_FN_MAP

    overlap = set(_PRICE_FN_MAP) & set(_FUNDAMENTAL_FN_MAP)
    if overlap:
        # margin_buy_ratio is intentionally in both maps (different function signatures)
        allowed = {"margin_buy_ratio"}
        unexpected = overlap - allowed
        assert not unexpected, f"Unexpected overlap between maps: {unexpected}"


def test_no_duplicate_function_definitions():
    """Verify no compute_ function is defined more than once in compute.py."""
    import quant.factor.compute as fc as fc

    with open(fc.__file__) as f:
        counts = {}
        for line in f:
            m = re.match(r'^def (compute_\w+)', line)
            if m:
                name = m.group(1)
                counts[name] = counts.get(name, 0) + 1

    dups = {k: v for k, v in counts.items() if v > 1}
    assert not dups, f"Duplicate function definitions: {dups}"


def test_no_duplicate_helper_definitions():
    """Verify no helper function is defined more than once."""
    import quant.factor.compute as fc as fc

    with open(fc.__file__) as f:
        counts = {}
        for line in f:
            m = re.match(r'^def (_\w+)', line)
            if m:
                name = m.group(1)
                counts[name] = counts.get(name, 0) + 1

    dups = {k: v for k, v in counts.items() if v > 1}
    assert not dups, f"Duplicate helper definitions: {dups}"
