"""Factor Platform — 注册/血缘/文档/测试/回测/上线全流程 CI/CD.

统一因子全生命周期管理:
  1. 注册中心: 元数据/版本/依赖/血缘
  2. 文档化: 自动生成因子说明书 (公式/来源/IC/参数)
  3. 测试框架: 单元/集成/回测/冒烟
  4. CI/CD: 自动化评估/注册/部署/回滚
  5. 治理: 权限/审计/合规/下线

架构:
  Factor Registry (PostgreSQL) → CI Pipeline (GitHub Actions) → Staging/Prod 环境
"""

import os
import json
import yaml
import hashlib
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field, asdict
from enum import Enum
from contextlib import contextmanager

import pandas as pd
import numpy as np

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.factor.state_machine import FactorStateMachine

_log = get_logger("factor.platform")


class FactorStatus(str, Enum):
    DRAFT = "draft"              # 草稿态
    PENDING_REVIEW = "pending"   # 待评审
    ACTIVE = "active"            # 生产可用
    DEPRECATED = "deprecated"    # 废弃
    ARCHIVED = "archived"        # 归档


class FactorCategory(str, Enum):
    PRICE = "price"           # 价量类
    FUNDAMENTAL = "fundamental"  # 基本面
    ALTERNATIVE = "alternative"  # 另类数据
    ML = "ml"                 # ML 生成
    COMPOSITE = "composite"   # 合成因子


@dataclass
class FactorMetadata:
    """因子元数据完整定义."""
    name: str
    version: str
    expression: str
    category: FactorCategory
    source: str
    author: str
    description: str
    formula_latex: str = ""
    dependencies: List[str] = field(default_factory=list)  # 依赖因子名
    parameters: Dict[str, Any] = field(default_factory=dict)  # 可调参数
    tags: List[str] = field(default_factory=list)
    status: FactorStatus = FactorStatus.DRAFT
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    created_by: str = ""
    approved_by: str = ""
    approved_at: str = ""
    lineage: Dict[str, Any] = field(default_factory=dict)  # 血缘信息

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FactorMetadata":
        data = data.copy()
        data["category"] = FactorCategory(data["category"])
        data["status"] = FactorStatus(data["status"])
        return cls(**data)


@dataclass
class FactorTestCase:
    """因子测试用例."""
    name: str
    description: str
    input_data: Dict[str, Any]  # 输入数据 (symbols, dates, params)
    expected_output: Dict[str, Any]  # 期望输出 (shape, stats, values)
    tolerance: float = 1e-6
    tags: List[str] = field(default_factory=list)


@dataclass
class FactorPipelineResult:
    """流水线执行结果."""
    factor_name: str
    version: str
    stage: str  # compile, test, backtest, register, deploy
    status: str  # success, failed, skipped
    duration_sec: float
    details: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


class FactorRegistry:
    """因子注册中心 — PostgreSQL 持久化."""

    def __init__(self, dsn: str = None):
        self.dsn = dsn or os.environ.get("FACTOR_REGISTRY_DSN", "postgresql://localhost/factor_registry")
        self._pool = None
        self._init_db()

    def _get_conn(self):
        import psycopg2
        from psycopg2.pool import ThreadedConnectionPool
        if self._pool is None:
            self._pool = ThreadedConnectionPool(1, 10, self.dsn)
        return self._pool.getconn()

    def _put_conn(self, conn):
        if self._pool:
            self._pool.putconn(conn)

    def _init_db(self):
        """初始化表结构."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS factor_metadata (
                        name VARCHAR(100) PRIMARY KEY,
                        version VARCHAR(20) NOT NULL,
                        expression TEXT NOT NULL,
                        category VARCHAR(30) NOT NULL,
                        source VARCHAR(100),
                        author VARCHAR(50),
                        description TEXT,
                        formula_latex TEXT,
                        dependencies JSONB DEFAULT '[]',
                        parameters JSONB DEFAULT '{}',
                        tags JSONB DEFAULT '[]',
                        status VARCHAR(20) DEFAULT 'draft',
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW(),
                        created_by VARCHAR(50),
                        approved_by VARCHAR(50),
                        approved_at TIMESTAMP,
                        lineage JSONB DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS idx_factor_category ON factor_metadata(category);
                    CREATE INDEX IF NOT EXISTS idx_factor_status ON factor_metadata(status);
                    CREATE INDEX IF NOT EXISTS idx_factor_author ON factor_metadata(author);
                """)
                conn.commit()
        finally:
            self._put_conn(conn)

    def register(self, metadata: FactorMetadata) -> bool:
        """注册新因子版本."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO factor_metadata
                    (name, version, expression, category, source, author, description,
                     formula_latex, dependencies, parameters, tags, status, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET
                        version = EXCLUDED.version,
                        expression = EXCLUDED.expression,
                        category = EXCLUDED.category,
                        source = EXCLUDED.source,
                        description = EXCLUDED.description,
                        formula_latex = EXCLUDED.formula_latex,
                        dependencies = EXCLUDED.dependencies,
                        parameters = EXCLUDED.parameters,
                        tags = EXCLUDED.tags,
                        status = EXCLUDED.status,
                        updated_at = NOW(),
                        updated_by = EXCLUDED.created_by
                    RETURNING version
                """, (
                    metadata.name, metadata.version, metadata.expression,
                    metadata.category.value, metadata.source, metadata.author,
                    metadata.description, metadata.formula_latex,
                    json.dumps(metadata.dependencies), json.dumps(metadata.parameters),
                    json.dumps(metadata.tags), metadata.status.value, metadata.created_by
                ))
                conn.commit()
                return True
        except Exception as e:
            _log.error(f"Register factor failed: {e}")
            conn.rollback()
            return False
        finally:
            self._put_conn(conn)

    def get(self, name: str, version: str = None) -> Optional[FactorMetadata]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if version:
                    cur.execute("SELECT * FROM factor_metadata WHERE name = %s AND version = %s", (name, version))
                else:
                    cur.execute("SELECT * FROM factor_metadata WHERE name = %s ORDER BY version DESC LIMIT 1", (name,))
                row = cur.fetchone()
                if not row:
                    return None
                cols = [desc[0] for desc in cur.description]
                data = dict(zip(cols, row))
                return FactorMetadata(
                    name=data["name"],
                    version=data["version"],
                    expression=data["expression"],
                    category=FactorCategory(data["category"]),
                    source=data["source"],
                    author=data["author"],
                    description=data["description"],
                    formula_latex=data.get("formula_latex", ""),
                    dependencies=data.get("dependencies", []),
                    parameters=data.get("parameters", {}),
                    tags=data.get("tags", []),
                    status=FactorStatus(data["status"]),
                    created_at=str(data["created_at"]),
                    updated_at=str(data["updated_at"]),
                    created_by=data["created_by"],
                    approved_by=data.get("approved_by", ""),
                    approved_at=str(data["approved_at"]) if data.get("approved_at") else "",
                    lineage=data.get("lineage", {}),
                )
        finally:
            self._put_conn(conn)

    def list(self, category: FactorCategory = None, status: FactorStatus = None) -> List[FactorMetadata]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                sql = "SELECT * FROM factor_metadata WHERE 1=1"
                params = []
                if category:
                    sql += " AND category = %s"
                    params.append(category.value)
                if status:
                    sql += " AND status = %s"
                    params.append(status.value)
                sql += " ORDER BY name, version DESC"
                cur.execute(sql, params)
                rows = cur.fetchall()
                cols = [desc[0] for desc in cur.description]
                return [FactorMetadata(
                    name=r[0], version=r[1], expression=r[2], category=FactorCategory(r[3]),
                    source=r[3], author=r[4], description=r[5], formula_latex=r[6] or "",
                    dependencies=r[6] or [], parameters=r[7] or {}, tags=r[8] or [],
                    status=FactorStatus(r[9]), created_at=str(r[10]), updated_at=str(r[10]),
                    created_by=r[11], approved_by=r[12] or "", approved_at=str(r[13]) if r[13] else "",
                    lineage=r[14] or {},
                ) for r in rows]
        finally:
            self._put_conn(conn)

    def update_status(self, name: str, version: str, status: FactorStatus, approved_by: str = "") -> bool:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE factor_metadata
                    SET status = %s, approved_by = %s, approved_at = NOW(), updated_at = NOW()
                    WHERE name = %s AND version = %s
                """, (status.value, approved_by, name, version))
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._put_conn(conn)

    def get_lineage(self, name: str, version: str) -> Dict[str, Any]:
        """获取因子血缘: 上游依赖 + 下游依赖."""
        meta = self.get(name, version)
        if not meta:
            return {"upstream": [], "downstream": []}

        upstream = meta.dependencies
        # 查找下游: 依赖该因子的其他因子
        conn = self._get_conn()
        downstream = []
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT name, version FROM factor_metadata
                    WHERE %s = ANY(dependencies)
                """, (meta.name,))
                downstream = [{"name": r[0], "version": r[1]} for r in cur.fetchall()]
        finally:
            self._put_conn(conn)

        return {"upstream": upstream, "downstream": downstream}


class FactorDocumentGenerator:
    """因子文档自动生成器."""

    TEMPLATE = """# {name} v{version}

**分类**: {category}
**来源**: {source}
**作者**: {author}
**状态**: {status}
**创建时间**: {created_at}
**更新时间**: {updated_at}

## 描述
{description}

## 数学公式
{formula_latex}

## 表达式
```python
{expression}
```

## 参数
{parameters_table}

## 依赖因子
{dependencies_list}

## 标签
{tags}

## 血缘关系
{lineage}

## 测试用例
{test_cases}

---
*自动生成于 {generated_at}*
"""

    def generate(self, metadata: FactorMetadata, test_cases: List[FactorTestCase] = None,
                 lineage: Dict[str, Any] = None) -> str:
        # 参数表
        params_md = "| 参数 | 类型 | 默认值 | 说明 |\n|------|------|--------|------|\n"
        for k, v in metadata.parameters.items():
            if isinstance(v, dict):
                default = v.get("default", "")
                typ = v.get("type", "any")
                desc = v.get("description", "")
            else:
                default = v
                typ = type(v).__name__
                desc = ""
            params_md += f"| {k} | {typ} | {default} | {desc} |\n"

        # 依赖列表
        deps_md = ", ".join(f"`{d}`" for d in metadata.dependencies) if metadata.dependencies else "无"

        # 标签
        tags_md = ", ".join(f"`{t}`" for t in metadata.tags) if metadata.tags else "无"

        # 血缘
        lineage_md = ""
        if lineage:
            up = ", ".join(f"`{u}`" for u in lineage.get("upstream", [])) or "无"
            down = ", ".join(f"`{d['name']} v{d['version']}`" for d in lineage.get("downstream", [])) or "无"
            lineage_md = f"上游依赖: {up}\n\n下游依赖: {down}"
        else:
            lineage_md = "暂无"

        # 测试用例
        tests_md = ""
        if test_cases:
            for tc in test_cases:
                tests_md += f"### {tc.name}\n{tc.description}\n\n"
                tests_md += f"**输入**: {json.dumps(tc.input_data, indent=2, ensure_ascii=False)}\n\n"
                tests_md += f"**期望输出**: {json.dumps(tc.expected_output, indent=2, ensure_ascii=False)}\n\n"
                tests_md += f"**容差**: {tc.tolerance}\n\n---\n"
        else:
            tests_md = "暂无"

        return self.TEMPLATE.format(
            name=metadata.name,
            version=metadata.version,
            category=metadata.category.value,
            source=metadata.source,
            author=metadata.created_by,
            status=metadata.status.value,
            created_at=metadata.created_at,
            updated_at=metadata.updated_at,
            description=metadata.description,
            formula_latex=metadata.formula_latex or "无",
            expression=metadata.expression,
            parameters_table=params_md,
            dependencies_list=deps_md,
            tags=tags_md,
            lineage=lineage_md,
            test_cases=tests_md,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def save_markdown(self, metadata: FactorMetadata, output_dir: Path, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        md = self.generate(metadata, **kwargs)
        path = output_dir / f"{metadata.name}_v{metadata.version}.md"
        path.write_text(md, encoding="utf-8")
        return path


class FactorTestRunner:
    """因子测试执行器."""

    def __init__(self, registry: FactorRegistry):
        self.registry = registry

    def run_tests(self, factor_name: str, version: str = None,
                  test_cases: List[FactorTestCase] = None) -> Dict[str, Any]:
        """运行因子测试套件."""
        metadata = self.registry.get(factor_name, version)
        if not metadata:
            return {"success": False, "error": "Factor not found"}

        # 编译因子
        from quant.factor.compute.expr_compiler import compile_factor
        try:
            fn = compile_factor(metadata.expression)
        except Exception as e:
            return {"success": False, "error": f"Compile failed: {e}", "stage": "compile"}

        # 加载测试数据
        from quant.data.store import DataStore
        store = DataStore()

        results = {
            "factor": factor_name,
            "version": metadata.version,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "cases": [],
        }

        # 运行内置测试用例
        test_cases = test_cases or self._get_default_test_cases(metadata)
        for tc in test_cases:
            result = self._run_single_test(fn, tc, store)
            results["total"] += 1
            if result["passed"]:
                results["passed"] += 1
            else:
                results["failed"] += 1
            results["cases"].append(result)

        # 计算整体状态
        results["success"] = results["failed"] == 0
        return results

    def _get_default_test_cases(self, metadata: FactorMetadata) -> List[FactorTestCase]:
        """生成默认测试用例."""
        return [
            FactorTestCase(
                name="basic_computation",
                description="基本计算正确性",
                input_data={"symbols": ["000001", "600000"], "date": "2024-01-01"},
                expected_output={"shape": (2,), "dtype": "float64", "no_nan": True},
            ),
            FactorTestCase(
                name="nan_handling",
                description="NaN 处理正确性",
                input_data={"symbols": ["000001", "INVALID"], "date": "2024-01-01"},
                expected_output={"shape": (2,), "allow_nan": True},
            ),
            FactorTestCase(
                name="boundary_values",
                description="边界值处理",
                input_data={"symbols": ["000001"], "date": "2020-01-01"},
                expected_output={"shape": (1,), "finite": True},
            ),
        ]

    def _run_single_test(self, fn, tc: FactorTestCase, store) -> Dict[str, Any]:
        """运行单个测试用例."""
        try:
            # 准备数据
            symbols = tc.input_data.get("symbols", [])
            date = tc.input_data.get("date")
            params = tc.input_data.get("params", {})

            # 获取数据
            data = store.get_daily(symbols, start=date, end=date)
            fundamentals = store.get_fundamentals(symbols, date=date)

            # 执行因子
            from quant.factor.compute._dispatch import compute_all_factors
            fv = compute_all_factors(data, date, fundamentals=fundamentals,
                                     factor_names=[metadata.name], factor_fail_fast=False)

            result = fv.get(metadata.name)
            if result is None:
                return {"name": tc.name, "passed": False, "error": "Factor returned None"}

            # 验证
            checks = []
            exp = tc.expected_output
            if "shape" in exp:
                checks.append(("shape", result.shape == tuple(exp["shape"])))
            if "dtype" in exp:
                checks.append(("dtype", str(result.dtype) == exp["dtype"]))
            if "no_nan" in exp and exp["no_nan"]:
                checks.append(("no_nan", not result.isna().any()))
            if "allow_nan" in exp and exp["allow_nan"]:
                checks.append(("allow_nan", result.isna().any()))
            if "finite" in exp and exp["finite"]:
                checks.append(("finite", np.isfinite(result).all()))

            all_passed = all(c[1] for c in checks)
            return {
                "name": tc.name,
                "passed": all_passed,
                "checks": [{"check": c[0], "passed": c[1]} for c in checks],
            }
        except Exception as e:
            return {"name": tc.name, "passed": False, "error": str(e)}


class FactorPipeline:
    """因子 CI/CD 流水线.

    Stages:
      1. compile     - 编译表达式
      2. test        - 单元/集成测试
      3. backtest    - 走前回测 (可选)
      4. register    - 注册到注册中心
      5. deploy      - 部署到生产环境
    """

    STAGES = ["compile", "test", "backtest", "register", "deploy"]

    def __init__(self, registry: FactorRegistry = None):
        self.registry = registry or FactorRegistry()
        self.test_runner = FactorTestRunner(self.registry)
        self.doc_generator = FactorDocumentGenerator()

    def run(self, factor_name: str, version: str = None,
            stages: List[str] = None, auto_approve: bool = False) -> List[FactorPipelineResult]:
        """执行完整流水线."""
        if stages is None:
            stages = self.STAGES

        results = []
        metadata = self.registry.get(factor_name, version)
        if not metadata:
            return [FactorPipelineResult(
                factor_name=factor_name, version=version or "latest",
                stage="init", status="failed",
                duration_sec=0, error="Factor not found"
            )]

        for stage in stages:
            if stage not in self.STAGES:
                continue

            t0 = time.time()
            result = self._run_stage(stage, metadata)
            duration = time.time() - t0

            results.append(FactorPipelineResult(
                factor_name=metadata.name,
                version=metadata.version,
                stage=stage,
                status=result.get("status", "failed"),
                duration_sec=duration,
                details=result.get("details", {}),
                error=result.get("error", ""),
            ))

            if not result.get("success", False):
                _log.error(f"Pipeline {metadata.name} failed at {stage}: {result.get('error')}")
                break

        return results

    def _run_stage(self, stage: str, metadata: FactorMetadata) -> Dict[str, Any]:
        if stage == "compile":
            return self._stage_compile(metadata)
        elif stage == "test":
            return self._stage_test(metadata)
        elif stage == "backtest":
            return self._stage_backtest(metadata)
        elif stage == "register":
            return self._stage_register(metadata)
        elif stage == "deploy":
            return self._stage_deploy(metadata)
        return {"success": False, "error": f"Unknown stage: {stage}"}

    def _stage_compile(self, metadata: FactorMetadata) -> Dict[str, Any]:
        from quant.factor.compute.expr_compiler import compile_factor
        try:
            fn = compile_factor(metadata.expression)
            return {"success": True, "details": {"compiled": True}}
        except Exception as e:
            return {"success": False, "error": f"Compile failed: {e}"}

    def _stage_test(self, metadata: FactorMetadata) -> Dict[str, Any]:
        result = self.test_runner.run_tests(metadata.name, metadata.version)
        return {
            "success": result["success"],
            "details": result,
            "error": result.get("error", "") if not result["success"] else "",
        }

    def _stage_backtest(self, metadata: FactorMetadata) -> Dict[str, Any]:
        """快速回测验证."""
        try:
            from quant.backtest.loop import run_backtest
            result = run_backtest(
                start_date="2022-01-01",
                end_date="2023-12-31",
                capital=10000,
                strategy=f"factor_{metadata.name}",
                universe_size=100,
                factor_status_filter="backtesting",
            )
            sharpe = result.get("metrics", {}).get("sharpe", 0)
            if sharpe < 0.5:
                return {"success": False, "error": f"Sharpe too low: {sharpe}"}
            return {"success": True, "details": {"sharpe": sharpe}}
        except Exception as e:
            return {"success": False, "error": f"Backtest failed: {e}"}

    def _stage_register(self, metadata: FactorMetadata) -> Dict[str, Any]:
        # 更新状态
        new_status = FactorStatus.ACTIVE
        if metadata.status == FactorStatus.PENDING_REVIEW:
            new_status = FactorStatus.ACTIVE

        ok = self.registry.update_status(metadata.name, metadata.version, new_status, "pipeline")
        if not ok:
            return {"success": False, "error": "Registration failed"}
        return {"success": True, "details": {"status": new_status.value}}

    def _stage_deploy(self, metadata: FactorMetadata) -> Dict[str, Any]:
        """生成文档、生成部署包."""
        # 生成文档
        doc_dir = Path("docs/factors")
        self.doc_generator.save_markdown(metadata, Path("docs/factors"))

        # 生成部署包 (可选: Docker 镜像/Helm Chart)
        deploy_info = {
            "docs_generated": True,
            "doc_path": f"docs/factors/{metadata.name}_v{metadata.version}.md",
        }
        return {"success": True, "details": deploy_info}

    def generate_docs(self, factor_name: str, version: str = None,
                      output_dir: str = "docs/factors") -> Path:
        """生成因子文档."""
        metadata = self.registry.get(factor_name, version)
        if not metadata:
            raise ValueError(f"Factor {factor_name} not found")

        test_cases = self.test_runner._get_default_test_cases(metadata)
        lineage = self.registry.get_lineage(metadata.name, metadata.version)
        path = self.doc_generator.save_markdown(metadata, Path(output_dir),
                                                test_cases=test_cases, lineage=lineage)
        return path

    def run_ci(self, factor_name: str, version: str = None) -> Dict[str, Any]:
        """CI 入口: 运行完整流水线."""
        results = self.run(factor_name, version)
        overall_success = all(r.status == "success" for r in results)

        return {
            "factor": factor_name,
            "version": version,
            "overall_success": overall_success,
            "stages": [asdict(r) for r in results],
        }


# ── CI/CD 配置生成器 ──

class CICDGenerator:
    """生成 GitHub Actions / GitLab CI 配置."""

    GITHUB_ACTIONS_TEMPLATE = """name: Factor CI/CD

on:
  push:
    branches: [main, develop]
    paths:
      - 'quant/factor/**'
      - 'tests/factor/**'
  pull_request:
    branches: [main, develop]
  workflow_dispatch:
    inputs:
      factor_name:
        description: 'Factor name to test'
        required: false
      stage:
        description: 'Stage to run (compile/test/backtest/register/deploy/all)'
        required: false
        default: 'all'

jobs:
  factor-ci:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
      
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'
      
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install -e .
      
      - name: Run Factor Pipeline
        env:
          FACTOR_NAME: ${{ github.event.inputs.factor_name }}
          STAGE: ${{ github.event.inputs.stage }}
        run: |
          python -c "
from quant.factor.platform import FactorPipeline
pipeline = FactorPipeline()
result = pipeline.run('${{ env.FACTOR_NAME }}', stage='${{ env.STAGE }}')
import json
print(json.dumps([r.__dict__ for r in result], default=str))
          "

  deploy-staging:
    needs: factor-ci
    if: github.ref == 'refs/heads/main' && success()
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Deploy to Staging
        run: |
          echo "Deploy to staging environment"
          # kubectl apply -f k8s/staging/

  deploy-production:
    needs: deploy-staging
    if: github.ref == 'refs/heads/main' && success()
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - name: Deploy to Production
        run: |
          echo "Deploy to production"
          # kubectl apply -f k8s/production/

  notify:
    needs: [deploy-staging, deploy-production]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Notify
        run: |
          echo "Pipeline completed"

"""

    @classmethod
    def generate_github_actions(cls, output_dir: Path = Path(".github/workflows")):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "factor-ci.yml"
        path.write_text(cls.GITHUB_ACTIONS_TEMPLATE)
        _log.info(f"GitHub Actions workflow generated: {path}")

    @classmethod
    def generate_gitlab_ci(cls, output_path: Path = Path(".gitlab-ci.yml")):
        gitlab_ci = """
stages:
  - compile
  - test
  - backtest
  - register
  - deploy

variables:
  PYTHONPATH: "."
  PYTHON_VERSION: "3.12"

compile:
  stage: compile
  script:
    - python -c "from quant.factor.platform import FactorPipeline; p=FactorPipeline(); r=p.run('$FACTOR_NAME', stage='compile'); print(r)"
  artifacts:
    reports:
      junit: test-results.xml

test:
  stage: test
  script:
    - python -c "from quant.factor.platform import FactorPipeline; p=FactorPipeline(); r=p.run('$FACTOR_NAME', stage='test'); print(r)"
  coverage: '/Coverage: \d+\.\d+%/'

backtest:
  stage: backtest
  script:
    - python -c "from quant.factor.platform import FactorPipeline; p=FactorPipeline(); r=p.run('$FACTOR_NAME', stage='backtest'); print(r)"
  when: manual
  allow_failure: true

register:
  stage: register
  script:
    - python -c "from quant.factor.platform import FactorPipeline; p=FactorPipeline(); r=p.run('$FACTOR_NAME', stage='register'); print(r)"
  when: manual
  only:
    - main

deploy:
  stage: deploy
  script:
    - echo "Deploy to production"
  when: manual
  only:
    - tags
  environment:
    name: production
"""
        output_path.write_text(gitlab_ci)
        _log.info(f"GitLab CI generated: {output_path}")


# ── 全局实例 ──

_factor_registry: Optional[FactorRegistry] = None
_factor_pipeline: Optional[FactorPipeline] = None


def get_factor_registry() -> FactorRegistry:
    global _factor_registry
    if _factor_registry is None:
        _factor_registry = FactorRegistry()
    return _factor_registry


def get_factor_pipeline() -> FactorPipeline:
    global _factor_pipeline
    if _factor_pipeline is None:
        _factor_pipeline = FactorPipeline(get_factor_registry())
    return _factor_pipeline


# ── CLI 入口 ──

def main():
    """CLI 入口: factor-platform <command> [args]."""
    import sys
    if len(sys.argv) < 2:
        print("Usage: factor-platform <command> [args]")
        print("Commands: register, test, pipeline, docs, ci-gen")
        return 1

    cmd = sys.argv[1]
    registry = get_factor_registry()
    pipeline = get_factor_pipeline()

    if cmd == "register":
        # factor-platform register <name> <expression> <source> <direction> <category> [--version]
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        parser.add_argument("expression")
        parser.add_argument("source")
        parser.add_argument("direction", choices=["positive", "negative"])
        parser.add_argument("category", choices=[c.value for c in FactorCategory])
        parser.add_argument("--version", default="1.0")
        parser.add_argument("--source", default="internal")
        parser.add_argument("--author", default="auto")
        parser.add_argument("--description", default="")
        args = parser.parse_args(sys.argv[2:])

        meta = FactorMetadata(
            name=args.name, version=args.version, expression=args.expression,
            category=FactorCategory(args.category), source=args.source,
            author=args.author, direction=args.direction,
            description=args.description,
        )
        ok = get_factor_registry().register(meta)
        print("Registered" if ok else "Failed")
        return 0 if ok else 1

    elif cmd == "test":
        # factor-platform test <factor_name> [--version]
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        parser.add_argument("--version", default=None)
        args = parser.parse_args(sys.argv[2:])
        result = get_factor_pipeline().test_runner.run_tests(args.name, args.version)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["success"] else 1

    elif cmd == "pipeline":
        # factor-platform pipeline <factor_name> [--version] [--stages]
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        parser.add_argument("--version", default=None)
        parser.add_argument("--stages", nargs="+", default=None)
        args = parser.parse_args(sys.argv[2:])
        results = get_factor_pipeline().run(args.name, args.version, args.stages)
        for r in results:
            print(f"  {r.stage}: {r.status} ({r.duration_sec:.1f}s)")
            if r.error:
                print(f"  ERROR: {r.error}")
        return 0 if all(r.status == "success" for r in results) else 1

    elif cmd == "docs":
        # factor-platform docs <factor_name> [--version] [--output-dir]
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("name")
        parser.add_argument("--version", default=None)
        parser.add_argument("--output-dir", default="docs/factors")
        args = parser.parse_args(sys.argv[2:])
        path = get_factor_pipeline().generate_docs(args.name, args.version, args.output_dir)
        print(f"Documentation generated: {path}")
        return 0

    elif cmd == "ci-gen":
        # factor-platform ci-gen [--github|--gitlab] [--output-dir]
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--github", action="store_true")
        parser.add_argument("--gitlab", action="store_true")
        parser.add_argument("--output-dir", default=".github/workflows")
        args = parser.parse_args(sys.argv[2:])
        if args.github:
            CICDGenerator.generate_github_actions(Path(args.output_dir))
        if args.gitlab:
            CICDGenerator.generate_gitlab_ci(Path(".gitlab-ci.yml"))
        print("CI/CD config generated")
        return 0

    else:
        print(f"Unknown command: {cmd}")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())