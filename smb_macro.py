"""smb_macro.py — 把「玩马里奥」变成**一系列决策问题**：语义宏动作 + 可读状态

与之前 `smb_fallback.py`（脚本兜底）的区别：
  * 兜底脚本是「反应式控制器」——它自己决定一切；
  * 这里把**决策权交给模型**：每 N 帧问一次模型「现在该做哪个宏动作」，
    脚本只负责**执行**（把选中的键按住若干帧）＝模型是策略、脚本是马达。

动作集（语义宏，模型看到的是中文/英文描述，执行时映射到按键 + 按住帧数）：
    向前跑 / 向前跳 / 后退 / 等待 / 原地跳（踩菇顶砖）

状态（喂给模型的是**结构化特征**，不是原始像素）：
    已推进距离 x、是否在地面、前方最近的坑/障碍距离（像素）、最近可收集物距离、上一步做了什么
    —— 特征由画面 + RAM 现算，模型不需要自己看图。
"""
from __future__ import annotations

import json
from pathlib import Path

GROUND = (0xB1, 0x54, 0x00)
SKY = (0x81, 0x79, 0xFF)

# 宏动作：id -> (按键, 按住帧数, 中文, 一句话描述)
MACRO_ACTIONS: dict[str, tuple[list[str], int, str, str]] = {
    "run_fwd":   (["RIGHT", "B"], 20, "向前跑",   "run forward at full speed"),
    "jump_fwd":  (["RIGHT", "A", "B"], 14, "向前跳", "run and jump forward"),
    "back":      (["LEFT"], 12, "后退",       "step back to the left"),
    "wait":      ([], 12, "等待",             "do nothing for a moment"),
    "jump_here": (["A"], 10, "原地跳",        "jump straight up in place"),
}


# 「时序座位」：马达层一直保持向前（除后退/停），模型的决策只负责**时序**：
# 什么时候跳、跳多满、什么时候停、什么时候退。
# 每条动作都是**一次原子按压**（执行层先松键再按，见 nes_play 宏分支）：
#   * 所以"跳"的按住帧数＝这条动作的**词汇含义**（30 帧＝满跳；6 帧只有小跳）
#   * 一个只会反复选"跳"的大脑 ⇒ 一串满跳；会读坑/障碍的大脑 ⇒ 用满跳跨过去
# 关键教训（2026-09-22 实测）：若"按住 A"跨决策保持，常量翻跳的大脑会只跳一次（边沿触发），
# 画面看着像"链路坏了/模型冻住了"，其实是座位语义给的假能力 —— 座位必须让两个大脑都只能表达
# 它真正选的东西。
TIMING_ACTIONS: dict[str, tuple[list[str], int, str, str]] = {
    # 只保留"时序修正"：**前进由马达层默认承担**（go/jump 都含 RIGHT+B），跳是修正而非驱动因素。
    # 删掉 idle / jump_here（它们会主动放弃前进 —— 用户 2026-09-22 指出：赢法的默认就是向前走，
    # 跳不是驱动因素；把"向前"和"跳"混在同一个动作里会让评分与对照都失真）。
    "go":   (["RIGHT", "B"], 6, "继续冲（不加按 A）", "keep sprinting right (no jump)"),
    "jump": (["RIGHT", "A", "B"], 6, "起跳（加按 A）", "jump right now, while sprinting"),
    "back": (["LEFT", "B"], 6, "退后（按左）", "back off to the left"),
}


def actions_for(mode: str) -> dict:
    return TIMING_ACTIONS if mode == "timing" else MACRO_ACTIONS


def _at(px, w, x, y):
    i = (y * w + x) * 3
    return (px[i], px[i + 1], px[i + 2])


def ground_row(px, w, h) -> int:
    """地面带的**表面行**（＝自下而上连续地面色区段的最上一行）。

    ⚠ 修过：旧实现返回"自下而上第一行地面色占比>40%"，那是**地面带内部**的行（实测 231），
    于是 ① 坑检测（在"地面行"找天空色）永远不触发 ② 障碍扫描的行全落在地面砖块里、
    整行都不是天空 ⇒ 恒报"障碍 22px"（喂给模型的状态是常量，2026-09-22 用户点破）。"""
    def is_ground(y: int) -> bool:
        cnt = sum(1 for x in range(0, w, 4) if _at(px, w, x, y) == GROUND)
        return cnt > (w // 4) * 0.4

    top = None
    for y in range(h - 4, 150, -1):
        if is_ground(y):
            top = y
        elif top is not None:
            break
    return top if top is not None else 208


def ground_band(px, w, h, anchor_x: int) -> int:
    """地面带的**表面行**，锚定在**马里奥附近**的列窗口（整幅宽度不行：左边常有天空/城堡）。

    做法：在 x∈[anchor-40, anchor+120] 窗口里逐行统计地面色占比，取"连续地面段的最上一行"。"""
    x0, x1 = max(0, anchor_x - 40), min(w, anchor_x + 120)

    def frac(y: int) -> float:
        tot = 0
        hit = 0
        for x in range(x0, x1, 2):
            tot += 1
            if _at(px, w, x, y) == GROUND:
                hit += 1
        return hit / max(1, tot)

    top = None
    for y in range(h - 4, 150, -1):
        if frac(y) > 0.35:
            top = y
        elif top is not None:
            break
    return top if top is not None else 224


def features(stats: dict, mario_x: int, history: list[str], progress: int) -> dict:
    """从**一帧 + RAM** 算出结构化状态（模型只看这些数字/短语，不看像素）。

    ⚠ 只用**可靠**的量：坑距离（地面带里出现天空色）。原先的"障碍/敌人距离"用"地面上方非天空像素"
    判定 ⇒ SMB 1-1 的背景灌木/山丘本身就是非天空色，**恒报 22px**，等于给模型一个常量状态
    （2026-09-22 用户点破：状态恒定 ⇒ 动作恒定，看着像"模型只会一个动作"）。删掉，别再造假特征。
    """
    px, w, h = stats["pixels"], stats["w"], stats["h"]
    gy = ground_band(px, w, h, mario_x)

    # 坑：在地面带**内部**（表面下方 6px）扫天空色 —— 坑是贯穿地面的缺口
    probe_y = min(h - 3, gy + 6)
    pit_d = None
    for x in range(mario_x + 22, min(w, mario_x + 140)):
        if _at(px, w, x, probe_y) == SKY:
            pit_d = x - mario_x
            break

    on_ground = _at(px, w, min(w - 1, mario_x), gy - 2) == SKY or \
                _at(px, w, min(w - 1, mario_x), gy - 1) == SKY
    return {"progress_px": progress, "mario_screen_x": mario_x, "ground_row": gy,
            "on_ground_hint": bool(on_ground), "pit_ahead_px": pit_d,
            "last_actions": history[-3:]}


def state_text(f: dict, frame: int) -> str:
    pit = f"{f['pit_ahead_px']}px ahead" if f["pit_ahead_px"] is not None else "none in view"
    return (
        "You are the high-level controller of Mario in Super Mario Bros level 1-1. "
        "Each decision lasts a fixed number of frames; the low-level timing is handled for you.\n"
        f"frame={frame}\n"
        f"distance already advanced this level: {f['progress_px']} px\n"
        f"pit ahead: {pit}\n"
        f"mario roughly on the ground: {f['on_ground_hint']}\n"
        f"your last actions: {f['last_actions'] or '(none yet)'}\n"
        "Choose the single best action for the next moment. Options: "
        "向前跑 (run forward), 向前跳 (run and jump forward), 后退 (step back), 等待 (wait), 原地跳 (jump in place).\n"
        "question: which action should be taken now to make progress toward the flagpole?\n"
    )


def timing_text(f: dict, frame: int) -> str:
    """时序座位的状态文本：只说"现在该不该跳"，不提供"往哪走"这种送分选项。"""
    pit = f"{f['pit_ahead_px']}px ahead" if f["pit_ahead_px"] is not None else "none in view"
    return (
        "You are the TIMING controller of Mario in Super Mario Bros 1-1. Mario is sprinting to the right "
        "no matter what; your only job is to decide the next few frames of input. The only thing that can "
        "kill him on this stretch is a pit, and jumping too late or too early drops him in.\n"
        f"frame={frame}\n"
        f"distance advanced: {f['progress_px']} px\n"
        f"pit ahead: {pit}\n"
        f"your last decisions: {f['last_actions'] or '(none yet)'}\n"
        "Options: 继续冲 (keep sprinting forward, no jump), 起跳 (jump right now), "
        "退后 (back off to the left).\n"
        "question: what should be done in the next 8 frames?\n"
    )


def candidates() -> list[dict]:
    return [{"id": k, "text": v[3]} for k, v in MACRO_ACTIONS.items()]
