"""smb_state.py — 超级马里奥 1-1 的 RAM 语义状态读取（字段全部带"我怎么验证它对"的证据）

用法：
    from smb_state import read_state
    st = read_state(page, png_bytes)      # page = 已 __boot 的 viewer/play 页；png = 同一帧的画面

字段（英文名 / 单位 / 验证证据）见 read_state 的 docstring。
**未确定**的字段在返回里标 `None` 或写在 `read_state(...)["uncertain"]` 列表里，绝不编数。

=== 本文件里每个地址的验证方式（都做过，不是猜的）===
* TIME 0x07F8/0x07F9/0x07FA（三位数字，各占一字节）
  验证：开局读到 4,0,0；跑 240 帧后读到 3,9,5 ⇒ 与画面 HUD 的 TIME 400→395 一致；
  另外在同一批 run 里把 RAM 数字与视觉模型读出的 HUD（400/392/388/335/271）对齐过。
* lives 0x075A
  验证：开局 2；故意撞蘑菇兵死一次后读到 1（与 HUD 的 ×2 → ×1 一致）。
* coins 0x07ED（**本机只验证到"0 与画面 ×00 一致"，≥1 的情况未验证**）
* 马里奥关卡 x 低字节 0x0086
  验证：按 RIGHT 递增/按 LEFT 递减（方向复核）；且 (0x0086 − 0x071C) & 0xFF 恒为 112
  （相机锁定后马里奥始终在屏幕 x=112）⇒ 它确实是"世界 x 的低字节"，不是屏幕 x。
* 相机左边缘 0x071C
  验证：与 0x0086 联立后马里奥屏幕 x 恒为 112；把它当相机去预测"关卡滚动"与画面一致。
* 马里奥竖直速度 0x009F
  验证：跳跃时读到 251→252→…→0→…→4 的完整抛物线（与逐帧量的精灵高度同步）；
  站在地面上恒为 0。
* 敌人（屏幕 x / y）：读 **精灵 OAM** 0x0200-0x023F（64 个精灵 × 4 字节：y/tile/attr/x），
  排除马里奥自己的 6 个精灵（屏幕 x 落在马里奥红像素包围盒附近）。
  验证：probe_actions.py 第 3 段把 OAM 条目与"画面里按颜色量出的贴地实体列"逐帧配对——
  帧 2 里 OAM 出现 (x=122,y=192,tile=0x71) / (x=130,y=192,tile=0x70)，正好是画面里那两只
  蘑菇兵的左/右半（tile 0x70/0x71 = 蘑菇兵），而 (112..120, 180..200, 0xfc/0x9e/0x9f)
  是马里奥自己的精灵 ⇒ OAM 里"非马里奥的贴地精灵"就是敌人，tile 可判种类。
  **未确定**：敌人槽表在 RAM 的哪一段（0x06E0-0x06FF 试过，不是）；敌人的"存活/踩扁"状态字节。
* 场景结构（坑 / 障碍 / ? 砖块）：纯像素判据（下面 PIXEL 一节），不依赖 RAM。
"""
from __future__ import annotations

SKY = (0x81, 0x79, 0xFF)
GROUND = (0xB1, 0x54, 0x00)
PEACH = (0xFF, 0xD1, 0xC7)
BLACK = (0x00, 0x00, 0x00)
GREEN = {(0x00, 0xAB, 0x00), (0x75, 0xE3, 0x00), (0x5B, 0x6A, 0x00)}
QUESTION_ORANGE = (0xF7, 0xB4, 0x00)      # ? 砖块/金币的橙（也是马里奥鞋色，需配合 y 区间用）
MARIO_RED = (0xBC, 0x19, 0x00)

RAM = {"time": (0x07F8, 0x07F9, 0x07FA), "lives": 0x075A, "coins": 0x07ED,
       "level_x": 0x0086, "camera": 0x071C, "vy": 0x009F,
       "oam": 0x0200, "oam_len": 0x40, "powerup": 0x0756}
GROUND_BAND_TOP = 208          # 地砖带最上一行（本机实测：208~231 是地砖，232+ 是黑边）


def _ram(page, addr: int, n: int = 1):
    return page.evaluate("([a, n]) => window.__ram(a, n)", [addr, n])


def _px(png: bytes):
    from nes_play import png_stats
    st = png_stats(png)
    return st["pixels"], st["w"], st["h"]


def _at(px, w, x, y):
    i = (y * w + x) * 3
    return (px[i], px[i + 1], px[i + 2])


def find_mario(px, w, h):
    """画面里马里奥（红 #bc1900 全屏唯一）→ (中心 x, 红包围盒 x0, x1, y0, y1)。

    返回整盒是为了**按几何包含排除他的 OAM**：跳/走姿势比他站立时更高更宽，
    用"脚底行 ± 固定值"会漏（Main 的 spritecheck 用例就是这么漏的）。
    """
    pat, row = bytes(MARIO_RED), w * 3
    x0 = x1 = y0 = y1 = None
    for y in range(96, h):
        s = y * row
        i = px.find(pat, s, s + row)
        while i != -1:
            if i % 3 == 0:
                x = (i - s) // 3
                x0 = x if x0 is None else min(x0, x)
                x1 = x if x1 is None else max(x1, x)
                y0 = y if y0 is None else min(y0, y)
                y1 = y
            i = px.find(pat, i + 3, s + row)
    if x0 is None:
        return None
    return (x0 + x1) // 2, x0, x1, y0, y1


def scan_ahead(px, w, h, mx: int, gy: int = GROUND_BAND_TOP, lookahead: int = 120):
    """往前看：坑沿 / 障碍 / ? 砖块（返回距离 px，None = 这段没有）"""
    a0, a1 = mx + 12, min(w - 1, mx + lookahead)
    pit = pit_far = None
    for x in range(a0, a1):
        if sum(1 for y in (gy + 6, gy + 12, gy + 18) if _at(px, w, x, y) == SKY) >= 2:
            pit_far = x
            if pit is None:
                pit = x
    obs = None
    for x in range(a0, a1):
        hh = 0
        for y in range(gy - 1, gy - 26, -1):
            if _at(px, w, x, y) == SKY:
                break
            hh += 1
        if hh < 8:
            continue
        c6 = _at(px, w, x, gy - 6)
        green = c6 in GREEN and (hh <= 20 or _at(px, w, x, gy - 14) in GREEN)
        if green and hh <= 20:
            continue
        obs = x
        break
    blocks = []
    for x in range(a0, a1):
        for y in range(128, 192):
            if _at(px, w, x, y) == QUESTION_ORANGE:
                blocks.append({"screen_x": x, "screen_y": y, "kind": "question"})
                break
            if _at(px, w, x, y) in (GROUND, PEACH) and _at(px, w, x, y - 2) == BLACK:
                blocks.append({"screen_x": x, "screen_y": y, "kind": "brick_or_used"})
                break
    # 合并相邻列（同一块砖宽 16px）
    merged = []
    for b in blocks:
        if merged and b["screen_x"] - merged[-1]["screen_x"] <= 2 and b["kind"] == merged[-1]["kind"]:
            continue
        merged.append(b)
    return {"pit_near_px": None if pit is None else pit - mx,
            "pit_far_px": None if pit_far is None else pit_far - mx,
            "obstacle_px": None if obs is None else obs - mx,
            "blocks": merged}


def read_oam(page):
    """精灵 OAM（0x0200-0x023F，64 个 × 4 字节：y / tile / attr / x）→ **生物分组**（8×8 精灵模式）。

    分组规则（8×8 精灵模式，实测）：一个 16×16 的生物占 **4 条 OAM**（2 列 × 2 行，如板栗仔
    在 (170,184,0xFC)(178,184,0xFC)(170,192,0x71)(178,192,0x70)）；小马里奥 = 2 列 × 3 行 = 6 条。
    HUD 图标在 y < 40（如 0xFF 出现在 (88,24)），一律不算生物。
    """
    raw = _ram(page, RAM["oam"], RAM["oam_len"])
    ents = []
    for i in range(0, len(raw), 4):
        y, tile, attr, x = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if y <= 1 or y >= 240 or y < 40:          # y=240+ 隐藏精灵；y<40 是 HUD
            continue
        ents.append({"slot": i // 4, "screen_x": x, "screen_y": y, "tile": tile, "attr": attr})
    ents.sort(key=lambda e: (e["screen_y"], e["screen_x"]))
    out, used = [], set()
    for i, e in enumerate(ents):
        if i in used:
            continue
        # 一个生物 = 以 (x0,y0) 为左上角的 2×3 格（16px 宽 × 最多 24px 高）内的所有 OAM 条目：
        # 板栗仔 = 2×2 = 4 条；小马里奥 = 2×3 = 6 条（8×8 精灵模式下每格一条）。
        box = [e]
        used.add(i)
        for j, o in enumerate(ents):
            if j in used:
                continue
            if e["screen_x"] <= o["screen_x"] <= e["screen_x"] + 8 and \
               e["screen_y"] <= o["screen_y"] <= e["screen_y"] + 16:
                box.append(o)
                used.add(j)
        out.append({"screen_x": min(p["screen_x"] for p in box),
                    "screen_y": min(p["screen_y"] for p in box),
                    "tiles": [p["tile"] for p in box], "n_oam": len(box),
                    "attr": box[0]["attr"]})
    return out


TILE_KIND = {
    # tile → (kind, 中文, 证据)
    0x70: ("goomba", "板栗仔(怪物蘑菇)",
           "同帧配对实测**完整 OAM 组**：(170,184,0xFC)(178,184,0xFC)(170,192,0x71)(178,192,0x70)"
           "= 画面里那只板栗仔的 2×2 四格；dy=+16（与马里奥脚同高=站地上）；逐帧 x 变小=会走；"
           "Main 独立目检 112/113(0x70/0x71)、114/115(0x72/0x73) 也是它"),
    0x71: ("goomba", "板栗仔(怪物蘑菇)", "同上（左右半的 tile 差 1）"),
    0x72: ("goomba", "板栗仔(怪物蘑菇)", "走动动画的第二帧（同组 tile 差 1）——**按同组推断，未单独目检**"),
    0x73: ("goomba", "板栗仔(怪物蘑菇)", "同上"),
}
# 马里奥各姿势的 tile（Main 的 spritecheck 用例里出现 0x41-0x43；还有 0x4F/0x50 之类）
MARIO_TILES = {0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x3B, 0x3C,
               0x41, 0x42, 0x43, 0x44, 0x45, 0x4F, 0x50, 0x51, 0x52,
               0x9E, 0x9F, 0xFC, 0xF6, 0xFB, 0xAD, 0xAE, 0xAF, 0xE0, 0xE1, 0xE2, 0xE3}


def read_state(page, png: bytes) -> dict:
    """从**同一帧**的 RAM + 画面读出语义状态。

    返回字段（单位都写在名字里）：
      time_s          剩余时间（游戏内的 TIME 计数，3 位数字）        [RAM 0x07F8-FA，见模块 docstring]
      lives           剩余人数                                        [RAM 0x075A]
      coins           金币数                                          [RAM 0x07ED，只验证到 0 的情形]
      level_x_px      马里奥关卡 x（像素，低字节；调用方自己用 ±128 规则展开）
      camera_x_px     相机左边缘（像素，低字节）
      mario           屏幕 x 中心/脚底行/竖直速度/是否站地/是否顶砖/变身（powerup 见 uncertain）
      sprites         OAM 分组后的**生物**列表（一个生物 = 同 y、x 差 8px、tile 差 1 的两条 OAM）：
                    字段 screen_x/screen_y/tiles/n_oam/attr + kind/kind_zh/evidence
                    （kind 由 TILE_KIND 表决定，表里每条都写了验证依据；不在表里 = "unknown/未定案"）
    enemies         = sprites 里 screen_y ≥ 170 的那些（贴地生物，兼容旧字段；**计数以 sprites 为准**）
      ahead           坑沿/障碍/前方砖块距离
      uncertain       本机没能确定的字段名列表（别当成数据用）
    """
    px, w, h = _px(png)
    t = _ram(page, RAM["time"][0], 3)
    time_s = t[0] * 100 + t[1] * 10 + t[2] + 1
    lives = _ram(page, RAM["lives"], 1)[0]
    coins = _ram(page, RAM["coins"], 1)[0]
    lx = _ram(page, RAM["level_x"], 1)[0]
    cam = _ram(page, RAM["camera"], 1)[0]
    vy = _ram(page, RAM["vy"], 1)[0]
    vy_s = vy - 256 if vy > 127 else vy
    m = find_mario(px, w, h)
    mx = m[0] if m else (lx - cam) & 0xFF
    feet = m[4] if m else GROUND_BAND_TOP - 1          # 红包围盒最下一行 ≈ 脚底
    grounded = vy_s == 0 and any(_at(px, w, min(w - 1, mx), y) != SKY
                                 for y in range(feet + 1, min(h, feet + 11)))
    oam = read_oam(page)
    # 排除马里奥自己：按**几何包含**（不是中心点距离）——他的 OAM 是他的 2 列 × 3~4 行精灵块，
    # x 与他相同或 +8，y 覆盖他脚上方 ~44px 到脚下方 ~12px。跳/走姿势会更高，所以上界给足 44px。
    # 排除马里奥：以他的红像素包围盒为中心**扩 ±24px**（跳/走姿势更高更宽，固定几何会漏）
    if m:
        _, bx0, bx1, by0, by1 = m
    else:
        bx0 = bx1 = mx
        by0 = by1 = feet
    mario_box = (bx0 - 24, by0 - 24, bx1 + 24, by1 + 24)
    sprites = []
    for e in oam:
        x0, y0, x1, y1 = mario_box
        in_box = x0 <= e["screen_x"] <= x1 and y0 <= e["screen_y"] <= y1
        all_mario_tiles = all(t in MARIO_TILES for t in e["tiles"])
        # 排除玩家的判据必须**两条都成立**：① 落在他的包围盒里 ② 4~6 条 OAM 的 tile **全部**是
        # 马里奥的 tile。只按几何吞会把紧贴他的板栗仔误杀（板栗仔组 = [0xFC,0xFC,0x71,0x70]，
        # 0xFC 与马里奥同号，但 0x70/0x71 不是 ⇒ 它必须活下来）——Main 的 audit2/frame-021 就是这个 bug。
        if in_box and all_mario_tiles:
            continue
        # 认类别：在该生物的 4~6 条 OAM 里找**第一块已定案的 tile**（跳过马里奥自己的 tile 号，
        # 因为板栗仔的左上角 tile 0xFC 与马里奥的某姿势同号）
        k = next((TILE_KIND[t] for t in e["tiles"] if t in TILE_KIND and t not in MARIO_TILES), None)
        sprites.append({**e,
                        "kind": k[0] if k else "unknown",
                        "kind_zh": k[1] if k else "未定案",
                        "evidence": k[2] if k else "tile 不在已定案表里（需 probe_sprites 补证据）"})
    return {
        "time_s": time_s, "lives": lives, "coins": coins,
        "level_x_px": lx, "camera_x_px": cam,
        "mario": {"screen_x_px": mx, "screen_y_px": feet, "vy_px_per_frame": vy_s,
                  "on_ground": grounded, "powerup": _ram(page, RAM["powerup"], 1)[0]},
        "sprites": sprites,
        "enemies": [s for s in sprites if s["screen_y"] >= 170],   # 兼容旧字段：贴地生物 
        "ahead": scan_ahead(px, w, h, mx),
        "uncertain": ["powerup 的取值含义（0=小 已确认；1=大 / 2=火球 未在 1-1 里拿到变身验证过）",
                      "coins 只有 0 的情形与画面核对过",
                      "enemies 只做到「OAM 贴地精灵」这一层：敌人槽表的 RAM 地址未找到，"
                      "敌人「存活/被踩扁」的具体状态字节未确定（OAM 里有/没有只说明画不画）",
                      "「正在顶砖」没有独立字段：用 vy<0 且头顶有砖近似"],
    }
