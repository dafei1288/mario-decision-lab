"""detect/eval.py — 验收：**留出集**上"YOLO 检测 vs OAM 真值"逐帧比对（必须用 ML venv）。

  python detect/eval.py      # 用「ML venv」（需 torch/ultralytics）

两条独立的度量，都写进 `detect/eval_report.json`：
  ① ultralytics 自己的 val（走 data.yaml 的 val 划分）→ mAP50 / mAP50-95 / 每类 P、R
  ② 本脚本的**逐帧比对**：每帧过模型 → 与 labels/*.txt（= OAM 真值，且已按 ±1 帧对齐）做
     同类贪心 IoU≥0.5 匹配 → 每类 TP/FP/FN → P/R/F1；并给"逐帧一致率"
     （定义：该帧**每类** TP=真值数 且 FP=0，即完全一致），失败帧清单落盘。
`--viz` 额外把失败帧画出来（GT 原色 / 检出青色虚框）便于肉眼复核。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import oamlabel as OL                                    # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=str(HERE / "runs" / "smb" / "weights" / "best.pt"))
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--iou-thr", type=float, default=0.5)
    ap.add_argument("--imgsz", type=int, default=256)
    ap.add_argument("--viz", action="store_true")
    a = ap.parse_args()

    import yaml
    from ultralytics import YOLO
    data = yaml.safe_load((HERE / "data.yaml").read_text(encoding="utf-8"))
    names = {int(k): v for k, v in data["names"].items()} if isinstance(data["names"], dict) \
        else dict(enumerate(data["names"]))
    split = json.loads((HERE / "split.json").read_text(encoding="utf-8"))
    img_dir = HERE / "dataset" / "images" / "val"
    lab_dir = HERE / "dataset" / "labels" / "val"

    model = YOLO(a.weights)
    print(f"权重 {a.weights}\n留出集 {len(split['val'])} 帧（{img_dir}）")
    m = model.val(data=str(HERE / "data.yaml"), imgsz=a.imgsz, device="cpu", split="val",
                  conf=a.conf, iou=0.5, plots=False, verbose=False,
                  project=str(HERE / "runs"), name="eval")
    up = {names[i]: {"precision": round(float(m.box.p[i]), 4), "recall": round(float(m.box.r[i]), 4),
                     "mAP50": round(float(m.box.ap50[i]), 4), "mAP50-95": round(float(m.box.ap[i]), 4)}
          for i in range(len(names))}
    print(f"\n[ultralytics val] mAP50={m.box.map50:.4f} mAP50-95={m.box.map:.4f}")
    for k, v in up.items():
        print(f"  {k:7s} P={v['precision']:.3f} R={v['recall']:.3f} "
              f"mAP50={v['mAP50']:.3f} mAP50-95={v['mAP50-95']:.3f}")

    # ---- 逐帧比对（IoU≥0.5 命中）----
    stat = {c: {"tp": 0, "fp": 0, "fn": 0} for c in OL.CLASSES}
    consistent = 0
    n_gt_frames = n_frames = 0
    empty_frames = empty_fp = 0
    failures = []
    for stem in split["val"]:
        img = cv2.imread(str(img_dir / f"{stem}.png"))
        gt = OL.parse_label_file(lab_dir / f"{stem}.txt")
        r = model.predict(img, imgsz=a.imgsz, device="cpu", conf=a.conf, verbose=False)[0]
        dets = [{"cls": names[int(cl)], "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1, "conf": cf}
                for (x1, y1, x2, y2), cf, cl in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(),
                                                    r.boxes.cls.tolist())
                if int(cl) in names]            # 本数据集只有 2 类；别的类别 id 一律不算
        ok_all, detail = True, {}
        for c in OL.CLASSES:
            sub = OL.match([d for d in dets if d["cls"] == c], [g for g in gt if g["cls"] == c],
                           a.iou_thr)
            for k in ("tp", "fp", "fn"):
                stat[c][k] += sub[k]
            detail[c] = {"gt": sum(1 for g in gt if g["cls"] == c), "det": sum(1 for d in dets if d["cls"] == c),
                         "missed": sub["fn"], "spurious": sub["fp"]}
            if sub["tp"] != detail[c]["gt"] or sub["fp"]:
                ok_all = False
        n_frames += 1
        n_gt_frames += 1 if gt else 0
        if not gt:
            empty_frames += 1
            empty_fp += 1 if dets else 0
        if ok_all:
            consistent += 1
        else:
            failures.append({"frame": stem, "gt_boxes": len(gt), "det_boxes": len(dets),
                             "detail": detail, "image": str(img_dir / f"{stem}.png")})
    per_class = {}
    for c, s in stat.items():
        p = s["tp"] / (s["tp"] + s["fp"]) if s["tp"] + s["fp"] else 0.0
        rr = s["tp"] / (s["tp"] + s["fn"]) if s["tp"] + s["fn"] else 0.0
        per_class[c] = {**s, "precision": round(p, 4), "recall": round(rr, 4),
                        "f1": round(2 * p * rr / (p + rr), 4) if p + rr else 0.0}

    print(f"\n[逐帧比对 · IoU≥{a.iou_thr} 命中] 留出集 {n_frames} 帧"
          f"（有真值帧 {n_gt_frames} · 无真值帧 {empty_frames}）")
    for c, v in per_class.items():
        print(f"  {c:7s} TP={v['tp']:4d} FP={v['fp']:4d} FN={v['fn']:4d} "
              f"P={v['precision']:.3f} R={v['recall']:.3f} F1={v['f1']:.3f}")
    print(f"  逐帧一致率 {consistent}/{n_frames} = {consistent / n_frames * 100:.1f}%"
          f"（定义：该帧每类检出数与真值完全一致且无多余框）")
    print(f"  无真值帧误检 {empty_fp}/{empty_frames}")
    print(f"\n失败帧 {len(failures)} 张（按偏差最大的前 8 张）：")
    for f in sorted(failures, key=lambda f: -sum(d["missed"] + d["spurious"]
                                                 for d in f["detail"].values()))[:8]:
        d = f["detail"]
        print(f"  {f['image']}  真值 {f['gt_boxes']} / 检出 {f['det_boxes']}"
              f"  goomba(漏{d['goomba']['missed']}/多{d['goomba']['spurious']})"
              f"  mario(漏{d['mario']['missed']}/多{d['mario']['spurious']})")

    out = {"weights": a.weights, "conf": a.conf, "iou_thr": a.iou_thr, "val_frames": n_frames,
           "val_frames_with_gt": n_gt_frames, "frame_consistency": round(consistent / n_frames, 4),
           "empty_gt_frames": empty_frames, "empty_gt_false_positive_frames": empty_fp,
           "per_class_iou_match": per_class, "ultralytics_val": {
               "mAP50": round(float(m.box.map50), 4), "mAP50_95": round(float(m.box.map), 4),
               "per_class": up}, "failures": failures}
    (HERE / "eval_report.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                           encoding="utf-8")

    if a.viz:
        viz = HERE / "preview"
        viz.mkdir(exist_ok=True)
        rank = sorted(failures, key=lambda f: -sum(d["missed"] + d["spurious"]
                                                   for d in f["detail"].values()))[:6]
        for f in rank:
            stem = f["frame"]
            img = cv2.imread(str(img_dir / f"{stem}.png"))
            img = cv2.resize(img, (512, 480), interpolation=cv2.INTER_NEAREST)
            for g in OL.parse_label_file(lab_dir / f"{stem}.txt"):
                c = (60, 60, 255) if g["cls"] == "goomba" else (80, 255, 80)
                cv2.rectangle(img, (int(g["x"] * 2), int(g["y"] * 2)),
                              (int((g["x"] + g["w"]) * 2), int((g["y"] + g["h"]) * 2)), c, 2)
            r = model.predict(cv2.imread(str(img_dir / f"{stem}.png")), imgsz=a.imgsz,
                              device="cpu", conf=a.conf, verbose=False)[0]
            for (x1, y1, x2, y2), cf in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist()):
                cv2.rectangle(img, (int(x1 * 2), int(y1 * 2)), (int(x2 * 2), int(y2 * 2)),
                              (255, 210, 0), 1)
            cv2.imwrite(str(viz / f"fail-{stem}.png"), img)
        print(f"\n失败帧可视化（真值粗框 / 检出细黄框）→ {viz}/fail-*.png  {len(rank)} 张")


if __name__ == "__main__":
    main()
