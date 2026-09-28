"""detect/oamlabel.py — 把 OAM（0x0200..0x023F，64 字节）转成 YOLO 真值框。纯标准库，两个 venv 都能跑。

真值来源：**OAM**（不是识图模型）。分组规则与 `smb_state.read_oam` 一致：
一个生物 = 以某条 OAM 的 (x,y) 为左上角、`x ∈ [x0, x0+8]`、`y ∈ [y0, y0+16]` 内的所有 OAM 条目。

本目录实测出来的三条补充（有数字支撑，见 report）：
① **0xFC 是空白填充 tile**（马里奥 16×32 的精灵块里没用到的那几行、板栗仔块的最上一行都是 0xFC）。
   证据：帧 00161 马里奥块 y=176/184 两行全 0xFC，画面上对应 y=177..192 一片纯天空；
   板栗仔块 y=184 行全 0xFC，可见身体的最高点是从 0x70/0x71 那行开始的。
   ⇒ 定案前把 0xFC 当"马里奥 tile"会把**空白行算进框里**（框比生物高 8~16px）。
② 一个生物可能被切成**两组**（马里奥 16×24 + 16×8；板栗仔 6 条 0x70-0x73 + 空白行）。
   ⇒ 先按类分组，再把 **同类、同列、纵向相邻** 的组并成一个（合并只发生在同类之间，
   贴在一起的马里奥与板栗仔不会被并）。
③ 可见框 = **非空白 tile 条目的包围盒**，宽高各取 min 16px。实测（1200 帧里 229 帧有板栗仔）：
   框下沿与画面上生物像素块下沿**完全一致**的有 183/229 帧，x 方向 0 偏移最多（见 report）。

类别只定案到两类（其余未知组一律不标 = 宁缺毋滥）：
  goomba  ← 组里出现 tile 0x70..0x73（`smb_state.TILE_KIND` 已定案；实测板栗仔 = {0x70..0x73} + 0xFC 空白行）
  mario   ← 组的 tile 全落在 `smb_state.MARIO_TILES`（该集合含 0xFC，所以必须先判 goomba）
其余 → 不标。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 工程根目录
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smb_state import MARIO_TILES                      # noqa: E402  只读复用，不改动

SCREEN_W, SCREEN_H = 256, 240
OAM_ADDR, OAM_BYTES = 0x0200, 0x40
CLASSES = ("goomba", "mario")                          # 索引即 YOLO cls：goomba=0, mario=1
GOOMBA_TILES = frozenset(range(0x70, 0x74))
BLANK = 0xFC                                           # 空白填充 tile（实测：画面上什么都不画）
HUD_Y = 40                                             # y < 40 全是 HUD 图标
SPRITE = 8                                             # 8×8 精灵模式


def parse_oam(raw) -> list[dict]:
    """原始 64 字节 → OAM 条目（y/tile/attr/x）。过滤：屏幕外(y<=1 / y>=240) 与 HUD(y<40)。"""
    out = []
    for i in range(0, len(raw) - 3, 4):
        y, tile, attr, x = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if y <= 1 or y >= SCREEN_H or y < HUD_Y:
            continue
        out.append({"slot": i // 4, "x": x, "y": y, "tile": tile, "attr": attr})
    return out


def group_oam(entries: list[dict]) -> list[dict]:
    """OAM 条目 → 生物分组（与 smb_state.read_oam 同一规则）。"""
    ents = sorted(entries, key=lambda e: (e["y"], e["x"]))
    out, used = [], set()
    for i, e in enumerate(ents):
        if i in used:
            continue
        box = [e]
        used.add(i)
        for j, o in enumerate(ents):
            if j in used:
                continue
            if e["x"] <= o["x"] <= e["x"] + SPRITE and e["y"] <= o["y"] <= e["y"] + 2 * SPRITE:
                box.append(o)
                used.add(j)
        xs = [p["x"] for p in box]
        ys = [p["y"] for p in box]
        out.append({"x": min(xs), "y": min(ys),
                    "w": max(xs) - min(xs) + SPRITE, "h": max(ys) - min(ys) + SPRITE,
                    "tiles": [p["tile"] for p in box], "n_oam": len(box), "attr": box[0]["attr"]})
    return out


def group_class(tiles) -> str | None:
    """分组 tile 列表 → 类别名；未知组 → None（不标）。"""
    if any(t in GOOMBA_TILES for t in tiles):
        return "goomba"                                    # 先判 goomba：0xFC 同时也在 MARIO_TILES 里
    if tiles and all(t in MARIO_TILES for t in tiles):
        return "mario"
    return None


def _nonblank(entries, group) -> list[dict]:
    return [p for p in entries if p["tile"] != BLANK and
            group["x"] <= p["x"] <= group["x"] + SPRITE and
            group["y"] <= p["y"] <= group["y"] + 2 * SPRITE]


def merge_clusters(groups: list[dict], entries: list[dict]) -> list[dict]:
    """同类的组、列对齐(x 差 ≤2)、纵向相邻(间隔 ≤8) → 并成一个（马里奥的 16×24+16×8 是一个生物）。"""
    clusters: list[dict] = []
    for g in sorted(groups, key=lambda g: (g["cls"], g["x"], g["y"])):
        for c in clusters:
            if c["cls"] == g["cls"] and abs(c["x"] - g["x"]) <= 2 and g["y"] - (c["y"] + c["h"]) <= 8:
                c["y2"] = max(c["y2"], g["y"] + g["h"])
                c["h"] = c["y2"] - c["y"]
                c["groups"].append(g)
                break
        else:
            clusters.append({"cls": g["cls"], "x": g["x"], "y": g["y"], "y2": g["y"] + g["h"],
                             "h": g["h"], "groups": [g]})
    for c in clusters:                                     # 框 = 非空白条目的包围盒，宽高各 ≥16px
        pts = [p for g in c["groups"] for p in _nonblank(entries, g)]
        c["boxes"] = pts
        if not pts:
            c["skip"] = "只有空白填充 tile（生物没画出来）"
            continue
        x0, y0 = min(p["x"] for p in pts), min(p["y"] for p in pts)
        w = max(16, max(p["x"] for p in pts) - x0 + SPRITE)
        h = max(16, max(p["y"] for p in pts) - y0 + SPRITE)
        x1, y1 = min(SCREEN_W, x0 + w), min(SCREEN_H, y0 + h)
        if x1 - max(0, x0) < 8 or y1 - max(0, y0) < 8:
            c["skip"] = "出屏"
            continue
        c.update(x=max(0, x0), y=max(0, y0), w=x1 - max(0, x0), h=y1 - max(0, y0))
    return clusters


def boxes_from_oam(raw) -> tuple[list[dict], dict]:
    """OAM 原始字节 → (框列表, 统计)。框为屏幕像素坐标 {cls,x,y,w,h,tiles,n_oam}。"""
    entries = parse_oam(raw)
    groups = []
    for g in group_oam(entries):
        cls = group_class(g["tiles"])
        if cls is None:
            groups.append({**g, "cls": None})
        else:
            groups.append({**g, "cls": cls})
    stats = {"groups": len(groups), "unknown": sum(1 for g in groups if g["cls"] is None),
             "merged": 0, "clipped": 0, "blank_only": 0}
    clusters = merge_clusters([g for g in groups if g["cls"]], entries)
    boxes = []
    for c in clusters:
        stats["merged"] += len(c["groups"]) - 1
        if c.get("skip"):
            stats["blank_only"] += 1
            continue
        if (c["x"], c["y"]) != (min(p["x"] for p in c["boxes"]), min(p["y"] for p in c["boxes"])):
            stats["clipped"] += 1
        boxes.append({"cls": c["cls"], "x": c["x"], "y": c["y"], "w": c["w"], "h": c["h"],
                      "tiles": sorted({t for g in c["groups"] for t in g["tiles"]}),
                      "n_oam": sum(g["n_oam"] for g in c["groups"])})
    return boxes, stats


def to_yolo_line(b: dict) -> str:
    """屏幕像素框 → `cls cx cy w h`（归一化）。"""
    cls = CLASSES.index(b["cls"])
    cx = (b["x"] + b["w"] / 2) / SCREEN_W
    cy = (b["y"] + b["h"] / 2) / SCREEN_H
    return f"{cls} {cx:.6f} {cy:.6f} {b['w'] / SCREEN_W:.6f} {b['h'] / SCREEN_H:.6f}"


def labels_text(boxes: list[dict]) -> str:
    return "".join(to_yolo_line(b) + "\n" for b in boxes)


def parse_label_file(path: Path) -> list[dict]:
    """YOLO txt → 屏幕像素框（eval 用）。"""
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        p = line.split()
        if len(p) != 5:
            continue
        c, cx, cy, w, h = int(p[0]), *map(float, p[1:])
        out.append({"cls": CLASSES[c], "x": (cx - w / 2) * SCREEN_W, "y": (cy - h / 2) * SCREEN_H,
                    "w": w * SCREEN_W, "h": h * SCREEN_H})
    return out


def iou(a: dict, b: dict) -> float:
    ax1, ay1, ax2, ay2 = a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]
    bx1, by1, bx2, by2 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
    ix, iy = max(ax1, bx1), max(ay1, by1)
    ux, uy = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ux - ix), max(0.0, uy - iy)
    inter = iw * ih
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def match(dets: list[dict], gts: list[dict], thr: float = 0.5) -> dict:
    """同类贪心 IoU 匹配（先取最高 IoU）→ {tp, fp, fn, pairs}。"""
    pairs, used_d, used_g = [], set(), set()
    cands = sorted(((iou(d, g), di, gi) for di, d in enumerate(dets) for gi, g in enumerate(gts)
                    if d["cls"] == g["cls"]), key=lambda t: -t[0])
    for v, di, gi in cands:
        if v < thr or di in used_d or gi in used_g:
            continue
        used_d.add(di)
        used_g.add(gi)
        pairs.append((di, gi, v))
    tp = len(pairs)
    return {"tp": tp, "fp": len(dets) - tp, "fn": len(gts) - tp, "pairs": pairs}
