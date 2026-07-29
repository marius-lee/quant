"""测试 akshare 数据源速度 + 可用性."""
import akshare as ak, time

# 测试1: 单只速度
syms = [
    ('600519', '茅台'), ('000001', '平安银行'), ('002452', '长高电新'),
    ('601318', '中国平安'), ('300750', '宁德时代'),
]
print(f"{'代码':8s} {'名称':8s} {'耗时':8s} {'行数':6s} {'状态'}")
for sym, name in syms:
    t0 = time.time()
    try:
        df = ak.stock_zh_a_hist(symbol=sym, period='daily',
                                start_date='20260728', end_date='20260729',
                                adjust='qfq')
        t = time.time() - t0
        print(f'{sym:8s} {name:8s} {t:6.1f}s  {len(df):4d}行 ✅')
    except Exception as e:
        t = time.time() - t0
        err = str(e)[:40]
        print(f'{sym:8s} {name:8s} {t:6.1f}s  {0:4d}行 ❌ {err}')

# 测试2: 批量预估
print(f'\n单只平均耗时, 全量5481股预估: {5481 * 0.5 / 60:.0f}分钟 (假设0.5s/只)')
