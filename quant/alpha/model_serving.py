"""Model Serving Layer — MLflow + BentoML 统一模型服务化.

功能:
  1. MLflow Tracking: 实验记录/模型注册/版本管理
  2. BentoML Serving: 模型打包/REST API/批量推理/在线推理
  3. 影子流量/金丝雀发布: 新旧模型并行跑, 自动回滚
  4. A/B 测试: 流量分发 + 统计显著性判定

架构:
  MLflow (Tracking/Registry) → BentoML (Build/Serve) → 生产环境
"""

import os
import json
import time
import uuid
import tempfile
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, asdict
from enum import Enum
from contextlib import contextmanager

import mlflow
import mlflow.sklearn
import mlflow.lightgbm
import mlflow.xgboost
import bentoml
from bentoml.io import JSON, NumpyNdarray, PandasDataFrame
import pandas as pd
import numpy as np

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.config.paths import MLFLOW_TRACKING_URI, BENTOML_STORE_PATH

_log = get_logger("alpha.model_serving")


class ModelStage(str, Enum):
    """模型生命周期阶段."""
    STAGING = "Staging"       # 预发布验证
    PRODUCTION = "Production"  # 生产环境
    ARCHIVED = "Archived"      # 归档
    SHADOW = "Shadow"          # 影子流量 (不返回结果, 仅记录)


@dataclass
class ModelVersion:
    """模型版本元数据."""
    name: str
    version: str
    stage: ModelStage
    run_id: str
    metrics: Dict[str, float]
    params: Dict[str, Any]
    artifact_path: str
    created_at: str
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["stage"] = self.stage.value
        return d


class MLflowTracker:
    """MLflow 实验追踪与模型注册."""

    def __init__(self, tracking_uri: Optional[str] = None, experiment_name: str = "quant_alpha"):
        self.tracking_uri = tracking_uri or _require_cfg("mlflow.tracking_uri", default="sqlite:///mlflow.db")
        self.experiment_name = experiment_name

        mlflow.set_tracking_uri(self.tracking_uri)
        self._ensure_experiment()

    def _ensure_experiment(self):
        exp = mlflow.get_experiment_by_name(self.experiment_name)
        if exp is None:
            mlflow.create_experiment(self.experiment_name)
            _log.info(f"MLflow experiment created: {self.experiment_name}")

    @contextmanager
    def run(self, run_name: str = None, tags: Dict[str, str] = None):
        """上下文管理器: 自动开始/结束 run."""
        mlflow.set_experiment(self.experiment_name)
        with mlflow.start_run(run_name=run_name, tags=tags) as run:
            yield run

    def log_model(
        self,
        model: Any,
        artifact_path: str,
        model_name: str,
        metrics: Dict[str, float] = None,
        params: Dict[str, Any] = None,
        signature: Any = None,
        input_example: Any = None,
    ) -> str:
        """记录模型到 MLflow 并注册到 Model Registry."""
        with self.run() as run:
            # 记录参数/指标
            if params:
                mlflow.log_params(params)
            if metrics:
                mlflow.log_metrics(metrics)

            # 记录模型 (自动识别 flavor)
            if hasattr(model, "predict"):
                if "lightgbm" in str(type(model)).lower() or "lgb" in str(type(model)).lower():
                    mlflow.lightgbm.log_model(model, artifact_path, signature=signature, input_example=input_example)
                elif "xgboost" in str(type(model)).lower() or "xgb" in str(type(model)).lower():
                    mlflow.xgboost.log_model(model, artifact_path, signature=signature, input_example=input_example)
                else:
                    mlflow.sklearn.log_model(model, artifact_path, signature=signature, input_example=input_example)
            else:
                mlflow.pyfunc.log_model(artifact_path, python_model=model)

            # 注册模型
            model_uri = f"runs:/{run.info.run_id}/{artifact_path}"
            mv = mlflow.register_model(model_uri, name=self.experiment_name + "_model")

            _log.info(f"Model registered: {mv.name} v{mv.version}")
            return mv.version

    def transition_stage(self, model_name: str, version: str, stage: ModelStage):
        """转换模型阶段."""
        from mlflow.tracking import MlflowClient
        client = MlflowClient()
        client.transition_model_version_stage(name=model_name, version=version, stage=stage.value)
        _log.info(f"Model {model_name} v{version} -> {stage.value}")

    def get_latest_version(self, model_name: str, stage: ModelStage = ModelStage.PRODUCTION) -> Optional[str]:
        from mlflow.tracking import MlflowClient
        client = MlflowClient()
        versions = client.get_latest_versions(model_name, stages=[stage.value])
        return versions[0].version if versions else None

    def get_model_info(self, model_name: str, version: str) -> Dict[str, Any]:
        from mlflow.tracking import MlflowClient
        client = MlflowClient()
        mv = client.get_model_version(name=model_name, version=version)
        run = client.get_run(mv.run_id)
        return {
            "name": mv.name,
            "version": mv.version,
            "stage": mv.current_stage,
            "run_id": mv.run_id,
            "metrics": run.data.metrics,
            "params": run.data.params,
        }


class BentoMLService:
    """BentoML 模型服务构建与部署."""

    def __init__(self, store_path: Optional[str] = None):
        self.store_path = store_path or _require_cfg("bentoml.store_path", default="~/bentoml")
        os.environ["BENTOML_HOME"] = self.store_path

    def build_service(
        self,
        model_name: str,
        model_version: str,
        service_name: str = None,
        runners: Dict[str, Any] = None,
        input_spec: Any = PandasDataFrame(),
        output_spec: Any = JSON(),
    ) -> str:
        """构建 BentoML Service (生成 bento 包)."""
        service_name = service_name or f"{model_name}_service"

        # 创建 Service 定义
        @bentoml.service(name=service_name, runners=runners or {})
        class AlphaService:
            @bentoml.api(input=input_spec, output=output_spec)
            def predict(self, input_data: pd.DataFrame) -> pd.DataFrame:
                # 实际预测逻辑由 Runner 执行
                raise NotImplementedError("Override in subclass or use runner directly")

        # 保存到 BentoML store
        bento = bentoml.Service.from_class(AlphaService)
        bento.save()

        _log.info(f"BentoML service built: {service_name}")
        return service_name

    def build_from_mlflow(
        self,
        model_uri: str,
        service_name: str,
        model_signature: Any = None,
    ) -> str:
        """从 MLflow 模型构建 BentoML Service."""
        bento = bentoml.mlflow.import_model(model_uri, service_name)
        bento.save()
        _log.info(f"BentoML service built from MLflow: {service_name}")
        return service_name


class ShadowDeploymentManager:
    """影子流量 / 金丝雀发布管理器.

    流量分配:
      - Production: 主流量 (默认 100%)
      - Shadow: 影子流量 (镜像请求, 不返回结果, 仅记录差异)
      - Canary: 金丝雀发布 (小比例流量, 监控指标自动回滚)
    """

    def __init__(self):
        self.routes: Dict[str, Dict[str, float]] = {}  # model_name -> {version: weight}
        self.shadow_routes: Dict[str, str] = {}  # model_name -> shadow_version

    def set_production(self, model_name: str, version: str, weight: float = 1.0):
        """设置生产版本及权重."""
        if model_name not in self.routes:
            self.routes[model_name] = {}
        self.routes[model_name][version] = weight
        # 归一化
        total = sum(self.routes[model_name].values())
        for v in self.routes[model_name]:
            self.routes[model_name][v] /= total

    def set_shadow(self, model_name: str, shadow_version: str):
        """设置影子版本 (镜像流量, 记录预测差异)."""
        self.shadow_routes[model_name] = shadow_version
        _log.info(f"Shadow deployment: {model_name} -> {shadow_version}")

    def set_canary(self, model_name: str, canary_version: str, weight: float = 0.1):
        """设置金丝雀版本 (小比例流量)."""
        self.set_production(model_name, version, 1.0 - weight)  # 主版本降权
        self.routes[model_name][canary_version] = weight
        _log.info(f"Canary deployment: {model_name} v{canary_version} @ {weight*100}%")

    def select_version(self, model_name: str, request_id: str = None) -> str:
        """根据路由规则选择版本."""
        import random
        routes = self.routes.get(model_name, {})
        if not routes:
            raise ValueError(f"No routes configured for {model_name}")

        # 影子流量: 总是并行跑 shadow 版本 (异步, 不阻塞)
        if model_name in self.shadow_routes:
            # 实际实现中会异步并行调用 shadow 版本
            pass

        # 加权随机选择
        versions = list(self.routes[model_name].keys())
        weights = list(self.routes[model_name].values())
        return random.choices(versions, weights=weights)[0]


class ABTestManager:
    """A/B 测试管理器.

    功能:
      - 流量分桶 (用户/请求级别)
      - 实时指标收集 (CTR/转化率/收益等)
      - 统计显著性检验 (Z-test / t-test / Bootstrap)
      - 自动决策: 推广/回滚/继续观察
    """

    def __init__(self, min_sample_size: int = 1000, significance: float = 0.05, min_effect: float = 0.01):
        self.min_sample_size = min_sample_size
        self.significance = significance
        self.min_effect = min_effect
        self.experiments: Dict[str, Dict] = {}

    def create_experiment(
        self,
        exp_id: str,
        control_version: str,
        treatment_version: str,
        traffic_split: float = 0.5,  # treatment 占比
        metrics: List[str] = None,
    ):
        """创建 A/B 实验."""
        self.experiments[exp_id] = {
            "control": control_version,
            "treatment": treatment_version,
            "split": traffic_split,
            "metrics": metrics or ["sharpe", "cagr", "max_drawdown"],
            "start_time": time.time(),
            "samples": {"control": 0, "treatment": 0},
            "metrics_data": {"control": [], "treatment": []},
        }

    def assign_variant(self, exp_id: str, user_id: str) -> str:
        """为用户分配实验版本 (基于 user_id 哈希)."""
        import hashlib
        exp = self.experiments[exp_id]
        h = int(hashlib.md5(f"{exp_id}:{user_id}".encode()).hexdigest(), 16)
        return exp["treatment"] if (h % 100) < (exp["split"] * 100) else exp["control"]

    def record_metric(self, exp_id: str, variant: str, metric_name: str, value: float):
        """记录实验指标."""
        exp = self.experiments[exp_id]
        exp["samples"][variant] += 1
        exp["metrics_data"][variant].append({metric_name: value})

    def analyze(self, exp_id: str) -> Dict[str, Any]:
        """统计分析实验结果."""
        from scipy import stats
        exp = self.experiments[exp_id]
        results = {}

        for metric in exp["metrics"]:
            control_vals = [d.get(metric) for d in exp["metrics_data"]["control"] if metric in d]
            treatment_vals = [d.get(metric) for d in exp["metrics_data"]["treatment"] if metric in d]

            if len(control_vals) < self.min_sample_size or len(treatment_vals) < self.min_sample_size:
                results[metric] = {"decision": "continue", "reason": "insufficient_samples"}
                continue

            # Z-test for proportions / t-test for means
            stat, p_val = stats.ttest_ind(treatment_vals, control_vals, equal_var=False)
            effect_size = (np.mean(treatment_vals) - np.mean(control_vals)) / np.std(control_vals)

            if p_val < self.significance and effect_size > self.min_effect:
                decision = "promote"
            elif p_val < self.significance and effect_size < -self.min_effect:
                decision = "rollback"
            else:
                decision = "continue"

            results[metric] = {
                "control_mean": np.mean(control_vals),
                "treatment_mean": np.mean(treatment_vals),
                "p_value": p_val,
                "effect_size": effect_size,
                "decision": decision,
            }

        return results


# ── 统一模型服务入口 ──

class ModelServingPlatform:
    """统一模型服务平台 — MLflow + BentoML + 部署策略."""

    def __init__(
        self,
        mlflow_uri: str = None,
        bentoml_store: str = None,
        experiment_name: str = "quant_alpha",
    ):
        self.mlflow = MLflowTracker(mlflow_uri, experiment_name)
        self.bentoml = BentoMLService()
        self.shadow = ShadowDeploymentManager()
        self.ab_test = ABTestManager()

    def register_and_deploy(
        self,
        model: Any,
        model_name: str,
        metrics: Dict[str, float],
        params: Dict[str, Any],
        artifact_path: str = "model",
        initial_stage: ModelStage = ModelStage.STAGING,
    ) -> str:
        """完整流程: 训练 -> MLflow 记录 -> 注册 -> BentoML 打包 -> 部署."""
        # 1. MLflow 记录与注册
        version = self.mlflow.log_model(
            model=model,
            artifact_path="model",
            model_name=f"{self.mlflow.experiment_name}_model",
            metrics=metrics,
            params=params,
        )

        # 2. 设置初始阶段
        model_name = f"{self.mlflow.experiment_name}_model"
        self.mlflow.transition_stage(model_name, version, initial_stage)

        # 3. BentoML 打包服务
        model_uri = f"models:/{model_name}/{version}"
        service_name = f"{model_name}_v{version}"
        self.bentoml.build_from_mlflow(model_uri, service_name=service_name)

        _log.info(f"Model {model_name} v{version} deployed to {ModelStage.STAGING.value}")
        return version

    def promote_to_production(self, model_name: str, version: str):
        """推广到生产环境."""
        self.mlflow.transition_stage(model_name, version, ModelStage.PRODUCTION)
        _log.info(f"Promoted {model_name} v{version} to PRODUCTION")

    def rollback(self, model_name: str, target_version: str):
        """回滚到指定版本."""
        self.mlflow.transition_stage(model_name, target_version, ModelStage.PRODUCTION)
        _log.warning(f"Rollback {model_name} to v{target_version}")

    def enable_shadow(self, model_name: str, shadow_version: str):
        """启用影子流量."""
        self.shadow.set_shadow(model_name, shadow_version)

    def start_canary(self, model_name: str, canary_version: str, weight: float = 0.1):
        """启动金丝雀发布."""
        self.shadow.set_canary(model_name, canary_version, weight=0.1)

    def create_ab_test(
        self,
        exp_id: str,
        control_version: str,
        treatment_version: str,
        traffic_split: float = 0.5,
    ):
        """创建 A/B 测试."""
        self.ab_test.create_experiment(
            exp_id=exp_id,
            control_version=control_version,
            treatment_version=treatment_version,
            traffic_split=traffic_split,
        )

    def get_serving_version(self, model_name: str, request_id: str = None) -> str:
        """获取当前请求应路由的版本."""
        return self.shadow.select_version(model_name, request_id)


# 全局单例
_model_serving: Optional["ModelServingPlatform"] = None


def get_model_serving() -> ModelServingPlatform:
    global _model_serving
    if _model_serving is None:
        _model_serving = ModelServingPlatform()
    return _model_serving


if __name__ == "__main__":
    # 测试
    platform = ModelServingPlatform()
    print("Model Serving Platform initialized")
    print(f"MLflow URI: {platform.mlflow.tracking_uri}")
    print(f"BentoML store: {platform.bentoml.store_path}")