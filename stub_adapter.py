"""stub_adapter.py — 单发适配器协议的最小实现，用于**自检 harness 而不依赖任何模型**。

协议（模型适配器必须一致）：
    stdin : {"state_text": "...", "candidates": [{"id","text"}, ...]}
    stdout: {"choice": "<action id>", "probs": {"<action>": p, ...}}   ← 取末行 JSON

用法：
    python rollout.py --policy model --adapter "python stub_adapter.py --pick right_jump" --levels 6

`--pick` 固定输出某个动作（默认走一个小启发式），用来验证：
  ① 管道通（进程起得来、JSON 能往返）② 决策生效（换成 always 'right' 应当输、换成 oracle 的等价物应当赢）
"""
from __future__ import annotations

import argparse
import json
import sys

ACTIONS = ("right", "left", "jump", "right_jump", "wait")

ap = argparse.ArgumentParser()
ap.add_argument("--pick", default="", help="固定返回该动作；留空则按 state_text 里 @ / E / 坑做极简启发")
a = ap.parse_args()

payload = json.loads(sys.stdin.read() or "{}")
state_text = payload.get("state_text", "")
choice = a.pick
if not choice:
    # 只认「纯地图行」（字符集恰为 .#=@EG 的行），否则会把头部说明行当成地图
    rows = [l for l in state_text.splitlines() if l and set(l) <= set(".#=@EG")]
    prow = next((i for i, l in enumerate(rows) if "@" in l), None)
    if prow is None:                      # 玩家已坠出画面 → 死局，随便给一个
        choice = "right"
    else:
        row = rows[prow]
        px = row.find("@")
        ahead = row[px + 1:px + 3]
        ground = rows[prow + 1] if prow + 1 < len(rows) else ""
        pit_ahead = len(ground) > px and "." in ground[px:px + 3]
        choice = "right_jump" if ("E" in ahead or pit_ahead) else "right"

print(json.dumps({"choice": choice, "probs": {k: (1.0 if k == choice else 0.0) for k in ACTIONS}},
                 ensure_ascii=False))
