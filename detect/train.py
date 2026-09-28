"""detect/train.py — 用自监督标注训练 YOLOv8n（CPU）。必须用 ML venv：

  python detect/train.py      # 用「ML venv」（需 torch/ultralytics）

默认：`data=detect/data.yaml  model=yolov8n.pt  imgsz=256  epochs=40  batch=16  device=cpu`
（imgsz=256 = 画面原生 256×240，不做无意义上采样；CPU 上 batch=16 是实测能跑满核的档）

`--time-budget-min`（默认 22）走 ultralytics 的 `time=`：**到点就在下一个 epoch 边界停**，
所以"请求 40 轮 / 实际跑 N 轮"会在 summary 里如实写出来（CPU 训练的时间墙是硬约束）。
跑完把结果落 `detect/runs/<name>/`，并把关键数字写 `train_summary.json`。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(HERE / "data.yaml"))
    ap.add_argument("--model", default=str(HERE.parent / "yolov8n.pt"))
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--imgsz", type=int, default=256)
    ap.add_argument("--name", default="smb")
    ap.add_argument("--time-budget-min", type=float, default=22.0, help="到点停（0 = 不限）")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    from ultralytics import YOLO
    t0 = time.time()
    model = YOLO(a.model)
    kw = dict(data=a.data, epochs=a.epochs, batch=a.batch, imgsz=a.imgsz, device="cpu",
              project=str(HERE / "runs"), name=a.name, exist_ok=True, seed=0, deterministic=True,
              cache=True, workers=a.workers, plots=True, verbose=True)
    if a.time_budget_min:
        kw["time"] = a.time_budget_min / 60.0
    res = model.train(**kw)
    dt = time.time() - t0
    run_dir = Path(res.save_dir)                    # = detect/runs/<name>

    m = model.val(data=a.data, imgsz=a.imgsz, device="cpu", split="val", plots=False, verbose=False,
                  project=str(run_dir), name="val")       # 留出集评估落回本次训练目录下
    names = m.names
    per_class = []
    for i in sorted(names):
        p, r, ap50, ap = m.box.class_result(i)
        per_class.append({"class": names[i], "precision": round(p, 4), "recall": round(r, 4),
                          "mAP50": round(ap50, 4), "mAP50-95": round(ap, 4)})
    best = run_dir / "weights" / "best.pt"
    summary = {"epochs_requested": a.epochs, "epochs_ran": None,
               "seconds": round(dt, 1), "minutes": round(dt / 60, 2), "imgsz": a.imgsz,
               "batch": a.batch, "model": a.model, "data": a.data,
               "mAP50": round(float(m.box.map50), 4), "mAP50-95": round(float(m.box.map), 4),
               "per_class": per_class,
               "weights": str(best), "runs_dir": str(run_dir)}
    csv = run_dir / "results.csv"
    if csv.exists():                                    # 实际跑了多少轮 = results.csv 行数
        summary["epochs_ran"] = sum(1 for _ in csv.read_text().splitlines()) - 1
    (run_dir / "train_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (HERE / "train_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    print("\n=== 训练完成 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
