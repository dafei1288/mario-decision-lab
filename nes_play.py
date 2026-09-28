"""nes_play.py — 用**真实第三方游戏**跑四步闭环：截屏 → 分析 → 决策 → 执行

与 `play.py`（自研环境）的区别：这里的游戏是外面的真 ROM（默认用开源 NES 平台游戏 Nova the Squirrel，
GPL-3.0；把你的马里奥/魂斗罗 ROM 放进 `roms/` 换 `--rom` 即可），模拟器是 jsnes（Apache-2.0），
画面是真 NES 帧缓冲（256×240）。

四步落点：
  ① 截屏   jsnes 帧缓冲 → canvas → PNG 落盘（每秒 60 帧由模拟器驱动，我们按「决策周期」取样）
  ② 分析   **真游戏没有语义内存**——所以这里有两条通道，脚本里两条都给：
           (a) 画面统计：亮度均值 + 主要颜色占比 + 与上一帧的变化像素占比（真实可用的廉价感知）
           (b) 直读 RAM：`nes.mmap`（需要游戏的内存地图才有语义；Nova 是开源的，可去它源码里查）
           本脚本默认把 (a) 喂给模型，并把 (b) 一并记录用于对照。
  ③ 决策  5 个候选＝真手柄动作（不出手 / 右 / 左 / 右+A 跳 / 右+B 跑）
  ④ 执行  按住选中的键 N 帧（真实手柄必须按住若干帧），再进下一轮

用法：
  python nes_play.py --rom /roms/nova.nes --dry-run --cycles 5           # 不给模型，验证"操控真的生效"
  python nes_play.py --rom /roms/nova.nes --adapter "<模型命令>" --cycles 20 --tag nova-laya
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import zlib, struct
from urllib.parse import quote

BASE = __import__("os").environ.get("SMB_BASE","http://127.0.0.1:8791")

# 贴在手柄上的短标签（面板里的候选列表仍用模型自己的长描述）
ACTION_SHORT = {"none": "不出手", "start": "START", "a": "跳", "b": "跑",
                "right": "向右走", "left": "向左走", "right_a": "向右跳", "right_b": "向右跑",
                "right_ab": "向右跑跳", "down": "下蹲"}
# 每个动作的**按压时长（游戏帧）＝决策节奏**。粒度必须**细于跳的飞行时间**：
# 一步 32 帧（≈80px）时，一次满跳飞 100–140px 才落地 ⇒ 落点在决策回路之外（躲开这只蘑菇兵
# 正好落到下一只身上），任何策略都过不去（2026-09-22 实测：脚本大脑在新座位重开 3 次）。
# 所以：**决策步长 6 帧（≈15px），"跳"的保持时长交给 ButtonPulse 的有界窗口（30 帧＝满跳）**。
ACTION_HOLD = {k: 6 for k in ("none", "start", "a", "b", "right", "left", "right_a", "right_b",
                              "right_ab", "down")}


class ButtonPulse:
    """把"决策"翻译成**按键脉冲**的执行器：边沿正确 + 方向可常按 + 跳有界。

    铁律：SMB 的跳是**边沿触发** —— A 不松开就永远不会有第二跳。所以：
      * 方向键：每个决策都按一次（连续决策同一方向＝一直按着，观感就是按住）
      * 跳键：**A 最多连着按住 jump_window 帧**；超窗后再选跳＝先松 2 帧再按（新边沿）
    这样既不会出现"A 常亮却不跳"（旧 bug），也不会出现"跳比一步还长、落点失控"（粗粒度座位）。"""

    def __init__(self, page, jump_window: int = 30, release_frames: int = 2):
        self.page, self.window, self.rel = page, jump_window, release_frames
        self.a_until = -1                      # A 保持到第几帧（-1＝没在跳）

    def press(self, btns, n: int, shot=None) -> int:
        fc = self.page.evaluate("() => window.__frameCount()")
        if "A" in btns and fc >= self.a_until:
            if self.rel > 0:      # 预松帧数可设 0：某些策略（脚本判据）依赖**连续按键**的时序
                self.page.evaluate("([b, n]) => window.__set(b, n)", [[], self.rel])
            self.a_until = fc + self.rel + self.window
        if shot is None:
            return self.page.evaluate("([b, n]) => window.__set(b, n)", [list(btns), n])
        # 逐帧模式：一帧一帧走（视频按游戏帧拼，模型延迟不影响播放速度）
        self.page.evaluate("([b]) => window.__set(b, 0)", [list(btns)])
        for _ in range(n):
            self.page.evaluate("() => window.__step1()")
            shot()
        return self.page.evaluate("() => window.__frameCount()")


ACTIONS = {
    "none": ([], "do nothing"),
    "start": (["START"], "press START (confirm / begin)"),
    "a": (["A"], "press A (jump in place)"),
    "b": (["B"], "press B (run / fire in place)"),
    "right": (["RIGHT"], "press right (walk right)"),
    "left": (["LEFT"], "press left (walk left)"),
    "right_a": (["RIGHT", "A"], "press right + A (jump while moving right)"),
    "right_b": (["RIGHT", "B"], "press right + B (run while moving right)"),
    "down": (["DOWN"], "press down (crouch / enter door)"),
    "right_ab": (["RIGHT", "A", "B"], "press right + A + B (run and jump)"),
}


# ----------------------------------------------------------------- 极小 PNG 读（用于画面统计）

def png_stats(png_bytes: bytes) -> dict:
    """解 PNG → 亮度均值 / 主要颜色占比 / 原始 RGB（供帧间差分）。纯标准库。

    ⚠ 必须按 IHDR 的 color type 定 stride：Chrome `canvas.toDataURL` 出的是 **type 6（RGBA，4B/px）**，
    而自研渲染器写的是 type 2（RGB，3B/px）。按 3B 硬解 RGBA 图会得到"96.8% 全黑"的假统计——
    这等于把垃圾喂给模型（本目录踩过）。
    """
    data = png_bytes
    pos, idat, w, h, ctype = 8, b"", None, None, None
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            w, h, _depth, ctype = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + ln
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(idat)
    stride = w * bpp
    rows, prev, p = [], bytearray(stride), 0
    for _ in range(h):
        f = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]; c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        rows.append(line); prev = line

    rgb = bytearray()
    for line in rows:                              # 统一压成 RGB，丢掉 alpha
        if bpp == 3:
            rgb += line
        else:
            for i in range(0, len(line), bpp):
                rgb += line[i:i + 3]
    n = w * h
    lum = sum(rgb[i * 3] + rgb[i * 3 + 1] + rgb[i * 3 + 2] for i in range(n)) / (3 * n)
    from collections import Counter
    common = Counter(tuple(rgb[i * 3:i * 3 + 3]) for i in range(0, n, 7)).most_common(3)
    return {"w": w, "h": h, "luma_mean": round(lum, 1),
            "top_colors": [(f"#{r:02x}{g:02x}{b:02x}", round(c / (n / 7), 3)) for (r, g, b), c in common],
            "pixels": bytes(rgb)}


def changed_ratio(a: bytes, b: bytes) -> float:
    if len(a) != len(b):
        return 1.0
    diff = sum(1 for i in range(0, len(a), 3) if a[i:i + 3] != b[i:i + 3])
    return round(diff / (len(a) / 3), 4)


def state_text(frame_no: int, stats: dict, history: list[str], changed: float) -> str:
    tops = ", ".join(f"{c}({p})" for c, p in stats["top_colors"])
    return (
        "A real NES game is running. You control it with button presses; each decision you make is held "
        f"for several frames. Screen is 256x240.\n"
        f"frame={frame_no} luma_mean={stats['luma_mean']} dominant_colors={tops} "
        f"pixels_changed_since_last_frame={changed}\n"
        f"last actions: {history[-4:] if history else '(none yet)'}\n"
        "There is no readable semantic state: you only have these screen statistics and your own action history.\n"
        "question: which button action should be pressed now to make progress in the game?\n"
    )


# ------------------------------------------------------- 时序座位（timing）+ 语义状态
def timing_text_rich(st: dict, frame: int, history: list[str]) -> str:
    """时序座位的 prompt：**状态用语义字段**（敌人/障碍/坑/是否站地），选项只有 3 个时序修正。

    ⚠ 修的是"模型没有可用输入"这个根因：旧版只印了 `pit ahead` 一条 ⇒ 它看不到
    "前方 12px 有板栗仔" ⇒ 只能塌缩到单一动作（2026-09-22 子代理诊断 + 我复核确认）。
    """
    m, ad = st["mario"], st.get("ahead") or {}

    def d(v):
        return f"{v}px ahead" if v is not None else "none in view"

    sp = st.get("sprites") or []
    # 关键信息：生物的**相对距离**（前方 N px）——子代理指出的近似就靠这条变成精确值
    sp_txt = "; ".join(
        f"{e.get('kind')} {e['screen_x'] - m['screen_x_px']:+d}px ahead, "
        f"{'on the ground' if e['screen_y'] >= 170 else 'above ground'}"
        for e in sp[:4]) or "none"
    return (
        "You are the TIMING controller of Mario in Super Mario Bros 1-1. He keeps running right by "
        "default; your decision replaces the controls for the next 6 frames. Touching a creature or "
        "falling into a pit costs a life.\n"
        f"frame={frame} time_left={st['time_s']} lives={st['lives']}\n"
        f"mario: level_x={st['level_x_px']}px screen_x={m['screen_x_px']} "
        f"on_ground={m['on_ground']} vy={m['vy_px_per_frame']}px/f\n"
        f"pit ahead: {d(ad.get('pit_near_px'))}\n"
        f"obstacle ahead: {d(ad.get('obstacle_px'))}\n"
        f"creatures on screen: {sp_txt}\n"
        f"your last decisions: {history[-4:] or '(none yet)'}\n"
        "Options: go=保持向右跑(不按 A) / jump=现在起跳(按住 A) / back=后退(按左)\n"
        "question: what should be done in the next 6 frames?\n"
    )


# ----------------------------------------------------------------- 富语义座位（rich）
def rich_text(state: dict, frame: int, history: list[str]) -> str:
    """把 `smb_state.read_state()` 的语义字典写成 prompt（模型看的全是**有单位的真实量**）。"""
    import smb_actions as SA
    m, ad = state["mario"], state.get("ahead") or {}

    def d(v):
        return f"{v}px ahead" if v is not None else "none in view"

    ens = state.get("sprites") or []
    # 只暴露**已验证**的判据：kind（tile 已验：goomba / 其余=unknown）＋ 贴地与否（y>=170=贴地怪物）
    en_txt = "; ".join(
        f"{e.get('kind')} at screen_x={e['screen_x']} y={e['screen_y']} "
        f"({'on the ground' if e['screen_y'] >= 170 else 'above the ground / on a block'})"
        for e in ens[:4]) or "none"
    blk = (ad.get("blocks") or [])[:4]
    blk_txt = "; ".join(f"{b['kind']} block screen_x={b['screen_x']} y={b['screen_y']}" for b in blk) or "none in view"
    opts = " / ".join(f"{v[2]}={v[3]}" for v in SA.TIMING_ACTIONS.values())
    return (
        "You are the decision layer of Mario in Super Mario Bros level 1-1. The motor layer keeps him "
        "running right by default; your decision overrides the controls for the next few frames only.\n"
        f"frame={frame} time_left={state['time_s']} lives={state['lives']} coins={state['coins']}\n"
        f"mario: level_x={state['level_x_px']}px screen_x={m['screen_x_px']} screen_y={m['screen_y_px']} "
        f"on_ground={m['on_ground']} vy={m['vy_px_per_frame']}px/f powerup={m['powerup']}\n"
        f"pit: {d(ad.get('pit_near_px'))} (furthest: {d(ad.get('pit_far_px'))})\n"
        f"obstacle ahead: {d(ad.get('obstacle_px'))}\n"
        f"creatures on screen: {en_txt}\n"
        f"blocks ahead: {blk_txt}\n"
        f"your last decisions: {history[-4:] or '(none yet)'}\n"
        f"Options: {opts}\n"
        "question: what should be done for the next few frames?\n"
    )


# ----------------------------------------------------------------- 模型（常驻）

class Adapter:
    def __init__(self, cmd: str, timeout: int = 900):
        self.proc = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        self.timeout, self.calls = timeout, 0

    def ask(self, text: str, candidates=None) -> tuple[str, float, dict]:
        payload = {"state_text": text,
                   "candidates": candidates or [{"id": k, "text": v[1]} for k, v in ACTIONS.items()]}
        t0 = time.perf_counter()
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + self.timeout
        while True:
            if time.time() > deadline:
                raise TimeoutError("adapter 超时")
            line = (self.proc.stdout.readline() or "").strip()
            if not line:
                continue
            try:
                got = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "choice" in got:
                self.calls += 1
                probs = {k: float(v) for k, v in (got.get("probs") or {}).items()}
                return got["choice"], (time.perf_counter() - t0) * 1000, probs


# ----------------------------------------------------------------- 游戏内 HUD 直读
# 面板与日志里的"进度"一律用**游戏内 HUD**：观众能对着画面逐项核对，不依赖任何
# 我自己算的坐标（0x0086 是屏幕相对 x，随镜头滚屏回绕，曾把我带偏过）。
_HUD_ADDRS = (0x07F8, 0x07F9, 0x07FA, 0x075A, 0x07ED)      # 计时3字节 · 生命 · 金币


def hud_text(page) -> tuple[str, list[int]]:
    b = page.evaluate("(as_) => as_.map(a => window.__ram(a, 1)[0])", list(_HUD_ADDRS))
    time_v = b[0] * 100 + b[1] * 10 + b[2]          # 计时＝三位十进制 BCD 各占一字节
    coins = (b[4] >> 4) * 10 + (b[4] & 0xF)
    return f"游戏内 TIME {time_v} · 生命 {b[3]} · 金币 {coins}", list(b)



class YoloBoxer:
    """YOLO 检测框：调 ML venv 的子进程，一帧一问一答（座位侧只负责转发与画框）。"""
    def __init__(self, cmd: str):
        self.proc = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        # 等 ready
        while True:
            line = (self.proc.stdout.readline() or "").strip()
            if line:
                try:
                    if json.loads(line).get("ready"):
                        break
                except json.JSONDecodeError:
                    continue

    def boxes(self, png: bytes):
        self.proc.stdin.write(json.dumps({"png_b64": base64.b64encode(png).decode()}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = (self.proc.stdout.readline() or "").strip()
            if not line:
                continue
            try:
                return json.loads(line).get("boxes") or []
            except json.JSONDecodeError:
                continue

    def close(self):
        try:
            self.proc.kill()
        except Exception:
            pass


# ----------------------------------------------------------------- 主循环

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rom", default="/roms/nova.nes")
    ap.add_argument("--adapter", default="", help="模型单发命令（--stdin-loop）；--dry-run 时不需要")
    ap.add_argument("--dry-run", action="store_true",
                    help="不用模型：固定按 右+A 循环，用来验证'按键真的控制了游戏'")
    ap.add_argument("--cycles", type=int, default=6)
    ap.add_argument("--hold-frames", type=int, default=6, help="每个决策按住几帧（＝决策步长/粒度）")
    ap.add_argument("--yolo-boxes", default="", metavar="CMD",
                    help="接 YOLO 检测框：给 ML venv 的 detect/boxes_ml.py 命令，每次决策前跑一次并画在画面上")
    ap.add_argument("--pulse-release", type=int, default=2,
                    help="每次新跳前先松全部键几帧（0=不松；脚本判据依赖连续按键时要设 0）")
    ap.add_argument("--jump-window", type=int, default=30,
                    help="A 最多连续按住多少帧（＝满跳飞行）；超窗后再选跳会先松手重按（保证边沿）")
    ap.add_argument("--guard", action="store_true",
                    help="分工架构：判断层（检测器/规则）给出**安全动作集**，模型只在集合内选"
                         "（危险=生物≤60px 或 坑≤90px 或 障碍≤60px ⇒ {小跳,大跳,退后}；否则 ⇒ {冲刺,走}）")
    ap.add_argument("--stop-on-stall", action="store_true",
                    help="检测到'游戏不再推进'（TIME/x/相机/digest 全冻 ≥ stall-frames 帧）就结束本轮。"
                         "实测：某些状态下 ROM 会进等待循环——帧数在涨、游戏不动，看着像'大脑变笨'，其实无进展")
    ap.add_argument("--no-press-start", dest="press_start", action="store_false",
                    help="跳过开机按键（默认会按，因为真游戏停在标题/菜单页）")
    ap.add_argument("--lives", type=int, default=0,
                    help="开局后把生命字节 0x075A 改成这个值（0 = 不动）")
    ap.add_argument("--boot", default="START:6:60,A:4:60,A:4:90",
                    help="开机配方 '键:按住帧:等待帧,...'（每款游戏不同；Nova 要过标题→选关→Start! 三层）")
    ap.add_argument("--dry-action", default="right_b", help="--dry-run 时固定按哪个动作")
    ap.add_argument("--peek-addr", type=int, default=134,
                    help="每周期顺带读一个内存字节（默认 0x0086＝SMB 马里奥**屏幕相对 x**：会随镜头滚屏回绕，"
                         "**不是世界坐标**；只进日志/调试，不喂模型、不当进度判据）")
    ap.add_argument("--peek-addr2", type=int, default=int("0x071C", 16),
                    help="第二个内存字节（默认 0x071C＝SMB 关卡滚动左边缘；喂给兜底脚本）")
    ap.add_argument("--peek-addr3", type=int, default=int("0x009F", 16),
                    help="第三个内存字节（默认 0x009F＝SMB 马里奥垂直速度：站地=0，用来判断是否在空中）")
    ap.add_argument("--warmup", action="store_true",
                    help="录制前先让模型跑一次空决策（首次调用含模型加载，可消掉录像开头的黑屏）")
    ap.add_argument("--tag", default="dry", help="结果目录/文件标签")
    ap.add_argument("--fallback-only", action="store_true",
                    help="只跑兜底脚本、不问模型（用来验证'脚本能不能过 1-1'）")
    ap.add_argument("--handoff-after", type=int, default=0,
                    help="微基准：先用脚本大脑跑该次数决策，再把控制权交给 --macro-brain（按决策数，比按进度稳）")
    ap.add_argument("--handoff-at", type=int, default=0,
                    help="微基准：先用脚本大脑推进到该进度(px)，再把控制权交给 --macro-brain 指定的脑子")
    ap.add_argument("--macro-mode", choices=("macro", "timing", "rich"), default="macro",
                    help="macro=语义宏（自带位移）/ timing=时序座位（马达固定向右，模型只管 跳/停/退）")
    ap.add_argument("--macro-brain", choices=("model", "script", "random", "ref", "script_guard"), default="model",
                    help="宏决策座位的大脑：model=模型 / ref=参照大脑（完整策略，对照上界）/ "
                         "script=简化映射 / random=随机下界")
    ap.add_argument("--macro", action="store_true",
                    help="宏决策座位：模型每 N 帧选一个语义宏动作（向前跑/向前跳/后退/等待/原地跳），脚本只执行")
    ap.add_argument("--hybrid", action="store_true",
                    help="门控混合：兜底脚本平时跑，只在它判定'有威胁'时问模型（决策者标签分别标注）")
    ap.add_argument("--viewer", action="store_true",
                    help="用带决策面板的 viewer.html（可视化：候选概率条 + 状态 + 决策者）")
    ap.add_argument("--record", action="store_true", help="录制页面视频（webm→mp4，DSF 必须=1）")
    ap.add_argument("--frames-out", action="store_true",
                    help="**逐帧录制**：每步逐帧推进并截图到 shots/，之后用 ffmpeg 按游戏帧拼视频。"
                         "模型每步要几百毫秒、而一步只推进几帧时，按墙钟录像＝把游戏放慢数倍又一顿一顿")
    ap.add_argument("--shot-every", type=int, default=2, help="逐帧录制时每几帧存一张（2 ⇒ 播放用 30fps 即实时）")
    ap.add_argument("--decider", default="模型（Laya）", help="面板上标注的决策者")
    ap.add_argument("--note", default="", help="面板上的补充说明（口径/兜底等）")
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright
    outdir = Path(__file__).parent / f"nes-play-{a.tag}"
    outdir.mkdir(parents=True, exist_ok=True)
    log_path = outdir / f"nes-play-{a.tag}.jsonl"
    ad = None if (a.dry_run or a.fallback_only or (a.macro and a.macro_brain not in ("model", "script_guard"))) else Adapter(a.adapter)
    from smb_fallback import SmbPolicy
    POL = SmbPolicy(hold_frames=a.hold_frames)
    history, prev_px, rows = [], None, []
    prog, prev_x = 0, 40
    stall_time, stall_run = None, 0
    RICH_PREV = {}
    RESTART = {}

    if a.warmup and ad is not None:
        t0 = time.perf_counter()
        try:
            ad.ask("warmup: the game is at the start of level 1-1. What now?")
        except Exception as exc:  # 预热失败不该毁掉整轮
            print(f"预热失败（忽略）：{exc}")
        print(f"模型预热完成 {time.perf_counter() - t0:.0f}s（此段不再进录像）")

    vp = {"width": 1020, "height": 1010} if a.viewer else {"width": 512, "height": 480}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport=vp, device_scale_factor=1,      # DSF≠1 会录出白帧（环境事实 #4）
                            record_video_dir=str(outdir / "video") if a.record else None,
                            record_video_size=vp)
        page = ctx.new_page()
        page.on("console", lambda m: None)
        page.goto(f"{BASE}/nes/{'viewer' if a.viewer else 'play'}.html")
        rom_url = quote(a.rom, safe="/")            # ROM 文件名常带空格/括号，必须编码后再注入
        page.evaluate("() => new Promise(res => window.__boot('%s', res))" % rom_url)
        boot_frames = page.evaluate("() => window.__frameCount()")
        print(f"模拟器已启动：{a.rom}，开机 {boot_frames} 帧")
        if a.press_start:
            # 真游戏有多层开局，且**每款游戏不同** ⇒ 开机配方外置为参数（'键:按住帧:等待帧,...'）
            for step in [s for s in a.boot.split(",") if s.strip()]:
                key, hold, settle = step.split(":")
                page.evaluate("([b, n]) => window.__hold(b, n)", [[key.strip()], int(hold)])
                page.evaluate("([n]) => window.__step(n)", [int(settle)])
            print(f"已按开机配方开局，当前 {page.evaluate('() => window.__frameCount()')} 帧")
        if a.lives:
            page.evaluate("(n) => { window.__nes.cpu.mem[0x075A] = n; }", a.lives)
            got = page.evaluate("() => window.__ram(0x075A, 1)[0]")
            print(f"[lives] 生命已设为 {got}（写入值 {a.lives}）")
            assert got == a.lives, "生命写入失败"

        shots_dir = outdir / "shots"
        shot_n = 0

        def take_shot() -> None:
            """逐帧录制：每 shot_every 帧存一张页面截图（含面板）。若开了 YOLO 框，
            则**在这一帧也重画一次框**（否则框只在决策点出现、帧间会消失）。"""
            nonlocal shot_n
            if shot_n % a.shot_every == 0:
                if BOXER is not None:
                    try:
                        _png = base64.b64decode(page.evaluate("() => window.__png()").split(",", 1)[1])
                        page.evaluate("(bs) => window.__boxes(bs)", BOXER.boxes(_png))
                    except Exception:
                        pass
                shots_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shots_dir / f"f-{shot_n // a.shot_every:05d}.png"))
            shot_n += 1

        PULSE = ButtonPulse(page, jump_window=a.jump_window, release_frames=a.pulse_release)
        BOXER = YoloBoxer(a.yolo_boxes) if a.yolo_boxes else None

        with log_path.open("w", encoding="utf-8") as log:
            for c in range(a.cycles):
                # ① 截屏
                data_url = page.evaluate("() => window.__png()")
                png = base64.b64decode(data_url.split(",", 1)[1])
                png_file = outdir / f"cycle-{c:03d}.png"
                png_file.write_bytes(png)
                # ② 分析（只看画面）
                st = png_stats(png)
                changed = 1.0 if prev_px is None else changed_ratio(prev_px, st["pixels"])
                fc_now = page.evaluate("() => window.__frameCount()")
                text = state_text(fc_now, st, history, changed)
                # 进度指标：只用于计分/日志，**不喂给模型**（模型只看画面统计）
                peek = page.evaluate("([a]) => window.__ram(a, 1)[0]", [a.peek_addr])
                peek2 = page.evaluate("([a]) => window.__ram(a, 1)[0]", [a.peek_addr2])
                peek3 = page.evaluate("([a]) => window.__ram(a, 1)[0]", [a.peek_addr3])
                hud_line, hud_raw = hud_text(page)
                # ③ 决策
                if a.macro:
                    import smb_macro as MG
                    import smb_state as SS
                    if a.macro_mode == "rich":
                        import smb_actions as SA
                        ACTS = SA.TIMING_ACTIONS            # 8 项：冲刺/走/小跳/大跳/蹲/后退/停/火球
                        state = SS.read_state(page, png)    # 语义状态（同一帧 RAM + 画面）
                        # ---- 判断层（不在模型里）：把"有没有危险"算成安全动作集 ----
                        DANGER = []
                        if a.guard:
                            mx0 = state["mario"]["screen_x_px"]
                            for e in (state.get("sprites") or []):
                                dx = e["screen_x"] - mx0
                                if 0 <= dx <= 45:
                                    DANGER.append(f"{e.get('kind')} {dx}px ahead")
                            ad0 = state.get("ahead") or {}
                            # 阈值要**贴着动作耗时**：一次满跳飞 ~100px ⇒ 坑在 90px 就跳＝跳早了会落进坑
                            # （实测：≤90px 触发时 300 步仍 3 次重开）⇒ 收到 40px 才起跳。
                            if ad0.get("pit_near_px") is not None and ad0["pit_near_px"] <= 40:
                                DANGER.append(f"pit {ad0['pit_near_px']}px ahead")
                            if ad0.get("obstacle_px") is not None and ad0["obstacle_px"] <= 45:
                                DANGER.append(f"obstacle {ad0['obstacle_px']}px ahead")
                            # 危险时的安全集只留**能过/能躲**的两种：满跳 或 后退
                            # （小跳常常不够高 ⇒ 之前它 60/63 掷硬币式选跳高，结果仍 3 次重开）
                            SAFE = (["big_jump", "back"] if DANGER else ["sprint", "walk"])
                            ACTS = {k: SA.TIMING_ACTIONS[k] for k in SAFE}
                        # 行为分类：怪物向左走 / 道具向右滑（用**关卡 x**比较，避开相机滚动的假位移。
                        # 依据：博主 2026-09-22「怪物蘑菇有腿和眼（会走）」+ 实测）
                        _prev = RICH_PREV.get("sprites") or []
                        _cur = state.get("sprites") or []
                        for sp in _cur:
                            sp["level_x"] = sp["screen_x"] + state["camera_x_px"]
                        if _prev:
                            cam_now = state["camera_x_px"]
                            cam_prev = RICH_PREV.get("cam", cam_now)
                            dcam = (cam_now - cam_prev) % 256      # 相机只向右推；取模展开跨 256 回绕
                            for sp in _cur:
                                q = min(_prev, key=lambda z: abs(z["screen_x"] - sp["screen_x"]), default=None)
                                if q is None or abs(q["screen_x"] - sp["screen_x"]) > 40:
                                    sp["motion"] = "new"
                                else:
                                    # 屏幕位移 + 相机位移 ⇒ 真实移动（正=向右/道具，负=向左/怪物）
                                    d = (sp["screen_x"] - q["screen_x"]) + dcam
                                    sp["dx_real"] = d
                                    sp["motion"] = ("moving_left(monster?)" if d < -4 else
                                                    "moving_right(item?)" if d > 4 else "still")
                        RICH_PREV["sprites"] = _cur
                        RICH_PREV["cam"] = state["camera_x_px"]
                        forx = {"pit_ahead_px": (state.get("ahead") or {}).get("pit_near_px"),
                                "rich": state}
                        text = rich_text(state, fc_now, history)
                        if a.guard:
                            text = ("DANGER CHECK (done by the perception layer, trust it): "
                                    + ("; ".join(DANGER) if DANGER else "no danger in range")
                                    + "\nYour allowed actions for this step are ONLY: "
                                    + ", ".join(f"{ACTS[k][2]}={ACTS[k][3]}" for k in ACTS) + "\n" + text)
                        MACRO_CANDS = [{"id": k, "text": v[3]} for k, v in ACTS.items()]
                        brain = a.macro_brain
                        if brain == "model":
                            choice, dt, probs = ad.ask(text, MACRO_CANDS)
                        elif brain == "script_guard":
                            # 分工架构（座位侧）：判据用**验证过能通关的** SmbPolicy，
                            # 模型只在"按该意图收窄后的安全集"里挑；安全集只剩 1 项时直接执行、不问模型。
                            import smb_fallback as FB
                            if "POL" not in RICH_PREV:
                                RICH_PREV["POL"] = FB.SmbPolicy(hold_frames=6)
                            base, why = RICH_PREV["POL"].decide(st, peek, cam=peek2, vy=peek3)
                            intent = {"right_b": "run", "right_ab": "jump", "right_a": "jump",
                                      "left": "back", "none": "run", "right": "run"}.get(base, "run")
                            # ⚠ 关键：危险窗口用 **small_jump(8 帧)** 而不是 big_jump(30 帧)——
                            # big_jump 一步吃 30 帧 ⇒ 判据每 30 帧才被问一次，时序被破坏（实测 3 次重开）。
                            # small_jump 连续选择会跨步保持 A ⇒ 等效长按，且判据回到 ~8 帧节奏。
                            SAFE = {"jump": ["small_jump", "back"], "back": ["back", "small_jump"],
                                    "run": ["sprint", "walk"]}[intent]
                            ACTS = {k: SA.TIMING_ACTIONS[k] for k in SAFE}
                            DANGER = [f"judgment layer (proven policy): {intent}"]  # 让日志可解析
                            RICH_PREV["intent"] = intent
                            probs = {}
                            if len(ACTS) == 1:
                                choice, dt = list(ACTS)[0], 0.0
                            else:
                                choice, dt, probs = ad.ask(
                                    "DANGER CHECK (from the perception+rule layer, trust it): "
                                    + DANGER[0] + "\nYour allowed actions for this step are ONLY: "
                                    + ", ".join(f"{ACTS[k][2]}={ACTS[k][3]}" for k in ACTS)
                                    + "\n" + rich_text(state, fc_now, history),
                                    [{"id": k, "text": v[3]} for k, v in ACTS.items()])
                                if choice not in ACTS:
                                    choice = list(ACTS)[0]
                        elif brain == "script":
                            import smb_fallback as FB
                            if "POL" not in RICH_PREV:
                                RICH_PREV["POL"] = FB.SmbPolicy(hold_frames=6)
                            base, why = RICH_PREV["POL"].decide(st, peek, cam=peek2, vy=peek3)
                            choice = {"right_b": "sprint", "right_ab": "big_jump",
                                      "right_a": "small_jump", "left": "back",
                                      "none": "stop", "right": "walk"}.get(base, "sprint")
                            dt, probs = 0.0, {}
                            RICH_PREV["why"] = why
                        else:
                            import random as _r
                            choice = _r.Random(c).choice(list(ACTS))
                            dt, probs = 0.0, {}
                        if choice not in ACTS:
                            choice = list(ACTS)[0]
                        macro_btns, macro_hold = ACTS[choice][0], ACTS[choice][1]
                        fc = PULSE.press(macro_btns, macro_hold,
                                         shot=take_shot if a.frames_out else None)
                        history.append(choice)
                        rec = {"cycle": c, "frame": fc, "choice": choice, "buttons": macro_btns,
                               "state_rich": state, "danger": DANGER if a.guard else None,
                               "safe_set": list(ACTS) if a.guard else None, "top_prob": round(max(probs.values()), 4) if probs else None,
                               "model_ms": round(dt, 1), "hud": hud_line, "hud_raw": hud_raw}
                        log.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        rows.append(rec)
                        if a.viewer:
                            page.evaluate("(d) => window.__ui(d)", {
                                "decider": a.decider, "note": (RICH_PREV.get("why") or a.note), "fallback": brain == "script",
                                "candidates": [{"id": k, "text": v[3], "p": probs.get(k)} for k, v in ACTS.items()],
                                "choice": choice, "buttons": [b for b in macro_btns if b not in ("RIGHT", "B")],
                                "motor": "马达层（恒定）：一直按住 RIGHT+B 向右跑",
                                "actionText": f"{ACTS[choice][2]}·按 {macro_hold} 帧",
                                "deciderIndex": ("model" if brain == "model" else "fallback"), "topProb": max(probs.values()) if probs else None,
                                "state": (f"cycle {c} / frame {fc_now}\n{hud_line}\n"
                                          f"level_x={state['level_x_px']} 敌人 {len(state.get('enemies') or [])} "
                                          f"坑 {forx['pit_ahead_px']} 障碍 {(state.get('ahead') or {}).get('obstacle_px')}"),
                                "history": history[-12:]})
                        print(f"  [富] {c:02d} 动作={ACTS[choice][2]:6s} p={rec['top_prob']} {dt:.0f}ms "
                              f"| {hud_line} | {state['level_x_px']}px 敌{len(state.get('enemies') or [])} "
                              f"坑{(state.get('ahead') or {}).get('pit_near_px')} "
                              f"障{(state.get('ahead') or {}).get('obstacle_px')}")
                        prev_px = st["pixels"]
                        continue
                    ACTS = MG.actions_for(a.macro_mode)
                    if a.macro_mode == "timing":
                        stateR = SS.read_state(page, png)      # 与 rich 同一套语义状态
                        text = timing_text_rich(stateR, fc_now, history)
                    # 0x0086 是**世界 x 的低字节**（256 回绕）。按"单步位移 <128"解展开：
                    # 不能用 |Δ|，否则"后退/停下"也被算成推进（本目录踩过）
                    d = peek - prev_x
                    if d > 128:
                        d -= 256
                    elif d < -128:
                        d += 256
                    prog += d
                    prev_x = peek
                    f = MG.features(st, peek, history, prog)
                    if a.macro_mode != "timing":       # timing 已用 timing_text_rich()，别覆盖
                        text = MG.state_text(f, fc_now)
                    MACRO_CANDS = [{"id": k, "text": v[3]} for k, v in ACTS.items()]
                    brain = a.macro_brain
                    if (a.handoff_after and c < a.handoff_after) or (a.handoff_at and prog < a.handoff_at):
                        brain = "script"          # 预跑段：用脚本大脑把马里奥开到指定位置
                    if brain == "model":
                        choice, dt, probs = ad.ask(text, MACRO_CANDS)
                    elif brain == "ref":
                        from smb_reference import ReferenceBrain
                        if "REF" not in globals():
                            REF = ReferenceBrain()
                        st2 = dict(st); st2["ram_x86"] = peek; st2["ram_cam"] = peek2; st2["ram_vy"] = peek3
                        keys, why = REF.decide(st2, fc_now)
                        # 键列表 → 同一套时序宏（元组查表，别用 frozenset 的字符串表示，顺序不稳）
                        # 两侧都按字母序排好（sorted(["RIGHT","A","B"]) = ("A","B","RIGHT")）
                        KEYMAP = {("B", "RIGHT"): "go", ("A", "B", "RIGHT"): "jump",
                                  ("B", "LEFT"): "back", ("LEFT",): "back",
                                  ("A",): "jump_here", (): "idle"}
                        choice = KEYMAP.get(tuple(sorted(keys)), "go")
                        dt, probs = 0.0, {}
                        f["ref_why"] = why                 # 参照大脑的理由（进日志/面板）
                    elif brain == "script":
                        base, why = POL.decide(st, peek)
                        choice = ({"right_b": "go", "right_ab": "jump"}.get(base, "go")
                                  if a.macro_mode == "timing" else
                                  {"right_b": "run_fwd", "right_ab": "jump_fwd"}.get(base, "run_fwd"))
                        dt, probs = 0.0, {}
                    else:
                        import random as _r
                        choice = _r.Random(c + a.handoff_at + a.handoff_after).choice(list(ACTS))
                        dt, probs = 0.0, {}
                    if choice not in ACTS:
                        choice = list(ACTS)[0]
                    macro_btns, macro_hold = ACTS[choice][0], ACTS[choice][1]
                    btns = macro_btns
                    # 一次决策 = 一次按键脉冲（A 有界保持；见 ButtonPulse）
                    fc = PULSE.press(macro_btns, macro_hold, shot=take_shot if a.frames_out else None)
                    history.append(choice)
                    rec = {"cycle": c, "frame": fc, "choice": choice, "buttons": macro_btns,
                           "peek": peek, "progress_px": prog,   # progress_px 未校准（屏幕相对 x 展开），仅调试
                           "hud": hud_line, "hud_raw": hud_raw,
                           "decider": {"model": "model", "script": "fallback", "random": "random",
                                        "ref": "reference"}[brain],
                           "phase": ("preroll" if ((a.handoff_after and c < a.handoff_after)
                                                   or (a.handoff_at and prog < a.handoff_at)) else "test"),
                           "features": f, "model_ms": round(dt, 1),
                           "top_prob": round(max(probs.values()), 4) if probs else None}
                    if a.viewer:
                        page.evaluate("(d) => window.__ui(d)", {
                            "decider": {"model": a.decider, "script": "脚本大脑（同座位对照）",
                                        "ref": "参照大脑（脚本上界）",
                                        "random": "随机大脑（下界）"}[a.macro_brain],
                            "note": f"时序座位 · 马达恒定向右 · 坑 {f['pit_ahead_px']}",
                            "fallback": False,
                            "candidates": [{"id": k, "text": v[3], "p": probs.get(k)} for k, v in ACTS.items()],
                            "choice": choice,
                            # 只画**模型自己的按键**：马达层的 RIGHT+B 单独一行，别让"A/B 同时亮"
                            # 看起来像"模型在一次决策里既选了跑又选了跳"（用户 2026-09-22 点破）
                            "buttons": [b for b in macro_btns if b not in ("RIGHT", "B")],
                            "motor": "马达层（恒定）：一直按住 RIGHT+B 向右跑",
                            "actionText": ACTS[choice][2],
                            "deciderIndex": ("model" if a.macro_brain == "model" else "fallback"),
                            "topProb": (max(probs.values()) if probs else None),
                            "state": (f"宏决策 {c} / frame {fc_now}\n{hud_line}\n"
                                      f"坑 {f['pit_ahead_px']}"),
                            "history": history[-12:]})
                    log.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    rows.append(rec)
                    print(f"  [宏] {c:02d} frame={fc:5d} 动作={ACTS[choice][2]:5s} 键={macro_btns or ['-']} "
                          f"{hud_line} 屏幕x={peek:3d} 相机={peek2:3d} 坑={f['pit_ahead_px']} "
                          f"p={rec['top_prob']} {dt:.0f}ms raw={hud_raw}")
                    prev_px = st["pixels"]
                    continue
                fallback_action, fallback_why, at_decision = None, "", False
                if a.hybrid:
                    fallback_action, fallback_why = POL.decide(st, peek, cam=peek2, vy=peek3)
                    at_decision = fallback_why.startswith(("前方地面断开", "前方"))
                if a.dry_run:
                    choice, dt, probs = a.dry_action, 0.0, {}
                elif a.hybrid and (not at_decision or a.fallback_only):
                    choice, dt, probs = fallback_action, 0.0, {}      # 兜底：不问模型
                else:
                    choice, dt, probs = ad.ask(text)
                    if choice not in ACTIONS:
                        choice = "none"
                btns = ACTIONS[choice][0]
                if a.viewer:
                    who = (a.decider if (at_decision and not a.fallback_only) or not a.hybrid
                           else "脚本兜底")
                    why = (f"模型接手：{fallback_why}" if (at_decision and not a.fallback_only)
                           else fallback_why) if a.hybrid else a.note
                    page.evaluate("""(d) => window.__ui(d)""", {
                        "decider": who, "note": why,
                        "fallback": ("兜底" in str(who)),
                        "candidates": [{"id": k, "text": v[1], "p": probs.get(k) if probs else None}
                                       for k, v in ACTIONS.items()],
                        "choice": choice, "buttons": btns,
                        "actionText": f"{ACTION_SHORT.get(choice, choice)}·按 {ACTION_HOLD.get(choice, a.hold_frames)} 帧",
                        "deciderIndex": ("model" if ((at_decision and not a.fallback_only) or not a.hybrid)
                                         else "fallback"),
                        "topProb": (max(probs.values()) if probs else None),
                        "state": (f"cycle {c} / frame {fc_now}\n{hud_line}\n"
                                  f"屏幕 x(0x{a.peek_addr:04X}) = {peek}  相机 = {peek2}  垂直速度 = {peek3}\n"
                                  f"亮度 {st['luma_mean']}  帧间变化 {changed}"),
                        "history": history[-12:]})
                if BOXER is not None:
                    try:
                        page.evaluate("(bs) => window.__boxes(bs)", BOXER.boxes(png))
                    except Exception as exc:
                        print(f"  ⚠ YOLO 画框失败（继续）：{exc}")
                # ④ 执行：一次决策 = 一次按键脉冲（方向常按、A 有界保持 ⇒ 边沿正确且落点可控）
                hold = ACTION_HOLD.get(choice, a.hold_frames)
                fc = PULSE.press(btns, hold, shot=take_shot if a.frames_out else None)
                # 无进展检测：**用游戏自己的心跳**（TIME 不递减）+ 排除刚重开的前 90 帧。
                # ⚠ 不再用"亮度>20"排除——终局/演示画面本身就偏暗，旧写法会让 game over 后的死跑
                # 一直不被识别（2026-09-22 实测白跑 20 分钟）。
                t_now = hud_raw[0] * 100 + hud_raw[1] * 10 + hud_raw[2]
                if t_now > (stall_time or 0):          # TIME 回升＝关卡重开
                    RESTART["frame"] = fc
                ready = fc - RESTART.get("frame", -10 ** 6) > 90
                frozen = (t_now == stall_time) and ready
                stall = stall_run + hold if frozen else 0
                stall_time, stall_run = t_now, stall
                if stall >= 120:
                    print(f"  ⚠ cycle {c}: 无进展 {stall} 帧（TIME 冻在 {t_now}）——本轮数据不可用于表现对比")
                    if a.stop_on_stall:
                        log.write(json.dumps({"stall_stop": True, "cycle": c, "frames": stall},
                                             ensure_ascii=False) + "\n")
                        print(f"  ⛔ --stop-on-stall：在 cycle {c} 结束本轮")
                        break
                history.append(choice)
                prev_px = st["pixels"]
                rec = {"cycle": c, "frame": fc, "choice": choice, "buttons": btns, "peek": peek,
                       "peek2": peek2, "peek3": peek3, "hud": hud_line, "hud_raw": hud_raw,
                       "decider": ("fallback" if (a.hybrid and (not at_decision or a.fallback_only))
                                   else ("dry" if a.dry_run else "model")),
                       "at_decision": at_decision, "fallback_why": fallback_why,
                       "luma_mean": st["luma_mean"], "changed_px_ratio": changed,
                       "top_colors": st["top_colors"], "model_ms": round(dt, 1),
                       "top_prob": round(max(probs.values()), 4) if probs else None, "stall_frames": stall}
                log.write(json.dumps(rec, ensure_ascii=False) + "\n")
                rows.append(rec)
                who_short = "脚本" if (a.hybrid and (not at_decision or a.fallback_only)) else "模型"
                print(f"  cycle {c:02d} frame={fc:5d} [{who_short}] 按键={btns or ['-']} 进度[0x{a.peek_addr:04X}]={peek:3d} "
                      f"亮度={st['luma_mean']:5.1f} 变化像素={changed:.3f} "
                      f"| {choice} {('p=%.3f ' % rec['top_prob']) if rec['top_prob'] else ''}"
                      f"{('%.0fms' % dt) if dt else '(dry-run)'} → {png_file.name}")
        if a.record:
            page.close()
            ctx.close()                          # 关闭上下文才会落盘 webm
            src = page.video.path()
            dst = outdir / "session.mp4"
            # 色偏修正：webm(VP8/VP9, BT.601 full) → h264 要显式指定矩阵，否则紫/绿色偏
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                            "-vf", "scale=out_color_matrix=bt709:out_range=tv",
                            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                            "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(dst)], check=True)
            print(f"录制完成 → {dst}")
        b.close()
    if ad:
        ad.proc.terminate()

    finals = {"rom": a.rom, "cycles": len(rows), "frames_advanced": rows[-1]["frame"] if rows else 0,
              "screen_changed_cycles": sum(1 for r in rows if (r.get("changed_px_ratio") or 0) > 0.001),
              "outdir": str(outdir), "dry_run": a.dry_run,
              "actions": [r["choice"] for r in rows]}
    (outdir / "summary.json").write_text(json.dumps(finals, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(finals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
