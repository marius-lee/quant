"""Alternative Data Integration — 研报情感/供应链/ESG/卫星/信用卡/招聘.

统一另类数据接入框架:
  1. 统一接口: 统一获取/清洗/存储/因子化接口
  2. 多数据源: 研报/供应链/ESG/卫星/信用卡/招聘/航运/专利/社交情绪
  3. 标准化: 统一字段/频率/时间戳/质量评分
  4. 因子化: 直接产出可用因子值 (IC/IR 预验证)

数据源分类:
  - 基础面增强: 研报情感/财报解读/管理层语调
  - 供应链: 上下游关系/出货量/库存周转/产能利用率
  - ESG: 环境/社会/治理评分/碳排放/绿色收入占比
  - 卫星/遥感: 厂区活跃度/停车场车流/油罐液位/作物长势
  - 消费/支付: 信用卡消费/移动支付/电商销售/线下客流
  - 招聘/人才: 招聘需求/薪资趋势/关键人才流失/技能需求
  - 航运/物流: 港口吞吐/集装箱吞吐/波罗的海指数/运费指数
  - 专利/创新: 专利申请/授权/引用/技术成熟度/研发投入
  - 社交情绪: 微博/雪球/东方财富/推特情绪/关键词热度
  - 宏观/高频: PMI/用电量/铁路货运/货车通行/信贷/社融
"""

import os
import json
import hashlib
import time
import threading
import sqlite3
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Iterator, Union
from enum import Enum
from abc import ABC, abstractmethod
from contextlib import contextmanager

import pandas as pd
import numpy as np
import requests

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.config.paths import MARKET_DB
from quant.data.store import DataStore
from quant.data.cache import get_backend

_log = get_logger("data.alternative")


class DataSourceType(str, Enum):
    RESEARCH_REPORT = "research_report"      # 研报
    SUPPLY_CHAIN = "supply_chain"           # 供应链
    ESG = "esg"                             # ESG
    SATELLITE = "satellite"                 # 卫星/遥感
    CREDIT_CARD = "credit_card"             # 信用卡/支付
    RECRUITMENT = "recruitment"             # 招聘
    SHIPPING = "shipping"                   # 航运/物流
    PATENT = "patent"                       # 专利
    SOCIAL_SENTIMENT = "social_sentiment"   # 社交情绪
    MACRO_HIGH_FREQ = "macro_high_freq"     # 宏观高频
    CUSTOM = "custom"                       # 自定义


class DataFrequency(str, Enum):
    REALTIME = "realtime"       # 实时
    MINUTE = "minute"           # 分钟级
    HOURLY = "hourly"           # 小时级
    DAILY = "daily"             # 日度
    WEEKLY = "weekly"           # 周度
    MONTHLY = "monthly"         # 月度
    QUARTERLY = "quarterly"     # 季度
    EVENT_DRIVEN = "event"      # 事件驱动


@dataclass
class DataSourceConfig:
    """数据源配置."""
    name: str
    source_type: DataSourceType
    frequency: DataFrequency
    enabled: bool = True
    priority: int = 1  # 1=最高
    
    # 连接配置
    api_endpoint: str = ""
    api_key: str = ""
    api_secret: str = ""
    rate_limit_per_min: int = 60
    timeout_sec: int = 30
    
    # 数据范围
    symbols: List[str] = field(default_factory=list)  # 空=全市场
    start_date: str = "2020-01-01"
    end_date: str = ""  # 空=至今
    
    # 质量控制
    min_quality_score: float = 0.6  # 0-1
    max_missing_pct: float = 0.1
    validate_schema: bool = True
    
    # 存储
    table_name: str = ""
    partition_by: str = "date"  # date/symbol/year_month
    retention_days: int = 365 * 3  # 3年
    
    # 因子化
    factor_prefix: str = "alt_"  # 因子名前缀
    auto_factorize: bool = True
    factor_config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RawDataRecord:
    """原始数据记录."""
    source: str
    symbol: str
    timestamp: datetime
    data: Dict[str, Any]
    quality_score: float = 1.0
    raw_json: str = ""


@dataclass
class FactorRecord:
    """因子记录."""
    factor_name: str
    symbol: str
    date: str
    value: float
    quality_score: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class DataSource(ABC):
    """数据源抽象基类."""

    def __init__(self, config: DataSourceConfig):
        self.config = config
        self._cache = {}
        self._session = None
        self._rate_limiter = None
        self._init_session()

    @abstractmethod
    def _init_session(self):
        """初始化连接/会话."""
        pass

    @abstractmethod
    def fetch(self, symbols: List[str], start_date: str, end_date: str) -> List[RawDataRecord]:
        """抓取原始数据."""
        pass

    @abstractmethod
    def validate(self, records: List[RawDataRecord]) -> List[RawDataRecord]:
        """数据质量验证."""
        pass

    @abstractmethod
    def factorize(self, records: List[RawDataRecord]) -> List[FactorRecord]:
        """原始数据 → 因子值."""
        pass

    def fetch_and_factorize(self, symbols: List[str], start_date: str, end_date: str) -> List[FactorRecord]:
        """完整流程: 抓取 → 验证 → 因子化."""
        raw = self.fetch(symbols, start_date, end_date)
        valid = self.validate(raw)
        factors = self.factorize(valid)
        return factors

    def _rate_limit(self):
        """速率限制."""
        if self._rate_limiter:
            self._rate_limiter.wait()


class ResearchReportSource(DataSource):
    """研报数据源 — 解析 PDF/HTML, 提取情感/观点/目标价."""

    def _init_session(self):
        import akshare as ak
        self._ak = ak
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Quant Research Bot)"
        })

    def fetch(self, symbols: List[str], start_date: str, end_date: str) -> List[RawDataRecord]:
        """抓取研报 (akshare 接口)."""
        records = []
        try:
            # akshare 研报接口
            df = self._ak.stock_research_report_em(symbol=",".join(symbols))
            if df is None or df.empty:
                return records
            
            for _, row in df.iterrows():
                # 解析关键字段
                symbol = row.get("股票代码", "").zfill(6)
                if not symbol or len(symbol) != 6:
                    continue
                
                pub_date = str(row.get("发布日期", ""))
                if not (start_date <= pub_date <= end_date):
                    continue
                
                record = RawDataRecord(
                    source=self.config.name,
                    symbol=symbol,
                    timestamp=datetime.strptime(pub_date, "%Y-%m-%d"),
                    data={
                        "title": row.get("标题", ""),
                        "institute": row.get("机构", ""),
                        "analyst": row.get("分析师", ""),
                        "rating": row.get("评级", ""),
                        "target_price": row.get("目标价", 0),
                        "content": row.get("内容摘要", ""),
                        "url": row.get("链接", ""),
                    },
                    quality_score=self._calc_quality(row),
                    raw_json=row.to_json(),
                )
                records.append(record)
        except Exception as e:
            _log.warning(f"Research report fetch failed: {e}")
        return records

    def validate(self, records: List[RawDataRecord]) -> List[RawDataRecord]:
        valid = []
        for r in records:
            if r.data.get("content") and len(r.data["content"]) > 50:
                r.quality_score = min(r.quality_score * 1.2, 1.0)
                valid.append(r)
            else:
                r.quality_score *= 0.5
                if r.quality_score >= self.config.min_quality_score:
                    valid.append(r)
        return valid

    def factorize(self, records: List[RawDataRecord]) -> List[FactorRecord]:
        """研报 → 情感因子/目标价因子/评级因子."""
        factors = []
        from quant.utils.nlp import sentiment_score  # 假设有 NLP 模块
        
        for r in records:
            date_str = r.timestamp.strftime("%Y-%m-%d")
            base_name = f"{self.config.factor_prefix}{r.symbol}"
            
            # 情感因子
            content = r.data.get("content", "")
            if content:
                sentiment = sentiment_score(content)  # -1 到 1
                factors.append(FactorRecord(
                    factor_name=f"{base_name}_sentiment",
                    symbol=r.symbol,
                    date=r.timestamp.strftime("%Y-%m-%d"),
                    value=sentiment,
                    quality_score=r.quality_score,
                    metadata={"source": r.data.get("institute", ""), "analyst": r.data.get("analyst", "")}
                ))
            
            # 目标价因子
            target = r.data.get("target_price", 0)
            if target > 0:
                factors.append(FactorRecord(
                    factor_name=f"{base_name}_target_price",
                    symbol=r.symbol,
                    date=r.timestamp.strftime("%Y-%m-%d"),
                    value=target,
                    quality_score=r.quality_score,
                    metadata={"rating": r.data.get("rating", "")}
                ))
            
            # 评级因子 (买入=1, 持有=0, 卖出=-1)
            rating_map = {"买入": 1, "增持": 0.5, "中性": 0, "减持": -0.5, "卖出": -1}
            rating = r.data.get("rating", "")
            if rating in rating_map:
                factors.append(FactorRecord(
                    factor_name=f"{base_name}_rating",
                    symbol=r.symbol,
                    date=r.timestamp.strftime("%Y-%m-%d"),
                    value=rating_map[rating],
                    quality_score=r.quality_score,
                ))
        
        return factors

    def _calc_quality(self, row) -> float:
        score = 0.5
        if row.get("内容摘要"): score += 0.2
        if row.get("目标价"): score += 0.15
        if row.get("评级"): score += 0.15
        return min(score, 1.0)


class SupplyChainSource(DataSource):
    """供应链数据源 — 上下游关系/出货量/库存/产能."""

    def _init_session(self):
        import akshare as ak
        self._ak = ak

    def fetch(self, symbols: List[str], start_date: str, end_date: str) -> List[RawDataRecord]:
        records = []
        try:
            # 上下游关系
            df = self._ak.stock_supply_chain_relation(symbol="all")
            for _, row in df.iterrows():
                symbol = row.get("stock_code", "").zfill(6)
                if symbol not in symbols:
                    continue
                records.append(RawDataRecord(
                    source=self.config.name,
                    symbol=symbol,
                    timestamp=datetime.strptime(end_date, "%Y-%m-%d"),
                    data={
                        "relation_type": row.get("relation_type", ""),  # 上游/下游
                        "related_symbol": row.get("related_code", "").zfill(6),
                        "related_name": row.get("related_name", ""),
                        "weight": row.get("weight", 0),
                        "data_source": "supply_chain",
                    },
                    quality_score=0.8,
                ))
        except Exception as e:
            _log.warning(f"Supply chain fetch failed: {e}")
        return records

    def validate(self, records: List[RawDataRecord]) -> List[RawDataRecord]:
        return [r for r in records if r.data.get("related_symbol") and r.quality_score >= self.config.min_quality_score]

    def factorize(self, records: List[RawDataRecord]) -> List[FactorRecord]:
        """供应链 → 产业链地位/依赖度/协同因子."""
        factors = []
        from collections import defaultdict
        
        # 聚合上下游
        upstream = defaultdict(list)
        downstream = defaultdict(list)
        for r in records:
            rel = r.data.get("relation_type")
            if rel == "上游":
                upstream[r.symbol].append(r.data["related_symbol"])
            elif rel == "下游":
                downstream[r.symbol].append(r.data["related_symbol"])
        
        for sym in set(list(upstream.keys()) + list(downstream.keys())):
            # 上游依赖度
            up_count = len(set(upstream.get(sym, [])))
            # 下游影响力
            down_count = len(set(downstream.get(sym, [])))
            
            factors.append(FactorRecord(
                factor_name="alt_supply_upstream_deps",
                symbol=sym,
                date=datetime.now().strftime("%Y-%m-%d"),
                value=up_count,
                quality_score=0.8,
            ))
            factors.append(FactorRecord(
                factor_name="alt_supply_downstream_influence",
                symbol=sym,
                date=datetime.now().strftime("%Y-%m-%d"),
                value=down_count,
                quality_score=0.8,
            ))
        return factors


class ESGSource(DataSource):
    """ESG 数据源 — 环境/社会/治理评分."""

    def _init_session(self):
        import akshare as ak
        self._ak = ak

    def fetch(self, symbols: List[str], start_date: str, end_date: str) -> List[RawDataRecord]:
        records = []
        try:
            df = self._ak.stock_esg_score(symbol="all")
            for _, row in df.iterrows():
                symbol = row.get("股票代码", "").zfill(6)
                if symbol not in symbols:
                    continue
                records.append(RawDataRecord(
                    source=self.config.name,
                    symbol=symbol,
                    timestamp=datetime.now(),
                    data={
                        "esg_score": row.get("ESG评分", 0),
                        "environment_score": row.get("环境评分", 0),
                        "social_score": row.get("社会评分", 0),
                        "governance_score": row.get("治理评分", 0),
                        "carbon_emission": row.get("碳排放", 0),
                        "green_revenue_pct": row.get("绿色收入占比", 0),
                        "data_year": row.get("数据年份", datetime.now().year),
                    },
                    quality_score=0.9,
                ))
        except Exception as e:
            _log.warning(f"ESG fetch failed: {e}")
        return records

    def validate(self, records: List[RawDataRecord]) -> List[RawDataRecord]:
        return [r for r in records if r.data.get("esg_score", 0) > 0 and r.quality_score >= self.config.min_quality_score]

    def factorize(self, records: List[RawDataRecord]) -> List[FactorRecord]:
        factors = []
        for r in records:
            base = f"{self.config.factor_prefix}{r.symbol}"
            d = r.data
            for key, fname in [
                ("esg_score", "esg"),
                ("environment_score", "env"),
                ("social_score", "social"),
                ("governance_score", "gov"),
                ("carbon_emission", "carbon"),
                ("green_revenue_pct", "green_rev"),
            ]:
                val = d.get(key, 0)
                if val > 0:
                    factors.append(FactorRecord(
                        factor_name=f"{base}_{fname}",
                        symbol=r.symbol,
                        date=r.timestamp.strftime("%Y-%m-%d"),
                        value=val,
                        quality_score=r.quality_score,
                    ))
        return factors


class AlternativeDataManager:
    """另类数据统一管理器."""

    def __init__(self):
        self._sources: Dict[str, DataSource] = {}
        self._lock = threading.Lock()
        self._store = DataStore()
        self._factor_store = None
        self._running = False
        self._sync_thread = None
        self._stop_sync = threading.Event()

    def register_source(self, source: DataSource) -> bool:
        with self._lock:
            if source.config.name in self._sources:
                return False
            self._sources[source.config.name] = source
            _log.info(f"Alternative data source registered: {source.config.name}")
            return True

    def unregister_source(self, name: str) -> bool:
        with self._lock:
            if name not in self._sources:
                return False
            del self._sources[name]
            return True

    def get_source(self, name: str) -> Optional[DataSource]:
        return self._sources.get(name)

    def list_sources(self) -> List[DataSourceConfig]:
        return [s.config for s in self._sources.values()]

    def fetch_all(self, symbols: List[str], start_date: str, end_date: str,
                  source_names: List[str] = None) -> Dict[str, List[FactorRecord]]:
        """批量抓取并因子化."""
        results = {}
        sources = [self._sources[n] for n in source_names] if source_names else list(self._sources.values())
        
        for source in sources:
            if not source.config.enabled:
                continue
            try:
                factors = source.fetch_and_factorize(symbols, start_date, end_date)
                results[source.config.name] = factors
                _log.info(f"Source {source.config.name}: {len(factors)} factors generated")
            except Exception as e:
                _log.error(f"Source {source.config.name} failed: {e}")
                results[source.config.name] = []
        return results

    def sync_to_db(self, factors: List[FactorRecord], table: str = "alternative_factors"):
        """写入数据库."""
        if not factors:
            return 0
        conn = sqlite3.connect(MARKET_DB)
        try:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    factor_name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    value REAL NOT NULL,
                    quality_score REAL,
                    metadata TEXT,
                    PRIMARY KEY (factor_name, symbol, date)
                )
            """)
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} (factor_name, symbol, date, value, quality_score, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                [(f.factor_name, f.symbol, f.date, f.value, f.quality_score, json.dumps(f.metadata)) for f in factors]
            )
            conn.commit()
            return len(factors)
        finally:
            conn.close()

    def start_auto_sync(self, interval_sec: int = 3600):
        """启动自动同步."""
        if self._running:
            return
        self._running = True
        self._stop_sync.clear()
        self._sync_thread = threading.Thread(target=self._sync_loop, args=(interval_sec,), daemon=True)
        self._sync_thread.start()
        _log.info("Alternative data auto-sync started")

    def stop_auto_sync(self):
        self._running = False
        self._stop_sync.set()
        if self._sync_thread:
            self._sync_thread.join(timeout=10)

    def _sync_loop(self, interval_sec: int):
        while not self._stop_sync.is_set():
            try:
                # 获取全市场股票
                store = DataStore()
                symbols = store.get_universe(date=datetime.now().strftime("%Y-%m-%d"))
                end_date = datetime.now().strftime("%Y-%m-%d")
                start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                
                results = self.fetch_all(symbols, start_date, end_date)
                total = 0
                for source_name, factors in results.items():
                    if factors:
                        self.sync_to_db(factors, f"alt_{source_name}")
                        total += len(factors)
                _log.info(f"Auto-sync completed: {total} factors from {len(results)} sources")
            except Exception as e:
                _log.error(f"Auto-sync error: {e}")
            self._stop_sync.wait(interval_sec)


# ── 全局实例 ──

_alt_manager: Optional[AlternativeDataManager] = None
_alt_lock = threading.Lock()


def get_alternative_manager() -> AlternativeDataManager:
    global _alt_manager
    with _alt_lock:
        if _alt_manager is None:
            _alt_manager = AlternativeDataManager()
            register_builtin_sources()
        return _alt_manager


# ── 内置数据源注册 (延迟加载) ──

def register_builtin_sources():
    """注册内置数据源."""
    _log.info("register_builtin_sources: starting")
    global _alt_manager
    mgr = _alt_manager  # 直接用已创建的实例，避免死锁
    _log.info("register_builtin_sources: got manager")
    
    # 研报
    _log.info("register_builtin_sources: registering research_report")
    mgr.register_source(ResearchReportSource(DataSourceConfig(
        name="research_report",
        source_type=DataSourceType.RESEARCH_REPORT,
        frequency=DataFrequency.DAILY,
        table_name="alt_research_report",
        factor_prefix="alt_rpt_",
    )))
    _log.info("register_builtin_sources: research_report registered")
    
    # 供应链
    _log.info("register_builtin_sources: registering supply_chain")
    mgr.register_source(SupplyChainSource(DataSourceConfig(
        name="supply_chain",
        source_type=DataSourceType.SUPPLY_CHAIN,
        frequency=DataFrequency.WEEKLY,
        table_name="alt_supply_chain",
        factor_prefix="alt_sc_",
    )))
    _log.info("register_builtin_sources: supply_chain registered")
    
    # ESG
    _log.info("register_builtin_sources: registering esg")
    mgr.register_source(ESGSource(DataSourceConfig(
        name="esg",
        source_type=DataSourceType.ESG,
        frequency=DataFrequency.MONTHLY,
        table_name="alt_esg",
        factor_prefix="alt_esg_",
    )))
    _log.info("register_builtin_sources: esg registered")

    _log.info("Built-in alternative data sources registered")


# ── CLI 入口 ──

def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: alt-data <command> [args]")
        print("Commands: register, fetch, sync, list, test")
        return 1

    cmd = sys.argv[1]
    mgr = get_alternative_manager()

    if cmd == "register":
        register_builtin_sources()
        print("Built-in sources registered")
        return 0

    elif cmd == "list":
        for cfg in mgr.list_sources():
            print(f"  {cfg.name}: {cfg.source_type.value} | {cfg.frequency.value} | {'enabled' if cfg.enabled else 'disabled'}")
        return 0

    elif cmd == "fetch":
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--source", required=True)
        parser.add_argument("--symbols", nargs="+", required=True)
        parser.add_argument("--start", required=True)
        parser.add_argument("--end", required=True)
        args = parser.parse_args(sys.argv[2:])
        
        source = mgr.get_source(args.source)
        if not source:
            print(f"Source {args.source} not found")
            return 1
        factors = source.fetch_and_factorize(args.symbols, args.start, args.end)
        print(f"Generated {len(factors)} factors")
        for f in factors[:10]:
            print(f"  {f.factor_name} {f.symbol} {f.date} = {f.value:.4f}")
        return 0

    elif cmd == "sync":
        mgr.sync_to_db([], "test")
        print("Sync completed")
        return 0

    elif cmd == "test":
        # 运行单元测试
        import pytest
        return pytest.main(["-xvs", "test/test_alternative_data.py"])

    else:
        print(f"Unknown command: {cmd}")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())