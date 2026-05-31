"""测试 utils/dates.py — 日期格式标准化"""
import pandas as pd
import pytest
from utils.dates import norm_yyyymmdd, norm_yyyy_mm_dd, to_timestamp, compare_dates


class TestNormYyyymmdd:
    def test_from_yyyy_mm_dd(self):
        assert norm_yyyymmdd("2024-01-15") == "20240115"

    def test_from_yyyymmdd(self):
        assert norm_yyyymmdd("20240115") == "20240115"

    def test_from_timestamp(self):
        ts = pd.Timestamp("2024-01-15")
        assert norm_yyyymmdd(ts) == "20240115"

    def test_none(self):
        assert norm_yyyymmdd(None) is None


class TestNormYyyyMmDd:
    def test_from_yyyymmdd(self):
        assert norm_yyyy_mm_dd("20240115") == "2024-01-15"

    def test_from_yyyy_mm_dd(self):
        assert norm_yyyy_mm_dd("2024-01-15") == "2024-01-15"

    def test_none(self):
        assert norm_yyyy_mm_dd(None) is None


class TestCompareDates:
    def test_same(self):
        assert compare_dates("20240115", "2024-01-15") == 0

    def test_earlier(self):
        assert compare_dates("20240115", "20240201") == -1

    def test_later(self):
        assert compare_dates("20240201", "2024-01-15") == 1
