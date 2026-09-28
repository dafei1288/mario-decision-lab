"""detect/preview_gt.py — 把 OAM 真值框画回画面上，肉眼核对"框真的套在生物身上"（自监督标注的 QC）。

用 ML venv（有 cv2/PIL）：
  python detect/preview_gt.py --n 8      # 用「ML venv」

输入 detect/images/NNNNN.png + detect/labels/NNNNN.txt（外加同帧 oam/NNNNN.json 打印原始 OAM 做证据）。
输出 detect/preview/gt-NNNNN.png。**标注真值始终是 OAM**，本工具只做可视化核对。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import oamlabel as OL                                    # noqa: E402

COLOR = {"goomba": (60, 60, 255), "mario": (80, 255, 80)}          # BGR：goomba 红 / mario 绿


def draw(png: Path, boxes: list[dict], out: Path) -> None:
    img = cv2.imread(str(png), cv2.IMREAD_COLOR)
    img = cv2.resize(img, (512, 480), interpolation=cv2.INTER_NEAREST)     # 2× 放大便于肉眼核对
    for b in boxes:
        x, y, w, h = (int(round(b["x"] * 2)), int(round(b["y"] * 2)),
                      int(round(b["w"] * 2)), int(round(b["h"] * 2)))
        c = COLOR[b["cls"]]
        cv2.rectangle(img, (x, y), (x + w, y + h), c, 2)
        txt = f"{b['cls']} {b['w']}x{b['h']}"
        ty = y - 6 if y > 14 else y + h + 16
        cv2.putText(img, txt, (x, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
        cv2.putText(img, txt, (x, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.5, c, 1)
    cv2.imwrite(str(out), img)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--goomba-only", action="store_true")
    a = ap.parse_args()
    outdir = HERE / "preview"
    outdir.mkdir(exist_ok=True)

    lab = sorted((HERE / "labels").glob("*.txt"))
    pick = []
    for f in lab:
        boxes = OL.parse_label_file(f)
        if a.goomba_only and not any(b["cls"] == "goomba" for b in boxes):
            continue
        pick.append((f.stem, boxes))
    if not pick:
        sys.exit("没有标注文件（先跑 collect.py）")
    rnd = random.Random(a.seed)
    rnd.shuffle(pick)
    show = pick[:a.n]

    for stem, boxes in show:
        out = outdir / f"gt-{stem}.png"
        draw(HERE / "images" / f"{stem}.png", boxes, out)
        rec = json.loads((HERE / "oam" / f"{stem}.json").read_text(encoding="utf-8"))
        raw = rec.get("oam_ppu") or rec.get("oam")
        oam = [f"(slot={i // 4},x={raw[i + 3]},y={raw[i]},tile=0x{raw[i + 1]:02X})"
               for i in range(0, min(len(raw), 256), 4) if 40 <= raw[i] < 240]
        print(f"{out.relative_to(HERE.parent)}  " +
              " | ".join(f"{b['cls']} x={b['x']:.0f} y={b['y']:.0f} {b['w']:.0f}x{b['h']:.0f}"
                         for b in boxes))
        print(f"    OAM: {' '.join(oam)}")


if __name__ == "__main__":
    main()
