# v539 tests — data_hash 整库指纹误伤修复:
# 整库指纹随每日增量变化 (晚间链拉新数据 → daily COUNT/MAX(date) 变) →
# 全段缓存误判缺失 → 回测 IC 检查全缺 (2026-08-18 实证 239 天).
# 修复: _get_existing_factors 删除 data_hash 判定 (局部信任已物化日期),
# 历史回填防护由 force 全量物化承担 (v529 语义).
import json
import os
import tempfile

from quant.factor.store import FactorStore, _source_hash_single


def _make_fake_factor(store: FactorStore, name: str, dates: list[str],
                      source_hash: str | None = None) -> None:
    """构造伪因子目录 + meta (模拟物化产物)."""
    os.makedirs(os.path.join(store._parquet_dir, name), exist_ok=True)
    meta = {
        "source_hash": source_hash if source_hash is not None else _source_hash_single(name),
        "dates": dates,
        "first_date": dates[0],
        "last_date": dates[-1],
        "data_hash": "deadbeef00000000",  # 旧指纹 (与当前必然不等)
    }
    store._save_factor_meta(name, meta)


def _make_store(tmp: str) -> FactorStore:
    return FactorStore(cache_dir=tmp)


# ── 1. 源码断言: data_hash 判定已删除, source_hash 判定保留 ──
def test_data_hash_judgment_removed():
    src = open("quant/factor/store.py", encoding="utf-8").read()
    existing = src.split("def _get_existing_factors")[1].split("def is_materialized")[0]
    assert 'meta.get("data_hash")' not in existing
    assert "source_hash" in existing


# ── 2. 行为: 旧指纹不匹配但日期已物化 → 判定有效 (不再整段失效) ──
def test_existing_ignores_stale_data_hash(tmp_path):
    store = _make_store(str(tmp_path))
    _make_fake_factor(store, "fake_a", ["2026-06-01", "2026-06-02"])
    missing = store._date_missing_factors("2026-06-01", ["fake_a"])
    assert missing == []


# ── 3. 行为: source_hash 不匹配仍判缺失 (因子代码变更 → 重算) ──
def test_existing_still_checks_source_hash(tmp_path):
    store = _make_store(str(tmp_path))
    _make_fake_factor(store, "fake_b", ["2026-06-01"], source_hash="stale_hash")
    missing = store._date_missing_factors("2026-06-01", ["fake_b"])
    assert missing == ["fake_b"]


# ── 4. 行为: 未物化日期仍判缺失 ──
def test_existing_missing_date(tmp_path):
    store = _make_store(str(tmp_path))
    _make_fake_factor(store, "fake_c", ["2026-06-01"])
    missing = store._date_missing_factors("2026-06-02", ["fake_c"])
    assert missing == ["fake_c"]