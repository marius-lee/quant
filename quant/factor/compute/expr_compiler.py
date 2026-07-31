"""AI 因子表达式编译器 (ADR-040 Phase 1) — 文本表达式 → 可执行因子函数。

将类自然语言的表达式字符串编译为可执行的因子函数。编译后的函数
与现有因子函数签名完全兼容: (data, date) → pd.Series。

支持的操作符:
  算术: +, -, *, /, abs, sqrt, log, ^2, sign, neg
  时序: ts_mean(N), ts_std(N), ts_max(N), ts_min(N), ts_sum(N),
        ts_delay(N), ts_delta(N), ts_rank(N)
  截面: rank(), zscore(), cs_mean(), cs_std()
  引用: close, open, high, low, volume, amount, turnover, vwap

用法:
    from quant.factor.compute.expr_compiler import compile_factor
    fn = compile_factor("ts_mean(rank(close/open), 20)")
    result = fn(data, "2026-07-27")  # → pd.Series

设计: 递归下降解析器 → AST → 惰性求值函数。纯 Python，零依赖。
"""

import numpy as np
import pandas as pd
from typing import Callable

from quant.utils.logger import get_logger
_log = get_logger("factor.expr_compiler")


# ═══════════════════════════════════════════════════════════
# Tokenizer
# ═══════════════════════════════════════════════════════════

import re

_TOKEN_RE = re.compile(
    r'\s*(\d+(?:\.\d+)?|[a-zA-Z_]\w*|[+\-*/^(),]|<=|>=|!=|==|<|>)\s*'
)

_FIELD_MAP = {
    'close': 'close', 'c': 'close',
    'open': 'open', 'o': 'open',
    'high': 'high', 'h': 'high',
    'low': 'low', 'l': 'low',
    'volume': 'volume', 'v': 'volume',
    'amount': 'amount', 'a': 'amount',
    'turnover': 'turnover', 't': 'turnover',
}


def tokenize(expr: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(expr) if t]


# ═══════════════════════════════════════════════════════════
# AST nodes
# ═══════════════════════════════════════════════════════════

class ASTNode:
    def evaluate(self, data: pd.DataFrame, date_idx: int) -> pd.Series:
        raise NotImplementedError


class FieldRef(ASTNode):
    """引用一个价格/成交量字段: close, volume, etc."""
    def __init__(self, field: str):
        self.field = field

    def evaluate(self, data, date_idx):
        col = _FIELD_MAP.get(self.field, self.field)
        try:
            # MultiIndex columns: (field, symbol)
            if isinstance(data.columns, pd.MultiIndex) and col in data.columns.get_level_values(0):
                return data[col].iloc[date_idx]
            # Single-level columns
            if col in data.columns:
                return data[col].iloc[date_idx]
        except (KeyError, IndexError) as _e:
            _log.debug("expr_compiler lookup failed: %s", _e)
        idx = data.columns.get_level_values(1) if isinstance(data.columns, pd.MultiIndex) else data.columns
        return pd.Series(np.nan, index=idx, dtype=float)

    def __repr__(self):
        return f"Field({self.field})"


class Constant(ASTNode):
    def __init__(self, value: float):
        self.value = value

    def evaluate(self, data, date_idx):
        idx = data.columns.get_level_values(1) if isinstance(data.columns, pd.MultiIndex) else data.columns
        return pd.Series(self.value, index=idx, dtype=float)

    def __repr__(self):
        return f"Const({self.value})"


class BinaryOp(ASTNode):
    def __init__(self, op: str, left: ASTNode, right: ASTNode):
        self.op = op
        self.left = left
        self.right = right

    def evaluate(self, data, date_idx):
        l = self.left.evaluate(data, date_idx)
        r = self.right.evaluate(data, date_idx)
        with np.errstate(divide='ignore', invalid='ignore'):
            if self.op == '+': return l + r
            if self.op == '-': return l - r
            if self.op == '*': return l * r
            if self.op == '/': return l / r.replace(0, np.nan)
            if self.op == '^': return l ** r
            if self.op == '>': return (l > r).astype(float)
            if self.op == '<': return (l < r).astype(float)
            if self.op == '==': return (l == r).astype(float)
        return pd.Series(np.nan, index=l.index, dtype=float)

    def __repr__(self):
        return f"({self.left} {self.op} {self.right})"


class UnaryOp(ASTNode):
    def __init__(self, op: str, arg: ASTNode):
        self.op = op
        self.arg = arg

    def evaluate(self, data, date_idx):
        x = self.arg.evaluate(data, date_idx)
        with np.errstate(divide='ignore', invalid='ignore'):
            if self.op == 'abs': return x.abs()
            if self.op == 'sqrt': return np.sqrt(x.clip(0, None))
            if self.op == 'log': return np.log(x.clip(1e-10, None))
            if self.op == 'neg': return -x
            if self.op == 'sign': return np.sign(x)
            if self.op == 'sqr': return x ** 2
        return pd.Series(np.nan, index=x.index, dtype=float)

    def __repr__(self):
        return f"{self.op}({self.arg})"


class TimeseriesOp(ASTNode):
    """时序滚动运算: ts_mean(arg, window) → 过去 window 日的均值。"""
    def __init__(self, op: str, arg: ASTNode, window: int):
        self.op = op
        self.arg = arg
        self.window = window

    def evaluate(self, data, date_idx):
        # 需要全历史数据 — evaluate over full date range
        start = max(0, date_idx - self.window + 1)
        end = date_idx + 1

        # 逐日计算 arg，再滚动聚合
        daily_vals = []
        for i in range(start, end):
            daily_vals.append(self.arg.evaluate(data, i))
        panel = pd.DataFrame(daily_vals).astype(float)

        w = min(self.window, end - start)
        if self.op == 'mean':
            return panel.iloc[-w:].mean()
        if self.op == 'std':
            return panel.iloc[-w:].std()
        if self.op == 'max':
            return panel.iloc[-w:].max()
        if self.op == 'min':
            return panel.iloc[-w:].min()
        if self.op == 'sum':
            return panel.iloc[-w:].sum()
        if self.op == 'delay':
            d = min(self.window, len(panel) - 1)
            return panel.iloc[-d - 1] if len(panel) > d else pd.Series(np.nan, index=panel.columns)
        if self.op == 'delta':
            if len(panel) > self.window:
                return panel.iloc[-1] - panel.iloc[-self.window - 1]
            return pd.Series(np.nan, index=panel.columns)
        return pd.Series(np.nan, index=panel.columns, dtype=float)

    def __repr__(self):
        return f"ts_{self.op}({self.arg}, {self.window})"


class CrossSectionOp(ASTNode):
    """截面运算: rank(arg), zscore(arg), cs_mean(arg), cs_std(arg)"""
    def __init__(self, op: str, arg: ASTNode):
        self.op = op
        self.arg = arg

    def evaluate(self, data, date_idx):
        x = self.arg.evaluate(data, date_idx)
        x = pd.to_numeric(x, errors='coerce').dropna()
        idx = data.columns.get_level_values(1) if isinstance(data.columns, pd.MultiIndex) else data.columns
        if len(x) == 0:
            return pd.Series(np.nan, index=idx, dtype=float)
        if self.op == 'rank':
            r = x.rank(pct=True)
            return r.reindex(idx)
        if self.op == 'zscore':
            z = (x - x.mean()) / (x.std() + 1e-10)
            return z.reindex(idx)
        if self.op == 'cs_mean':
            return pd.Series(x.mean(), index=idx)
        if self.op == 'cs_std':
            return pd.Series(x.std(), index=idx)
        return pd.Series(np.nan, index=idx, dtype=float)

    def __repr__(self):
        return f"cs_{self.op}({self.arg})"


# ═══════════════════════════════════════════════════════════
# Parser: recursive descent
# ═══════════════════════════════════════════════════════════

_UNARY_FNS = {'abs', 'sqrt', 'log', 'neg', 'sign', 'sqr'}
_TS_FNS = {'ts_mean', 'ts_std', 'ts_max', 'ts_min', 'ts_sum', 'ts_delay', 'ts_delta', 'ts_rank'}
_CS_FNS = {'rank', 'zscore', 'cs_mean', 'cs_std'}


class Parser:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def consume(self) -> str:
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def expect(self, expected: str):
        t = self.consume()
        if t != expected:
            raise SyntaxError(f"Expected '{expected}', got '{t}' at position {self.pos}")

    def parse(self) -> ASTNode:
        node = self.parse_expr()
        if self.pos < len(self.tokens):
            raise SyntaxError(f"Unexpected token '{self.peek()}' at position {self.pos}")
        return node

    def parse_expr(self) -> ASTNode:
        return self.parse_comparison()

    def parse_comparison(self) -> ASTNode:
        left = self.parse_additive()
        while self.peek() in ('>', '<', '>=', '<=', '==', '!='):
            op = self.consume()
            right = self.parse_additive()
            left = BinaryOp(op, left, right)
        return left

    def parse_additive(self) -> ASTNode:
        left = self.parse_multiplicative()
        while self.peek() in ('+', '-'):
            op = self.consume()
            right = self.parse_multiplicative()
            left = BinaryOp(op, left, right)
        return left

    def parse_multiplicative(self) -> ASTNode:
        left = self.parse_unary()
        while self.peek() in ('*', '/'):
            op = self.consume()
            right = self.parse_unary()
            left = BinaryOp(op, left, right)
        return left

    def parse_unary(self) -> ASTNode:
        if self.peek() == '-':
            self.consume()
            return UnaryOp('neg', self.parse_unary())
        if self.peek() == '+':
            self.consume()
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self) -> ASTNode:
        t = self.peek()
        if t is None:
            raise SyntaxError("Unexpected end of expression")

        # Number
        try:
            val = float(t)
            self.consume()
            return Constant(val)
        except ValueError as _e:
            _log.debug("expr_compiler parse failed: %s", _e)

        # Function call: fn_name ( arg )
        if t in _UNARY_FNS:
            self.consume()
            self.expect('(')
            arg = self.parse_expr()
            self.expect(')')
            return UnaryOp(t, arg)

        if t in _TS_FNS:
            self.consume()
            self.expect('(')
            arg = self.parse_expr()
            self.expect(',')
            window = int(float(self.consume()))
            self.expect(')')
            return TimeseriesOp(t.replace('ts_', ''), arg, window)

        if t in _CS_FNS:
            self.consume()
            self.expect('(')
            arg = self.parse_expr()
            self.expect(')')
            return CrossSectionOp(t, arg)

        # Parenthesized expression
        if t == '(':
            self.consume()
            node = self.parse_expr()
            self.expect(')')
            return node

        # Field reference
        if t in _FIELD_MAP or re.match(r'^[a-zA-Z_]\w*$', t):
            self.consume()
            return FieldRef(t)

        raise SyntaxError(f"Unexpected token '{t}' at position {self.pos}")


# ═══════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════

def compile_factor(expr: str) -> Callable:
    """将表达式字符串编译为因子函数。

    返回的函数签名: fn(data: pd.DataFrame, date: str) -> pd.Series

    示例:
        fn = compile_factor("ts_mean(close, 20) / close - 1")
        result = fn(data, "2026-07-27")

        fn2 = compile_factor("rank(ts_mean(volume, 5) / ts_mean(volume, 60))")
        result2 = fn2(data, "2026-07-27")
    """
    tokens = tokenize(expr)
    if not tokens:
        raise ValueError("Empty expression")
    ast = Parser(tokens).parse()
    _log.debug("compiled: %s → %s", expr, ast)

    def factor_fn(data: pd.DataFrame, date_str: str) -> pd.Series:
        idx = data.columns.get_level_values(1) if isinstance(data.columns, pd.MultiIndex) else data.columns
        if date_str not in data.index:
            return pd.Series(np.nan, index=idx, dtype=float)
        date_idx = data.index.get_loc(date_str)
        result = ast.evaluate(data, date_idx)
        result = pd.to_numeric(result, errors='coerce')
        # test-v322: MultiIndex columns 导致索引含重复股票代码, 取唯一值对齐 forward return
        if isinstance(data.columns, pd.MultiIndex):
            result = result.groupby(level=0).first()
        name = expr.replace(' ', '_').replace('(', '').replace(')', '')[:50]
        result.name = f"expr_{name}"
        return result

    return factor_fn


def parse_expression(expr: str) -> ASTNode:
    """返回 AST 而不执行（用于表达式分析和优化）。"""
    tokens = tokenize(expr)
    if not tokens:
        raise ValueError("Empty expression")
    return Parser(tokens).parse()
