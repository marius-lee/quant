#!/usr/bin/env python3
"""Replicate state_broker signal loading exactly."""
import os, sys, sqlite3, json
from datetime import datetime

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sig_path = os.path.join(_root, "quant", "data", "trades.db")
today = datetime.now().strftime("%Y-%m-%d")

sc = sqlite3.connect(sig_path)
sc.row_factory = sqlite3.Row

try:
    sig_row = sc.execute(
        "SELECT signals_json FROM daily_signals WHERE date=? AND mode='live' "
        "ORDER BY generated_at DESC LIMIT 1",
        (today,)
    ).fetchone()

    if sig_row and sig_row["signals_json"]:
        signals = json.loads(sig_row["signals_json"])
        print(f"STEP1 OK: {len(signals)} signals parsed")

        # exec_notes
        exec_notes_str = sig_row.get("exec_notes") if hasattr(sig_row, "get") else None
        print(f"STEP2: hasattr .get = {hasattr(sig_row, 'get')}, exec_notes_str = {exec_notes_str}")

        if not exec_notes_str:
            en_row = sc.execute(
                "SELECT exec_notes FROM daily_signals WHERE date=? AND mode='live' ORDER BY generated_at DESC LIMIT 1",
                (today,)
            ).fetchone()
            exec_notes_str = en_row["exec_notes"] if en_row else "{}"
            print(f"STEP3: fallback exec_notes_str = {exec_notes_str}")

        exec_notes = json.loads(exec_notes_str) if exec_notes_str else {}
        print(f"STEP4: exec_notes = {exec_notes}")

        for s in signals:
            s["exec_note"] = exec_notes.get(s.get("symbol", ""), "")
        print(f"STEP5: signals with exec_note: {len(signals)}")

        # name lookup
        mdb = sqlite3.connect(os.path.join(_root, "quant", "data", "market.db"))
        symbols = [s["symbol"] for s in signals if "symbol" in s]
        placeholders = ",".join(["?"] * len(symbols))
        name_map = dict(mdb.execute(
            f"SELECT symbol, name FROM stocks WHERE symbol IN ({placeholders})",
            symbols
        ).fetchall())
        print(f"STEP6: names loaded: {len(name_map)}")
        for s in signals:
            s["name"] = name_map.get(s.get("symbol", ""), "")
        mdb.close()

        print(f"STEP7 DONE: signals = {json.dumps(signals[:2], ensure_ascii=False, indent=2)}")
    else:
        print(f"STEP1: sig_row is None or signals_json empty: sig_row={sig_row is not None}")

    sc.close()
except Exception as e:
    print(f"EXCEPTION: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
