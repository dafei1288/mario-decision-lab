"""random_adapter.py — 均匀随机策略适配器（单发协议），用来给单点指标一个**实测**基线

确定性：以 `state_text` 的 md5 作为种子 → 同一状态永远给同一个答案（可复跑、可审计），
但跨状态均匀分布，等价于「不看状态的均匀随机」。
"""
from __future__ import annotations

import hashlib
import json
import random
import sys

ACTIONS = ("right", "left", "jump", "right_jump", "wait")
payload = json.loads(sys.stdin.read() or "{}")
seed = int(hashlib.md5(payload.get("state_text", "").encode("utf-8")).hexdigest()[:8], 16)
choice = random.Random(seed).choice(ACTIONS)
print(json.dumps({"choice": choice, "probs": {a: (1.0 if a == choice else 0.0) for a in ACTIONS}},
                 ensure_ascii=False))
