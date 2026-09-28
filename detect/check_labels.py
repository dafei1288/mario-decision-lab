"""detect/check_labels.py — 自监督标注的**验收工具**：labels/*.txt 到底套没套在画面里的生物上（ML venv）。

  python detect/check_labels.py      # 用「ML venv」

三件事，全部给数字（不靠肉眼、不靠识图模型）：
  ① **时间轴对齐**：画面 i 分别配 OAM(i-1)/OAM(i)/OAM(i+1) 三种标法，比板栗仔框与"画面里非天空
     像素块"的 x 偏移 → 证明该用哪一种（实测：PPU OAM 配同帧 100% 为 0）。
  ② **影子页 vs PPU OAM**：SSMB 的 CPU 影子页 $0200 是"游戏正在写的活数据"，帧末读会撞上中间态。
     对每一帧比两种真值：影子页标法漏掉、PPU 标法有的板栗仔框 → 再去画面里**独立核对**
     （该位置有没有板栗仔的棕色像素块）⇒ 用像素证明是"影子页漏了"，不是"PPU 多标了"。
  ③ **最终 labels vs 画面**：板栗仔（像素块）与马里奥（红像素重心）两条独立观测量。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import oamlabel as OL                                    # noqa: E402

SKY = np.array([0xFF, 0x79, 0x81])         # BGR 天空
RED = np.array([0x00, 0x19, 0xBC])         # BGR #bc1900 马里奥红
GROUND_TOP = 208


def brown_blob(img, px, py, pw, ph, x_pad=10):
    """窗口内"棕色生物像素块"（排除地面带）→ bbox 或 None。板栗仔的独立观测量。"""
    x0, x1 = max(0, px - x_pad), min(256, px + pw + x_pad)
    y0, y1 = max(0, py - 6), min(GROUND_TOP, py + ph + 6)
    win = img[y0:y1, x0:x1].astype(int)
    b, g, r = win[:, :, 0], win[:, :, 1], win[:, :, 2]
    m = ((r > 110) & (r < 215) & (g > 35) & (g < 115) & (b < 70)).astype(np.uint8)
    n, _lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    best, bestov = None, 0
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        bx, by = x0 + x, y0 + y
        ov = max(0, min(bx + w, px + pw) - max(bx, px)) * max(0, min(by + h, py + ph) - max(by, py))
        if area >= 40 and ov > bestov:
            best, bestov = (bx, by, w, h), ov
    return best


def blob(img, px, py, pw, ph, x_pad=10):
    """与框相交的最大非天空连通块（板栗仔贴着天空背景时的通用观测量）。"""
    x0, x1 = max(0, px - x_pad), min(256, px + pw + x_pad)
    y0, y1 = max(0, py - 6), min(GROUND_TOP, py + ph + 6)
    m = (np.abs(img[y0:y1, x0:x1].astype(int) - SKY).sum(2) > 40).astype(np.uint8)
    n, _lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    best, bestov = None, 0
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        bx, by = x0 + x, y0 + y
        ov = max(0, min(bx + w, px + pw) - max(bx, px)) * max(0, min(by + h, py + ph) - max(by, py))
        if area >= 20 and ov > bestov:
            best, bestov = (bx, by, w, h), ov
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1200)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    files = sorted(f.stem for f in (HERE / "oam").glob("*.json"))[:a.n]
    recs = {s: json.loads((HERE / "oam" / f"{s}.json").read_text(encoding="utf-8")) for s in files}

    def boxes(stem, key):
        return OL.boxes_from_oam(recs[stem][key])[0]

    # ---- ① 时间轴对齐（用 PPU OAM）----
    align = {"prev": [], "same": [], "next": []}
    for i in range(1, len(files) - 1):
        img = cv2.imread(str(HERE / "images" / f"{files[i]}.png"))
        for tag, idx in (("prev", i - 1), ("same", i), ("next", i + 1)):
            for b in boxes(files[idx], "oam_ppu"):
                if b["cls"] != "goomba":
                    continue
                bl = blob(img, b["x"], b["y"], b["w"], b["h"])
                if not bl or bl[0] <= b["x"] - 9 or bl[2] > 20 or bl[3] > 24:
                    continue
                align[tag].append(bl[0] - b["x"])

    # ---- ② 影子页(同帧, 不偏移) vs PPU OAM：谁和画面一致 ----
    ppu_win = cpu_win = both = neither = 0
    diff_frames = 0
    for stem in files:
        ppu = [b for b in boxes(stem, "oam_ppu") if b["cls"] == "goomba"]
        cpu = [b for b in boxes(stem, "oam_cpu") if b["cls"] == "goomba"]
        if len(ppu) == len(cpu) and all(
                any(abs(b["x"] - c["x"]) <= 3 and abs(b["y"] - c["y"]) <= 3 for c in cpu) for b in ppu):
            continue                                   # 两法一致，不用判
        if len(cpu) > len(ppu):
            ppu, cpu = cpu, ppu
            flip = True
        else:
            flip = False
        diff_frames += 1
        img = cv2.imread(str(HERE / "images" / f"{stem}.png"))
        for b in ppu:                                  # 多出来的那些框：PPU 有则 cpu 无（或反之）
            if any(abs(b["x"] - c["x"]) <= 3 and abs(b["y"] - c["y"]) <= 3 for c in cpu):
                continue
            p_ok = brown_blob(img, b["x"], b["y"], b["w"], b["h"]) is not None
            if p_ok and flip:
                cpu_win += 1
            elif p_ok:
                ppu_win += 1
            else:
                neither += 1
            break


    # ---- ③ 最终 labels vs 画面 ----
    goomba, mario_inside, mario_n = [], 0, 0
    for stem in files:
        lab = OL.parse_label_file(HERE / "labels" / f"{stem}.txt")
        if not lab:
            continue
        img = cv2.imread(str(HERE / "images" / f"{stem}.png"))
        for b in lab:
            if b["cls"] == "goomba":
                bl = brown_blob(img, int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"]))
                if not bl:
                    continue
                goomba.append({"dx": bl[0] - b["x"], "dy": bl[1] - b["y"],
                               "dw": bl[2] - b["w"], "dh": bl[3] - b["h"]})
            else:
                m = np.abs(img[96:, :].astype(int) - RED).sum(2) < 40
                ys, xs = np.nonzero(m)
                if len(xs) < 5:
                    continue
                cx, cy = xs.mean(), ys.mean() + 96
                mario_n += 1
                mario_inside += int(b["x"] - 4 <= cx <= b["x"] + b["w"] + 4 and
                                    b["y"] - 4 <= cy <= b["y"] + b["h"] + 4)

    def med(v):
        return float(statistics.median(v)) if v else None

    out = {"frames": len(files),
           "alignment_median_dx": {k: med(v) for k, v in align.items()},
           "alignment_dx_zero_rate": {k: (round(sum(1 for d in v if d == 0) / len(v), 3) if v else None)
                                      for k, v in align.items()},
           "alignment_n": {k: len(v) for k, v in align.items()},
           "shadow_vs_ppu": {"frames_differing": diff_frames, "ppu_position_confirmed_by_pixels": ppu_win,
                             "shadow_position_confirmed_by_pixels": cpu_win, "neither": neither,
                             "note": "同帧读 CPU 影子页($0200..) vs PPU OAM：多出来/对不上的那一个框，"
                                     "去画面里看该位置有没有棕色生物像素 → 谁和画面一致"},
           "goomba_n": len(goomba),
           "goomba_median_offset": {k: med([x[k] for x in goomba]) for k in ("dx", "dy", "dw", "dh")}
           if goomba else None,
           "goomba_dx_zero_rate": round(sum(1 for x in goomba if x["dx"] == 0) / len(goomba), 3) if goomba else None,
           "mario_centroid_inside_rate": round(mario_inside / mario_n, 4) if mario_n else None,
           "mario_n": mario_n}
    (HERE / "label_check.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
