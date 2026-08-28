#!/usr/bin/env python3
"""灰度发布快速演示 - 简化版"""
import asyncio
import sys
sys.path.insert(0, '.')

from quant.canary.canary import CanaryReleaseManager, CanaryConfig, RollbackTrigger

class MockBrokerManager:
    def get_broker(self, name): return None
    def get_connected_brokers(self): return ['sim']

class MockRouter:
    def select_broker(self, o): return None
    def select_algorithm(self, o, m):
        class Algo:
            def generate_slices(self, p, m): return []
        return Algo()

class Algo:
    def generate_slices(self, p, m): return []

class MockEngine:
    def __init__(self):
        self.broker_manager = None
        class Router:
            def select_broker(self, o): return None
            def select_algorithm(self, o, m):
                class Algo:
                    def generate_slices(self, p, m): return []
                return Algo()
        self.router = MockRouter()

    def get_active_orders(self):
        return []

class MockRouter:
    def select_broker(self, o): return None
    def select_algorithm(self, o, m):
        class Algo:
            def generate_slices(self, p, m): return []
        return Algo()

class Algo:
    def generate_slices(self, p, m): return []

class MockEngine:
    def __init__(self):
        self.broker_manager = None
        self.router = MockRouter()

    def get_active_orders(self):
        return []

class MockRiskManager:
    def __init__(self):
        self.circuit_breaker_manager = type("obj", (), {"_breakers": {}})()
    async def start(self): pass

async def main():
    print("=== 灰度发布演示 ===")

    from quant.canary.canary import CanaryReleaseManager, CanaryConfig, RollbackTrigger

    class MockBrokerManager:
        def get_broker(self, name): return None
        def get_connected_brokers(self): return ["sim"]

    class MockRouter:
        def select_broker(self, o): return None
        def select_algorithm(self, o, m):
            class Algo:
                def generate_slices(self, p, m): return []
            return Algo()

    class Algo:
        def generate_slices(self, p, m): return []

    class MockEngine:
        def __init__(self):
            self.broker_manager = None
            class Router:
                def select_broker(self, o): return None
                def select_algorithm(self, o, m):
                    class Algo:
                        def generate_slices(self, p, m): return []
                    return Algo()
            self.router = MockRouter()

        def get_active_orders(self):
            return []

    class MockRiskManager:
        def __init__(self):
            self.circuit_breaker_manager = type("obj", (), {"_breakers": {}})()
        async def start(self): pass

    from quant.canary.canary import CanaryReleaseManager, CanaryConfig, RollbackTrigger

    mgr = CanaryReleaseManager(
        broker_manager=MockBrokerManager(),
        execution_engine=MockEngine(),
        risk_manager=MockRiskManager(),
    )

    config = CanaryConfig(
        canary_id="demo",
        name="demo",
        strategy_ids=["demo"],
        account_ids=["demo"],
        traffic_split=0.01,
        phases=[
            {"traffic_split": 0.01, "duration_hours": 0.1, "name": "cold_start"},
            {"traffic_split": 0.05, "duration_hours": 0.1, "name": "small_scale"},
            {"traffic_split": 0.10, "duration_hours": 0.1, "name": "medium_scale"},
            {"traffic_split": 0.25, "duration_hours": 0.1, "name": "large_scale"},
            {"traffic_split": 0.50, "duration_hours": 0.1, "name": "half_release"},
            {"traffic_split": 1.00, "duration_hours": 0, "name": "full_release"},
        ],
        rollback_rules=[
            {"trigger": "pnl_drawdown", "threshold": -0.03, "description": "回撤3%"},
            {"trigger": "circuit_breaker", "threshold": 1, "description": "熔断器触发"},
        ],
    )
    canary_id = mgr.create_canary(config)
    print("Created canary:", canary_id)

    await mgr.start_canary("demo")
    print("Started canary")

    for i in range(3):
        await asyncio.sleep(1)
        status = mgr.get_canary_status("demo")
        print("Phase:", status["current_phase"], "Traffic:", f'{status["traffic_split"]:.1%}')

    await mgr.rollback_canary("demo")
    print("Rollback done")

    # Create a NEW canary for the second run
    config2 = CanaryConfig(
        canary_id="demo2",
        name="demo2",
        strategy_ids=["demo2"],
        account_ids=["demo2"],
        traffic_split=0.01,
        phases=[
            {"traffic_split": 0.01, "duration_hours": 0.1, "name": "cold_start"},
            {"traffic_split": 0.05, "duration_hours": 0.1, "name": "small_scale"},
            {"traffic_split": 0.10, "duration_hours": 0.1, "name": "medium_scale"},
            {"traffic_split": 0.25, "duration_hours": 0.1, "name": "large_scale"},
            {"traffic_split": 0.50, "duration_hours": 0.1, "name": "half_release"},
            {"traffic_split": 1.00, "duration_hours": 0, "name": "full_release"},
        ],
        rollback_rules=[
            {"trigger": "pnl_drawdown", "threshold": -0.03, "description": "回撤3%"},
            {"trigger": "circuit_breaker", "threshold": 1, "description": "熔断器触发"},
        ],
    )
    canary_id2 = mgr.create_canary(config2)
    print("Created canary2:", canary_id2)

    await mgr.start_canary("demo2")
    print("Started canary2")

    for i in range(3):
        await asyncio.sleep(1)
        status = mgr.get_canary_status("demo2")
        print("Phase:", status["current_phase"], "Traffic:", f'{status["traffic_split"]:.1%}')

    await mgr.complete_canary("demo2")
    print("Canary release completed!")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
