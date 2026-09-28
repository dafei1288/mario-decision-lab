"""smb_promptwrap.py — 给「宏决策座位」的模型加**提示词变体**（不改 nes_play/smb_macro/probe_laya）

原理：座位把 {"state_text","candidates"} 用一行 JSON 发给我们（`--adapter` 的子进程），
我们把 state_text **包一层提示词变体**再转给真正的模型进程，把模型的回答原样转回 stdout。
这样 prompt 工程完全在本文件里做，执行层与模型侧一行不动。

用法（配合时序座位）：
  python nes_play.py --rom "/roms/Super Mario Bros. (Japan, USA).nes" \
    --macro --macro-mode timing --macro-brain model --cycles 300 \
    --adapter "python smb_promptwrap.py --variant fewshot --model-cmd \"<LV> probe_laya.py --stdin-loop\"" \
    --tag prompt-fewshot

变体：`raw`(对照，原样转发) / `fewshot`(样例+判据表+反锁死) / `binary`(只回答 jump 还是 go + 紧迫感)
       / `table`(只给阈值表，不给样例)
每步会把 (变体, 状态摘要, 模型选择) 追加写到 runs/promptwrap-log.jsonl 供量化。
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

LOG = Path("runs/promptwrap-log.jsonl")

RULES = """
=== 你怎么决定（照着查表，别凭感觉）===
* 前方有坑（pit_ahead_px 是数字）：**≤ 90px 就必须 jump**（一步 6 帧能前进约 15px，犹豫一步就掉坑）。
* 前方有障碍/生物（creature 或 obstacle 在 60px 内）：**jump**（跳过或踩掉）。
* 脚下不是地面（on_ground=false）：让物理飞完，选 go（别在空中乱跳浪费机会）。
* 以上都没有：go（继续冲）。
* **不要因为你上一步选了同样的动作就继续选它**——每一步都重新看数字，数字变了选择就要变。
"""

FEWSHOT = """
=== 样例（状态 → 正确动作）===
1) pit_ahead_px=30, obstacle=none, on_ground=true          → jump
2) pit_ahead_px=none, obstacle=none, on_ground=true        → go
3) pit_ahead_px=none, creature at 150px, on_ground=true    → jump
4) pit_ahead_px=12, on_ground=true                         → jump
5) pit_ahead_px=none, obstacle=12px, on_ground=true        → jump
6) on_ground=false                                         → go
"""

BINARY = """
=== 只回答一件事：现在跳不跳？===
* 如果 pit_ahead_px ≤ 90px，或者 60px 内出现生物/障碍 → 选 `jump`（**再往前 6 帧就可能掉坑/撞死**）。
* 否则选 `go`。
* 不要重复上一步的选择作为理由；只看当前数字。
"""

TABLE = """
=== 判据表（照抄）===
pit_ahead_px ≤ 90 → jump ；obstacle_px ≤ 60 → jump ；生物在 60px 内 → jump ；on_ground=false → go ；都没有 → go
"""

SCAFFOLD = """
=== 输出格式（必须）===
先输出一行判据：`pit=<值> obstacle=<值> on_ground=<值> => <结论>`，然后**只**回答一个动作名：
`jump` 或 `go`。
"""

HARD = """
=== 硬约束 ===
If your previous answer was jump and there is no danger (pit none, obstacle none, no creature within 60px),
you MUST answer go. Repeating jump without danger is a mistake.
"""

BLOCKS = {"raw": "", "rules": RULES, "fewshot": RULES + FEWSHOT,
          "binary": RULES + BINARY, "table": RULES + TABLE,
          "scaffold": RULES + BINARY + SCAFFOLD,
          "scaffold_hard": RULES + BINARY + SCAFFOLD + HARD,
          "binary_front": RULES + BINARY + HARD,
          "guard": RULES + BINARY,           # binary + **机械约束**（消融用，不算模型决策）
          "two_step": RULES + BINARY,        # 每步两次调用：先判危险，再选动作
          "split": ""}                       # **分工架构**：判断层=SmbPolicy 阈值，动作层=模型在安全集内选

SPLIT_NOTE = ("\n\n一个已验证的检测器已经判断过当前局面，并给出了**安全动作集**；"
              "你只需要在这个集合里挑一个。不要挑集合外的动作。\n")

Q1 = ("\n\nQuestion A: is there anything dangerous ahead right now "
      "(a pit within 90px, or an obstacle/creature within 60px ahead)? "
      "Answer with exactly one word: yes or no.\n")
Q1_CAND = [{"id": "yes", "text": "yes, there is danger ahead"},
           {"id": "no", "text": "no, nothing dangerous ahead"}]
TWO_CAND = [{"id": "jump", "text": "起跳（现在按 A）"}, {"id": "go", "text": "保持向右跑（不按 A）"}]


def normalize(choice, two: bool) -> str:
    """把模型可能回的自由文本归一到座位能识别的动作 id（jump/go）。

    这不是"作弊"：座位对参照大脑也是用 KEYMAP 把键列表映射成 id 的；这里只是把**同一层**的
    映射搬到 prompt 适配器里，并如实记录映射前后的原文（日志里有 raw_choice）。
    """
    if not two:
        return choice
    c = (choice or "").strip().lower()
    if c in ("jump", "go", "back"):
        return c
    if any(k in c for k in ("jump", "跳")) and not any(k in c for k in ("不跳", "no jump")):
        return "jump"
    if any(k in c for k in ("go", "不跳", "继续", "跑", "keep")):
        return "go"
    return choice


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="raw")
    ap.add_argument("--model-cmd", required=True, help="真正的模型进程（stdin-loop）")
    ap.add_argument("--log", default=str(LOG))
    a = ap.parse_args()
    block = BLOCKS.get(a.variant, "")
    log = Path(a.log)
    log.parent.mkdir(parents=True, exist_ok=True)

    child = subprocess.Popen(shlex.split(a.model_cmd), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, encoding="utf-8", bufsize=1)
    n = 0
    last_choice = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = payload.get("state_text", "")
        payload["state_text"] = (text + "\n" + block) if block else text
        two = a.variant in ("binary", "scaffold", "scaffold_hard", "binary_front",
                            "guard", "two_step", "split")
        if a.variant == "binary_front":
            payload["state_text"] = block + "\n" + text
        if two:
            payload["candidates"] = TWO_CAND          # **payload 里就只剩 2 项**（Main 的①）
        q1_answer = None
        split_pick = None
        if a.variant == "split":
            # ---- 判断层：照 SmbPolicy 的判据（pit 29 / obstacle 40 / 生物 40px；一步 6 帧≈15px）----
            def _n(pat):
                m = re.search(pat, text)
                return int(m.group(1)) if m and m.group(1).isdigit() else None
            pit_v = _n(r"pit ahead: (\d+)px")
            obs_v = _n(r"obstacle ahead: (\d+)px")
            cre = [(k, int(v)) for k, v in re.findall(r"(goomba|koopa|unknown|mushroom)\s+([+-]\d+)px", text)]
            ground = "on_ground=True" in text
            danger = ((pit_v is not None and pit_v <= 29) or (obs_v is not None and obs_v <= 40)
                      or any(0 < d <= 40 for _k, d in cre))
            intent = "jump" if (danger and ground) else "run"
            cands = payload.get("candidates") or []
            def pick(keys):
                out = []
                for c in cands:
                    s_ = (str(c.get("id", "")) + " " + str(c.get("text", ""))).lower()
                    if any(k in s_ for k in keys) and c not in out:
                        out.append(c)
                return out
            safe = (pick(["jump", "跳"]) + pick(["back", "退"])) if intent == "jump" \
                else (pick(["sprint", "冲"]) + pick(["walk", "走"]) + pick(["go", "跑", "right"]))
            if not safe:
                safe = cands
            split_pick = {"intent": intent, "danger": danger, "safe_set": [c["id"] for c in safe],
                          "n_safe": len(safe)}
            if len(safe) == 1:                      # 安全集只有一项 ⇒ 不必问模型（省一次调用）
                got = {"choice": safe[0]["id"], "probs": {}}
                raw_choice = got["choice"]
                sys.stdout.write(json.dumps(got, ensure_ascii=False) + "\n")
                sys.stdout.flush()
                n += 1
                if split_pick is not None:
                    split_pick["chosen"] = got["choice"]
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"step": n, "variant": a.variant, "ms": 0,
                                        "choice": got["choice"], "reason": "safe_set_singleton",
                                        **split_pick, "state": text[:200]}, ensure_ascii=False) + "\n")
                continue
            payload["candidates"] = safe
            payload["state_text"] = text + SPLIT_NOTE
        if a.variant == "two_step":
            # 第一步：只判"有没有危险"
            q1_payload = {"state_text": text + Q1, "candidates": Q1_CAND}
            child.stdin.write(json.dumps(q1_payload, ensure_ascii=False) + "\n")
            child.stdin.flush()
            r1 = ""
            while True:
                r1 = (child.stdout.readline() or "").strip()
                if not r1:
                    continue
                try:
                    g1 = json.loads(r1)
                except json.JSONDecodeError:
                    continue
                if "choice" in g1:
                    break
            c1 = (g1.get("choice") or "").strip().lower()
            q1_answer = "yes" if "yes" in c1 or "是" in c1 else "no"
            # 第二步：拿着第一步的结论决定动作
            payload["state_text"] = (text + f"\n\nQuestion A was answered: {q1_answer}.\n"
                                            "Question B: given that, answer with exactly one action id: "
                                            "jump or go.\n")
        child.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        child.stdin.flush()
        t0 = time.time()
        while True:
            rep = (child.stdout.readline() or "").strip()
            if not rep:
                continue
            try:
                got = json.loads(rep)
            except json.JSONDecodeError:
                continue
            if "choice" in got:
                break
        raw_choice = got.get("choice")
        got["choice"] = normalize(raw_choice, two)
        forced = False
        if a.variant == "guard" and not truth_danger and got["choice"] == "jump" and last_choice == "jump":
            got["choice"], forced = "go", True          # **机械约束**（不是模型决策）
        last_choice = got["choice"]
        got["raw_choice"] = raw_choice
        sys.stdout.write(json.dumps(got, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        n += 1
        def grab(pat, cast=int):
            m = re.search(pat, text)
            if not m:
                return None
            v = m.group(1)
            if v in ("none", "None", "none in view"):
                return None
            try:
                return cast(v)
            except (TypeError, ValueError):
                return None

        # Main 已把生物改成**相对距离**：`goomba +38px ahead, on the ground`（正数=前方）
        cre = [[k, int(v)] for k, v in re.findall(r"(goomba|koopa|unknown|mushroom)\s+([+-]\d+)px", text)]
        truth_danger = ((grab(r"pit ahead: (\d+)px") or 9999) <= 90
                        or (grab(r"obstacle ahead: (\d+)px") or 9999) <= 60
                        or any(0 < d <= 60 for _k, d in cre))
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"step": n, "variant": a.variant, "ms": round((time.time() - t0) * 1000),
                                "choice": got.get("choice"), "raw_choice": raw_choice,
                                "pit": grab(r"pit ahead: (\d+)px"),
                                "obstacle": grab(r"obstacle ahead: (\d+)px"),
                                "on_ground": ("True" in (re.search(r"on_ground=(\w+)", text).group(1)
                                              if re.search(r"on_ground=(\w+)", text) else "False")),
                                "level_x": grab(r"level_x=(\d+)"),
                                "mario_screen_x": grab(r"screen_x=(\d+)"),
                                "lives": grab(r"lives=(\d+)"),
                                "time_left": grab(r"time_left=(\d+)"),
                                "creatures": cre, "truth_danger": truth_danger, "forced": forced,
                                "q1_answer": q1_answer, "split": split_pick,
                                "state": text[:300]}, ensure_ascii=False) + "\n")
    child.stdin.close()


if __name__ == "__main__":
    main()
