"""detect/check_video.py — 对成片**抽帧自检**：框是不是真的套在马里奥/板栗仔身上（ML venv 跑）。

  python detect/check_video.py      # 用「ML venv」

做法（不靠肉眼、不靠识图模型，可复算）：
  1. ffmpeg 按时间点抽帧 → `detect/out/frames/t=<秒>s.png`（页面里画布偏移 = wrap padding 14 + border 1）
  2. 在游戏区找**框描边**的连通块（goomba 红 #ff3b30 / mario 绿 #2fff7a）：描边是空心矩形 ⇒ 填充率低
  3. **板栗仔**：以描边框为中心找"棕+黑+桃"的**生物像素块**（只在地面带以上 y<208）→ 比 bbox 偏移
     （dx/dy/dw/dh 全 0 = 框紧贴生物）；再看框内有没有生物像素（0 = 框套空了）
  4. **马里奥**：框内数 #bc1900 红像素（全屏唯一 ⇒ 独立观测量），并检查"全画面红像素包围盒"是否被框包住
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
PAD = 15                                   # 画布在页面中的左上角（wrap padding 14 + border 1）
GW, GH = 512, 480                         # 游戏区（256×240 的 2× 放大）
GROUND_TOP = 416                          # 地面带顶（游戏像素 208 × 2）


def frames_of(mp4: Path, ts: list[float], dst: Path) -> list[Path]:
    dst.mkdir(parents=True, exist_ok=True)
    out = []
    for t in ts:
        p = dst / f"t={t:g}s.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t}", "-i", str(mp4),
                        "-frames:v", "1", str(p)], check=True)
        out.append(p)
    return out


def stroke_boxes(game: np.ndarray, cls: str) -> list[tuple[int, int, int, int]]:
    """找**框描边**（= 空心矩形）。

    颜色必须和画面自带的颜色分开，否则马里奥的红(#bc1900)会被当成 goomba 红框、管道的浅绿会被当成
    mario 绿框。描边色：#ff3b30 = (255,59,48)、#2fff7a = (47,255,122)（h264 后略偏）。判据：
      goomba: r>200, g<115, 25<b<90     ← 马里奥红 r=188 / 板栗仔棕 r≈177 / 地砖棕 都进不来
      mario : g>190, r<120, b>70        ← 管道浅绿 b=0 / 灌木绿 g=171 都进不来
    再加**结构判据**：着色像素必须集中在 bbox 外圈 2px 内（内圈填充率 <0.15）——像素块（马里奥自己、
    板栗仔身体）会被这一步挡掉。
    """
    b, g, r = game[:, :, 0].astype(int), game[:, :, 1].astype(int), game[:, :, 2].astype(int)
    m = ((r > 200) & (g < 115) & (b > 25) & (b < 90)) if cls == "goomba" else \
        ((g > 190) & (r < 120) & (b > 70))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), 8)
    out = []
    for i in range(1, n):
        x, y, w, h, _area = stats[i]
        if not (24 <= w <= 76 and 24 <= h <= 100):
            continue
        sub = (lab[y:y + h, x:x + w] == i)
        ring = int(sub[:3, :].sum() + sub[-3:, :].sum() + sub[:, :3].sum() + sub[:, -3:].sum())
        inner = int(sub[4:-4, 4:-4].sum()) if w > 10 and h > 10 else 0
        total = int(sub.sum())
        if total and ring / total > 0.85 and inner / max(1, (w - 8) * (h - 8)) < 0.15:
            out.append((x, y, w, h))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mp4", default=str(HERE / "out" / "annotated.mp4"))
    ap.add_argument("--ts", default="8,18,28")
    a = ap.parse_args()
    mp4 = Path(a.mp4)
    res = []
    for p in frames_of(mp4, [float(t) for t in a.ts.split(",")], HERE / "out" / "frames"):
        img = cv2.imread(str(p))
        game = img[PAD:PAD + GH, PAD:PAD + GW]
        b, g, r = game[:, :, 0].astype(int), game[:, :, 1].astype(int), game[:, :, 2].astype(int)
        brown = ((r > 130) & (r < 205) & (g > 50) & (g < 110) & (b < 25))   # 板栗仔棕（描边 b=48 排除）
        brown[GROUND_TOP:, :] = False                      # 地面带不算生物
        mred = (r > 150) & (r < 215) & (g < 60) & (b < 25)                   # #bc1900（描边 r=255 排除）
        report = []
        for cls, mask in (("goomba", brown), ("mario", mred)):
            for (x, y, w, h) in stroke_boxes(game, cls):
                inner = mask[y + 2:y + h - 2, x + 2:x + w - 2]
                row = {"cls": cls, "stroke_box": [x, y, w, h], "inside_pixels": int(inner.sum())}
                if cls == "goomba":                        # 框内的棕色像素分布（独立观测量）
                    ys, xs = np.nonzero(inner)
                    if len(xs):
                        row["inside_bbox"] = [int(x + 2 + xs.min()), int(y + 2 + ys.min()),
                                              int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]
                        row["inside_bbox_offset_vs_box"] = [int(x + 2 + xs.min() - x),
                                                            int(y + 2 + ys.min() - y),
                                                            int(xs.max() - xs.min() + 1 - (w - 4)),
                                                            int(ys.max() - ys.min() + 1 - (h - 4))]
                else:
                    n, _l, st, _c = cv2.connectedComponentsWithStats(mred.astype(np.uint8), 8)
                    blobs = [st[i] for i in range(1, n) if st[i][4] >= 20]
                    if blobs:                       # 只取最大的一块：马里奥身上那片红
                        bx, by, bw, bh, _a = max(blobs, key=lambda s: s[4])
                        row["red_bbox"] = [int(bx), int(by), int(bx + bw - 1), int(by + bh - 1)]
                        row["red_bbox_inside_stroke"] = bool(
                            x - 10 <= bx and bx + bw - 1 <= x + w + 10 and
                            y - 10 <= by and by + bh - 1 <= y + h + 10)
                report.append(row)
        res.append({"frame": str(p), "boxes": report})
        print(f"{p.name}  游戏区里的框 {len(report)} 个")
        for row in report:
            extra = ""
            if "inside_bbox_offset_vs_box" in row:
                extra = (f"  框内棕色像素 bbox={row['inside_bbox']}"
                         f"（相对框的偏移={row['inside_bbox_offset_vs_box']}）")
            if "red_bbox_inside_stroke" in row:
                extra = f"  红像素包围盒 {row['red_bbox']} 被框包住={row['red_bbox_inside_stroke']}"
            print(f"    {row['cls']:7s} 描边框@(x={row['stroke_box'][0]},y={row['stroke_box'][1]},"
                  f"{row['stroke_box'][2]}x{row['stroke_box'][3]})  框内该类像素"
                  f" {row['inside_pixels']}{extra}")
    (HERE / "video_check.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=int), encoding="utf-8")


if __name__ == "__main__":
    main()
