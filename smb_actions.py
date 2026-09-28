"""smb_actions.py — 「时序座位」的动作词汇（真实玩家的决策面；前进由马达层承担，模型只做修正）

表结构沿用 `smb_macro.actions_for()`：id -> (键集合, 保持帧数, 中文短标签, 英文一句描述)
额外给一个 `kind` 说明"这动作改的是哪一维"（时间/位置/竖直/武器），方便写状态机。

按下 → 松开的语义由座位执行器负责（`nes_play.ButtonPulse`：一次决策=一次按键脉冲，
先 `__set([],2)` 松全部键再 `__set(键, 时长)`；方向键"常按"由**连续选择同一动作**表达）。

关于"踩敌人 / 吃蘑菇 / 吃花 / 顶金币"：这些**不是按键动作**，是状态后果 —— 由
`smb_state.read_state()` 的字段体现，例如
  * 可踩：`abs(mario.screen_x_px - enemy.screen_x) <= 12 and enemy.screen_y - mario.screen_y >= 18`
  * 可吃蘑菇：`enemy.tile` 是蘑菇精灵且与马里奥距离 ≤ 12px
  * 可顶金币：`ahead.blocks` 里 kind=="question" 且 `screen_x - mario.screen_x in [-6, 14]`
"""
from __future__ import annotations

# id -> (keys, hold_frames, 中文, english, kind)
TIMING_ACTIONS: dict[str, tuple[list[str], int, str, str, str]] = {
    "sprint":     (["RIGHT", "B"], 6, "冲刺",   "sprint right (hold right+B)", "locomotion"),
    "walk":       (["RIGHT"], 6, "走",          "walk right without running (right only)", "locomotion"),
    "small_jump": (["RIGHT", "A", "B"], 8, "小跳", "small hop (~8 frames of A): short/low, for stomping", "vertical"),
    "big_jump":   (["RIGHT", "A", "B"], 30, "大跳", "big jump (~30 frames of A): full height, clears 3-tile pipes", "vertical"),
    "crouch":     (["DOWN"], 12, "蹲·下",       "crouch / press down (also enters pipes when big)", "vertical"),
    "back":       (["LEFT"], 6, "后退",         "step back to the left", "locomotion"),
    "stop":       ([], 6, "停",                "release everything and wait a beat", "timing"),
    "fireball":   (["B"], 8, "发射火球",        "throw a fireball (single B tap; needs fire power-up)", "weapon"),
}


def actions_for(mode: str = "timing") -> dict:
    """兼容 smb_macro.actions_for() 的结构；本模块只提供时序座位这一套。"""
    return TIMING_ACTIONS


def key_list(action_id: str) -> list[str]:
    return list(TIMING_ACTIONS[action_id][0])


def hold_frames(action_id: str) -> int:
    return TIMING_ACTIONS[action_id][1]


def label(action_id: str) -> str:
    return TIMING_ACTIONS[action_id][2]


def describe(action_id: str) -> str:
    return TIMING_ACTIONS[action_id][3]
