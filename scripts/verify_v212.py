#!/usr/bin/env python3
import json, sys, urllib.request

raw = urllib.request.urlopen("http://localhost:8521/api/state").read()
d = json.loads(raw)
sigs = d.get("data", {}).get("signals", [])
print(f"候选数: {len(sigs)}")
for s in sigs[:5]:
    r = s.get("reason", "")
    parts = r.split(", ")
    shown = ", ".join(parts[:2])
    more = f" +{len(parts)-2} more" if len(parts) > 2 else ""
    en = s.get("exec_note", "")
    en_str = f" [{en}]" if en else ""
    print(f"{s['symbol']} {s['name']} 得分={s['score']:.2f}{en_str}")
    print(f"  信号: {shown}{more}")
