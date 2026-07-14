 """Generate factor index cards from factor_registry + IC history.

 Reads factor_registry table for status and factor/compute maps for metadata,
 then writes structured JSON cards to factor/cards/.

 Usage:
    PYTHONPATH=. python3 scripts/generate_factor_cards.py
"""
 import json
 import os
 from pathlib import Path

 from data.repos import FactorRepo
 from factor.compute.price import _PRICE_FN_MAP
 from factor.compute.fundamental import _FUNDAMENTAL_FN_MAP


 ALL_FACTORS = {}
 for name, (fn, win) in _PRICE_FN_MAP.items():
     ALL_FACTORS[name] = {"type": "price", "window": win, "category": "dynamic"}
 for name, (cat, fn) in _FUNDAMENTAL_FN_MAP.items():
     ALL_FACTORS[name] = {"type": "fundamental", "window": 0, "category": cat}


 def main():
    repo = FactorRepo()
    db_factors = {f["name"]: f for f in repo.get_all_factors()}

    cards_dir = Path(__file__).parent.parent / "factor" / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    for name in sorted(ALL_FACTORS):
        meta = ALL_FACTORS[name]
        db = db_factors.get(name, {})

        card = {
            "name": name,
            "display_name": name,
            "category": db.get("category", meta["category"]),
            "sub_category": meta["type"],
            "formula": "",
            "window_days": meta["window"],
            "data_deps": [],
            "reference": "",
            "hypothesis": "",
            "status": db.get("status", "registered"),
            "status_history": [],
            "ic_mean_12m": db.get("ic_mean"),
            "ic_std_12m": None,
            "icir_12m": db.get("ic_ir"),
            "half_life_days": None,
            "decay_trend": "unknown",
            "correlations": {},
            "last_evaluated": db.get("last_evaluated"),
        }

        card_path = cards_dir / f"{name}.json"
        # Do not overwrite hand-curated cards
        if not card_path.exists():
            card_path.write_text(
                json.dumps(card, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            print(f"Created: {card_path}")

    print(f"Done. {len(ALL_FACTORS)} factors processed.")

 if __name__ == "__main__":
    main()
