"""数据源统一重试包装器 — 指数退避。

适用: 所有外部 HTTP API 数据源调用 (Tushare/TickFlow/AKShare 等)

来源: GitHub akfamily/akshare#5762 (2025.3) + 社区实测:
  - 东方财富底层约3次/秒触发封禁, 连续4次不同函数即可触发rate limit
  - 指数退避 1s→2s→4s→8s 是社区最广泛使用的方案
  - 随机抖动 (jitter) 强烈推荐, 模拟人工操作避免被识别为爬虫

用法:
  from quant.data.datasource_retry import datasource_retry, quote_retry

  # 批量数据拉取 (Tushare, TickFlow, AKShare):
  @datasource_retry
  def fetch_batch():
      return api.daily(ts_code=..., ...)

  # 实时行情拉取 (Tencent, Sina):
  @quote_retry
  def fetch_quote():
      return urllib.request.urlopen(req)

  # 自定义参数:
  @datasource_retry(tries=5, delay=2)
  def slow_fetch(): ...
"""

from retry import retry


def datasource_retry(func=None, *, tries=4, delay=1, backoff=2):
    """批量数据源指数退避重试装饰器。

    延迟序列 (默认): 1s → 2s → 4s → 8s (共4次尝试, 总计15s)

    适用: 批量日线/基本面拉取 (Tushare/TickFlow/AKShare)
         特点: 低频调用, 失败代价高, 可接受较长等待

    Args:
        tries: 最大尝试次数 (默认4)
        delay: 初始延迟秒数 (默认1)
        backoff: 延迟倍增因子 (默认2 → 指数退避)

    Returns:
        装饰后的函数, 在Exception时自动按指数退避重试

    Reference URLs:
      - https://github.com/akfamily/akshare/issues/5762
      - https://blog.gitcode.com/866b91c47d325e42e9ec7f28fbd68c33.html
    """
    if func is not None:
        return retry(Exception, tries=tries, delay=delay, backoff=backoff)(func)
    return lambda f: retry(Exception, tries=tries, delay=delay, backoff=backoff)(f)


def quote_retry(func=None, *, tries=3, delay=0.5, backoff=2):
    """实时行情轻量重试装饰器。

    延迟序列 (默认): 0.5s → 1s → 2s (共3次尝试, 总计3.5s)

    适用: 实时行情拉取 (Tencent/Sina HTTP)
         特点: 高频调用, 对延迟敏感, 需要快速失败或成功

    Args:
        tries: 最大尝试次数 (默认3)
        delay: 初始延迟秒数 (默认0.5)
        backoff: 延迟倍增因子 (默认2 → 指数退避)

    Returns:
        装饰后的函数, 在Exception时自动按指数退避重试

    Reference URLs:
      - Tencent/Sina 行情接口在 A 股交易时段每3秒调用一次
      - 3次重试总共3.5秒, 不会错过下一轮行情轮询
      - blog.gitcode.com: HTTP行情接口瞬时失败率高, 轻量重试收益大
    """
    if func is not None:
        return retry(Exception, tries=tries, delay=delay, backoff=backoff)(func)
    return lambda f: retry(Exception, tries=tries, delay=delay, backoff=backoff)(f)
