"""Add cancel_reason to pending_orders + exec_notes to daily_signals + update methods."""
import re

# ──────────── trade_repo.py ────────────
tr_path = '/Users/mariusto/project/quant/quant/data/trade_repo.py'
tr = open(tr_path).read()

# 1. Add column migrations (after the existing ALTER TABLE block)
old_migrations = """        # ── 迁移: daily_signals 兼容旧 schema (无 mode 列) ──
        try:
            c.execute("ALTER TABLE daily_signals ADD COLUMN mode TEXT DEFAULT 'live'")
        except sqlite3.OperationalError:
            pass  # column already exists"""

new_migrations = """        # ── 迁移: daily_signals 兼容旧 schema (无 mode 列) ──
        try:
            c.execute("ALTER TABLE daily_signals ADD COLUMN mode TEXT DEFAULT 'live'")
        except sqlite3.OperationalError:
            pass  # column already exists
        # ── 迁移: pending_orders.cancel_reason (test-v210) ──
        try:
            c.execute("ALTER TABLE pending_orders ADD COLUMN cancel_reason TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # ── 迁移: daily_signals.exec_notes (test-v210) ──
        # JSON dict: {"001258": "abandoned_sealed", "600744": "filled"}
        try:
            c.execute("ALTER TABLE daily_signals ADD COLUMN exec_notes TEXT DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass"""

assert old_migrations in tr, "Migrations block not found"
tr = tr.replace(old_migrations, new_migrations)

# 2. Add update_signal_exec_note method (after get_latest_signals)
old_method_end = """        if row:
            return {"date": row[0], "targets": _json.loads(row[1]), "capital": row[2]}
        return None"""

new_method_end = """        if row:
            return {"date": row[0], "targets": _json.loads(row[1]), "capital": row[2]}
        return None

    def get_signal_exec_notes(self, date_str: str) -> dict:
        """返回某日信号的执行备注 {symbol: note, ...}.

        来源: monitor 订单管理阶段回写 (封死涨停/资金不足/追价放弃)。
        """
        import json as _json
        c = self._conn()
        row = c.execute(
            "SELECT exec_notes FROM daily_signals WHERE date=? ORDER BY generated_at DESC LIMIT 1",
            (date_str,)
        ).fetchone()
        c.close()
        if row and row[0]:
            try:
                return _json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def update_signal_exec_note(self, date_str: str, symbol: str, note: str):
        """追加单个标的的执行备注到 daily_signals.exec_notes JSON."""
        import json as _json
        c = self._conn()
        row = c.execute(
            "SELECT exec_notes FROM daily_signals WHERE date=? ORDER BY generated_at DESC LIMIT 1",
            (date_str,)
        ).fetchone()
        if not row:
            c.close()
            return
        try:
            notes = _json.loads(row[0]) if row[0] else {}
        except (json.JSONDecodeError, TypeError):
            notes = {}
        notes[symbol] = note
        c.execute(
            "UPDATE daily_signals SET exec_notes=? WHERE date=?",
            (_json.dumps(notes, ensure_ascii=False), date_str)
        )
        c.commit()
        c.close()"""

assert old_method_end in tr, "get_latest_signals end not found"
tr = tr.replace(old_method_end, new_method_end)

open(tr_path, 'w').write(tr)
import ast; ast.parse(tr)
print("trade_repo.py: OK")

# ──────────── order_manager.py ────────────
om_path = '/Users/mariusto/project/quant/quant/scheduler/order_manager.py'
om = open(om_path).read()

# 3. Update _cancel to write cancel_reason to DB
old_cancel = '''    def _cancel(self, order_id: int, reason: str = ""):
        c = _conn()
        c.execute(
            "UPDATE pending_orders SET status='cancelled' WHERE id=?",
            (order_id,))
        c.commit()
        c.close()
        if reason:
            _log.info(f"[order_manager] cancelled order#{order_id}: {reason}")'''

new_cancel = '''    def _cancel(self, order_id: int, reason: str = ""):
        """取消订单 → 写入 cancel_reason 到 DB (test-v210)."""
        c = _conn()
        c.execute(
            "UPDATE pending_orders SET status='cancelled', cancel_reason=? WHERE id=?",
            (reason, order_id))
        c.commit()
        c.close()
        if reason:
            _log.info(f"[order_manager] cancelled order#{order_id}: {reason}")'''

assert old_cancel in om, "_cancel not found"
om = om.replace(old_cancel, new_cancel)

# 4. After sealed_limit_up abandon, update exec_notes
old_sealed = '''                        self._cancel(po.id, "sealed_limit_up")
                        actions.append({"symbol": po.symbol, "action": "abandon",
                                        "reason": "封死涨停(ask_volume=0), 无法买入"})
                        _log.info(f"[order_manager] ABANDON {po.symbol}: 封死涨停 "
                                  f"(ask={ask:.2f} ask_vol=0), 放弃买入")
                        continue'''

new_sealed = '''                        self._cancel(po.id, "sealed_limit_up")
                        actions.append({"symbol": po.symbol, "action": "abandon",
                                        "reason": "封死涨停(ask_volume=0), 无法买入"})
                        _log.info(f"[order_manager] ABANDON {po.symbol}: 封死涨停 "
                                  f"(ask={ask:.2f} ask_vol=0), 放弃买入")
                        # 回写信号执行状态 (test-v210)
                        self._note_signal(day, po.symbol, "abandoned_sealed")
                        continue'''

assert old_sealed in om, "sealed block not found"
om = om.replace(old_sealed, new_sealed)

# 5. Add _note_signal helper method (after _cancel)
old_cancel_all = '    def cancel_all(self, day: str, strategy: str = "quant"):'
new_note_method = '''    def _note_signal(self, day: str, symbol: str, note: str):
        """回写信号执行备注到 daily_signals.exec_notes (test-v210)."""
        try:
            from quant.data.trade_repo import TradeRepo
            TradeRepo().update_signal_exec_note(day, symbol, note)
        except Exception as e:
            _log.warning(f"[order_manager] exec_note write failed (non-fatal): {e}")

    def cancel_all(self, day: str, strategy: str = "quant"):'''

assert old_cancel_all in om, "cancel_all not found"
om = om.replace(old_cancel_all, new_note_method)

open(om_path, 'w').write(om)
import ast; ast.parse(om)
print("order_manager.py: OK")
