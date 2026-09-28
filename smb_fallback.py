"""smb_fallback.py — 兜底脚本：超级马里奥 1-1 的「一路向右跑，前方有坑/障碍就跳」

定位：**不是主角**。面板上它是「脚本兜底」那一半——模型（Laya）只在决策点插手，
其余时刻由本脚本按「画面统计 + 3 个 RAM 字节」反应式地跑跳。

=== 本机实测出来的坑（调这个脚本踩过的全部，视频里要讲）===
① **0x0086 不是"屏幕 x"，是马里奥世界 x 的低字节**（跑过 256px 就回绕：40→…→248→17→…）。
   屏幕 x = (0x0086 − 0x071C) & 0xFF，而 0x071C 是**相机（关卡滚动）左边缘**。
   早期版本直接把 0x0086 当屏幕坐标"往前看"，跑过 256px 后看的位置整体错位 ⇒ 瞎跳 + 该跳不跳。
② **`__hold(["RIGHT","A","B"], n)` 连续调用时 A 键从不松开**（jsnes 的抬起/按下之间不跑帧），
   而 SMB 的跳是**边沿触发**：按住 A 期间落地不会再跳 ⇒ 连着几百周期发 right_ab，马里奥全程只跳
   一次，跑到水管前永久卡死（上一版"卡在水管前"的真因，不是水管太高）。
   所以"跳"必须是**多周期宏**：按住 A ≈30 帧（跳到最高），结束必须有一个**不含 A** 的周期把 A 放开。
③ 判据不能是"非地面色＝坑"：地砖是 棕 `#b15400` / 浅桃 `#ffd1c7` / 黑 三层花纹交替。
   坑的判据是**地面带里露出天空色**。
④ **"我在空中吗"不能用"脚下方 5px 是不是天空"**：马里奥跑动姿势里红衣服只到膝盖，
   红像素包围盒底边比真实脚底高 ~6px ⇒ 站在地上也被判成空中 ⇒ 该跳的时候不跳（1-1 第一个蘑菇兵
   就是这么死的）。改用 0x009F（垂直速度，站地=0）+ "红包围盒下方 13px 内有没有实体像素"。
⑤ 地面上方 17~4px 的横带里"非天空＝障碍"会把**背景灌木/山丘**（绿色）一起算进去 ⇒ 一路瞎跳，
   跳的 30 帧里对蘑菇兵完全瞎 ⇒ 撞死。所以按**颜色**分：绿色且高度 ≤20px ＝灌木（无视），
   其余（棕/桃/黑的蘑菇兵、绿色但 ≥24px 的水管/乌龟、楼梯砖）＝真障碍。

行为契约不变：`decide(stats, mario_x) -> (action_id, reason)`，动作取自 `nes_play.ACTIONS`。
可选额外入参：`cam`（相机字节 0x071C）、`vy`（垂直速度字节 0x009F）——不传就退化成只看画面。
"""
from __future__ import annotations

GROUND = (0xB1, 0x54, 0x00)      # 地砖主色（本机实测）
SKY = (0x81, 0x79, 0xFF)         # 天空色（同时也用于"坑"判据）
MARIO_RED = (0xBC, 0x19, 0x00)   # 小马里奥帽子/衣服（实测 #bc1900，全屏唯一）
GREEN = {(0x00, 0xAB, 0x00), (0x75, 0xE3, 0x00), (0x5B, 0x6A, 0x00)}   # 灌木/山丘/水管/乌龟壳
BUSH_MAX_H = 20                  # 灌木/山丘的"矮"上限：绿色且不高于它 ⇒ 只是背景装饰


class SmbPolicy:
    """看画面的反应式兜底：默认一路 `right_b`（跑），只在坑/障碍前起跳。

    参数
    ----
    hold_frames : 每个决策按住多少帧（要和 nes_play 的 --hold-frames 一致：
                  "跳"宏要凑够 ~30 帧的 A 按住时长）
    pit_dist    : 坑沿进到这么近（px）就起跳（跳太早会落进坑里，所以坑要**晚跳**）
    obs_dist    : 障碍（蘑菇兵/水管/乌龟）进到这么近就起跳（跳早了顶多踏空，所以可以**早跳**）
    lookahead   : 往前看多远（px）
    """

    A_HOLD_FRAMES = 30           # 跳的 A 按住**总帧数**。两种驱动语义下都对：
                                 #  * 按键状态语义（`__set`，宏座位）：连续多步带 A ⇒ A 一直被按住 ⇒ 30 帧长按 ✔
                                 #  * 单次 __hold 语义（旧驱动）：多周期累加，jsnes 在周期之间看不到"抬起" ⇒ 同样 30 帧 ✔
                                 # 实测按住 A 的帧数 → 跳跃高度：4/6/8/12 帧 = 36/43/49/56px；
                                 # 3~4 格高的水管（48/64px）只有"跑起来 + 一次 30 帧长按"的满跳才上得去。
    BAND_UP, BAND_DOWN = 20, 3   # 障碍检测横带：地面上方 17~4px
    AIR_SCAN = 13                # "脚底 13px 内有没有实体" —— 站地判据

    def __init__(self, hold_frames: int = 10, pit_dist: int = 24, obs_dist: int = 40,
                 lookahead: int = 88):
        self.hold_frames = max(1, int(hold_frames))
        # 一个决策周期马里奥大约前进多少 px（跑 ≈2.5px/帧）——用来"提前一个周期"预判
        self.step_px = int(self.hold_frames * 2.5)
        self.last_frame = None        # 上一次决策时的帧号（用来**实测**一个周期到底多少帧）
        # 坑：**晚跳**（早跳会落进坑里），但阈值必须 ≥ 一个决策周期的步长：
        # 否则"这一周期坑沿还在 25px 外、下一周期已经到脚下"→ 一个周期跨过窗口 ⇒ 掉坑。
        # 一次完整跳的水平位移 ≈ 90~100px，所以 d ≤ step+14 时起跳仍能落到坑对面。
        self._base_pit, self._base_obs = pit_dist, obs_dist
        self.pit_dist = pit_dist          # try07 通关版常数：24
        self._refresh(lookahead)
        # 障碍：也要**早一个周期**（一个周期能跑 15~80px，跳晚了就是贴着水管起跳 ⇒ 原地上不去）
        self.obs_dist = max(obs_dist, self.step_px)
        self.lookahead = max(lookahead, self.pit_dist + 24)
        self.ground_y = None          # 地面**上表面**行（1-1 是平的，量一次锁住）
        self.jump_left = 0            # 还要继续按住 A 的周期数（跳宏剩余）
        self.cool = 0                 # 跳宏结束后的"放开 A / 等落地"周期数
        self.last_x = None
        self.stuck = 0
        self.recover = 0              # 卡住后的恢复动作进度（1=退一步 2-3=助跑 4=起跳）

    def _refresh(self, lookahead: int = 88) -> None:
        """按**实测**的一个决策周期帧数，重算"一个周期能跑多少 px"和三个阈值。

        为什么必须自适应：同一个策略要跑在两种座位上——
          * 旧座位：一步 6 帧（≈15px）  → 坑阈值 29px、障碍 40px 就够了；
          * 脉冲座位：一步 22~32 帧（≈55~80px）→ 阈值必须跟着放大，否则"这一周期坑沿还在 60px 外，
            下一周期已经在脚下"，判据完全错位（本目录实测：脉冲座位下 3 次重开）。
        """
        self.step_px = int(self.hold_frames * 2.5)
        self.pit_dist = self._base_pit    # try07 通关版常数：24（不再按步长派生）
        self.obs_dist = max(self._base_obs, self.step_px)
        self.lookahead = max(lookahead, self.pit_dist + 24)

    # ------------------------------------------------------------------ 画面理解
    @staticmethod
    def _at(px, w, x, y) -> tuple:
        i = (y * w + x) * 3
        return (px[i], px[i + 1], px[i + 2])

    def find_ground(self, px, w, h) -> int:
        """地面**上表面**行：从下往上第一行"过半像素不是天空"的（地砖带）。量一次锁住。

        1-1 全程平地，且马里奥不会走到底下去 ⇒ 锁住比每帧重算稳（最后一屏有九格楼梯，
        重算会被楼梯带偏）。

        ⚠ 阈值必须真的是"过半"（`w*0.6`；写成 `(w//2)*0.5` ＝ 1/4 宽会把背景绿色山丘当地面，
        地面行算到 200 ⇒ "坑"判据去看 206 行（本来就该是天空）⇒ 每周期都报"前方是坑"，一路瞎跳）。
        """
        if self.ground_y is not None:
            return self.ground_y
        for y in range(206, min(h, 236)):
            solid = sum(1 for x in range(0, w, 2) if self._at(px, w, x, y) != SKY)
            if solid > (w // 2) * 0.6:             # 采样步长 2 ⇒ 采样总数是 w//2
                self.ground_y = y
                return y
        self.ground_y = 208
        return self.ground_y

    def find_mario(self, px, w, h):
        """用像素找马里奥（红帽/红衣服全屏唯一）→ (屏幕 x 中心, 红包围盒最下一行)"""
        pat, row = bytes(MARIO_RED), w * 3
        x0 = x1 = y1 = None
        for y in range(96, h):
            s = y * row
            i = px.find(pat, s, s + row)
            while i != -1:
                if i % 3 == 0:
                    x = (i - s) // 3
                    x0 = x if x0 is None else min(x0, x)
                    x1 = x if x1 is None else max(x1, x)
                    y1 = y
                i = px.find(pat, i + 3, s + row)
        if x0 is None:
            return None
        return (x0 + x1) // 2, y1

    def _grounded(self, px, w, h, feet, mx, vy) -> bool:
        """站在实地上吗？① 0x009F（垂直速度）非 0 ⇒ 一定在空中；② 红包围盒下方 13px 内
        有没有实体像素（地砖/水管/砖块）。两者都不满足才算站地。"""
        if vy:
            return False
        x = min(w - 1, max(0, mx))
        for y in range(feet + 1, min(h, feet + self.AIR_SCAN)):
            if self._at(px, w, x, y) != SKY:
                return True
        return False

    # ------------------------------------------------------------------ 决策
    def decide(self, stats: dict, mario_x: int, cam: int = 0, vy: int | None = None,
               frame: int | None = None) -> tuple[str, str]:
        """返回 (动作, 理由)。动作 ∈ nes_play.ACTIONS 的键。

        frame：调用方给的**帧号**（可选）。给了就能实测"一个决策周期 = 多少帧" ⇒ 阈值自适应
               两种座位（步长 6 帧 / 22~32 帧）。
        """
        if frame is not None and self.last_frame is not None and frame > self.last_frame:
            self.hold_frames = max(1, frame - self.last_frame)
            self._refresh()
        if frame is not None:
            self.last_frame = frame
        px, w, h = stats["pixels"], stats["w"], stats["h"]
        gy = self.find_ground(px, w, h)
        m = self.find_mario(px, w, h)
        mx = m[0] if m else ((mario_x - cam) & 0xFF) + 8
        feet = m[1] if m else gy - 1
        grounded = self._grounded(px, w, h, feet, mx, vy)

        # --- ① 跳宏进行中：继续按住 A（跳到最高）---
        if self.jump_left > 0:
            self.jump_left -= 1
            if self.jump_left == 0:
                self.cool = 1
            return "right_ab", f"跳跃中（A 继续按住，剩 {self.jump_left} 周期）"
        # --- ② 跳宏刚结束：必须有一个不含 A 的周期把 A 放开，否则下一跳按不动 ---
        if self.cool > 0:
            self.cool -= 1
            return "right_b", "落地/松开 A（SMB 跳是边沿触发，A 不放开就再也跳不动）"

        # --- ③ 进度停滞：先原地起跳；还卡就"退一步 → 冲起来再跳"（人过水管就是这么过的）---
        if self.last_x is not None:
            adv = (mario_x - self.last_x) & 0xFF
            self.stuck = self.stuck + 1 if adv <= 4 else 0
        self.last_x = mario_x
        if grounded and self.stuck >= 3:
            n, self.stuck = self.stuck, 0
            return self._jump(f"原地卡住 {n} 周期（贴墙/贴水管）→ 起跳翻过去")

        # --- ④ 危险物：最近的坑沿 / 最近的地面障碍（按颜色区分灌木）---
        a0, a1 = mx + 12, min(w - 1, mx + self.lookahead)
        pit_near = pit_far = None
        for x in range(a0, a1):
            if sum(1 for y in (gy + 6, gy + 12, gy + 18)
                   if self._at(px, w, x, y) == SKY) >= 2:
                pit_far = x
                if pit_near is None:
                    pit_near = x
        obs, obs_kind = None, ""
        for x in range(a0, a1):
            hh = 0
            for y in range(gy - 1, gy - self.BAND_UP - 4, -1):
                if self._at(px, w, x, y) == SKY:
                    break
                hh += 1
            if hh < 8:
                continue
            c6 = self._at(px, w, x, gy - 6)
            green = c6 in GREEN and (self._at(px, w, x, gy - 14) in GREEN or hh <= BUSH_MAX_H)
            if green and hh <= BUSH_MAX_H:
                continue                      # 灌木/矮绿植：背景装饰，无视
            obs = x
            obs_kind = "水管/乌龟" if green else "蘑菇兵/砖"
            break

        d_pit = 999 if pit_near is None else pit_near - mx
        d_obs = 999 if obs is None else obs - mx
        if not grounded:
            return "right_b", "空中（上升/下落中）→ 不打扰，继续向右"
        if d_pit <= self.pit_dist:
            width = 999 if pit_far is None else pit_far - pit_near + 1
            return self._jump(f"前方地面断开（坑沿 {d_pit}px，宽约 {width}px，地面带露出天空）→ 起跳")
        if d_obs <= self.obs_dist:
            return self._jump(f"前方 {d_obs}px 处有{obs_kind}（地面上方 17~4px 连续实体）→ 起跳")
        return "right_b", f"无威胁 → 继续跑（右侧坑 {d_pit}px / 障碍 {d_obs}px）"

    def _jump(self, why: str) -> tuple[str, str]:
        # 本次决策就会按住 A；还要再按住几个决策步，把 A 凑够 ~A_HOLD_FRAMES 帧。
        # 脉冲座位里一步的 A 就有 30 帧 ⇒ 1 步封顶；旧座位一步 6 帧 ⇒ 5 步（小跳链）。
        a_per_step = max(1, self.hold_frames)         # 一步按满 = hold_frames 帧 A（release=0）
        self.jump_left = max(1, round(self.A_HOLD_FRAMES / a_per_step)) - 1
        self.last_x = None
        return "right_ab", why
