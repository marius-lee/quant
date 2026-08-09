-- P1-18fix: stocks 表添加 total_shares 列 (通过 ALTER TABLE 在 store.py ensure_tables 幂等创建)
ALTER TABLE stocks ADD COLUMN total_shares REAL;
