#!/usr/bin/env python3
"""
直接通过 Dagster API 启动所有 schedules。
"""
import os
import sys

os.environ.setdefault("DAGSTER_HOME", "/tmp/dagster_home")
os.environ.setdefault("QUANT_ORCHESTRATOR", "dagster")
sys.path.insert(0, "/Users/mariusto/project/quant")

from dagster import DagsterInstance
from dagster._core.scheduler.instigation import InstigatorStatus

def main():
    instance = DagsterInstance.get()
    
    # 获取所有 schedules
    states = list(instance.schedule_storage.all_instigator_state())
    
    schedules = [s for s in states if 'SCHEDULE' in str(s.instigator_type)]
    print(f"Found {len(schedules)} schedules")
    
    for s in schedules:
        status_str = str(s.status)
        if 'DECLARED_IN_CODE' in status_str:
            print(f"  Starting {s.name}...")
            try:
                # 更新状态为 RUNNING
                new_state = s.with_status(InstigatorStatus.RUNNING)
                instance.schedule_storage.update_instigator_state(new_state)
                print(f"    ✅ {s.name} started")
            except Exception as e:
                print(f"    ❌ {s.name} failed: {e}")
        else:
            print(f"  ⏭ {s.name} already {status_str}")
    
    # 验证
    print()
    print("Verifying...")
    states = list(instance.schedule_storage.all_instigator_state())
    for s in states:
        if 'SCHEDULE' in str(s.instigator_type):
            print(f"  {s.name}: {s.status}")

if __name__ == "__main__":
    main()
