"""detect/record_boxes.py — **ML 侧**（laya venv，有 ultralytics）：每帧 YOLO 推理 → 交驱动画框 → 录制。

  python detect/record_boxes.py      # 用「ML venv」（需 torch/ultralytics）

流程（严格帧同步，一帧一个来回）：
  驱动 `__set(btns,1)` 推进 1 帧 → 回传该帧 PNG(base64) →
  **本进程 YOLO 推理**（imgsz=256，conf 0.25）→ 回 `{"op":"boxes",[{x,y,w,h,cls,conf}]}`（256×240 像素坐标）
  → 驱动 `window.__boxes(...)` 画在**叠加层**上 → 再推下一帧。
浏览器在另一个进程里（`detect/record_driver.py`，poetry venv），因为 ML venv 没有 playwright。
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
DEFAULT_DRIVER_PY = "python"  # 浏览器侧解释器（需装 playwright）；用 --driver-python 指定


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=str(HERE / "runs" / "smb" / "weights" / "best.pt"))
    ap.add_argument("--frames", type=int, default=1500)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--imgsz", type=int, default=256)
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--driver-python", default=DEFAULT_DRIVER_PY)
    a = ap.parse_args()

    from ultralytics import YOLO
    model = YOLO(a.weights)
    model.predict(np.zeros((240, 256, 3), np.uint8), imgsz=a.imgsz, device="cpu",
                  conf=a.conf, verbose=False)                      # 预热：第一次调用最慢
    names = model.names
    n_boxes, n_frames, hist = 0, 0, {}
    t0 = time.time()

    proc = subprocess.Popen([a.driver_python, str(HERE / "record_driver.py"),
                             "--frames", str(a.frames), "--out", a.out],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            encoding="utf-8", bufsize=1)
    try:
        for line in proc.stdout:
            msg = json.loads(line)
            if msg.get("event") == "ready":
                print(f"驱动就绪 viewport={msg['viewport']} 目标帧数={msg['frames']}", flush=True)
                continue
            if msg.get("event") == "done":
                print(f"\n录制完成 → {msg['mp4']}\n  时长 {msg['seconds']}s · 帧 {msg['frames']} · "
                      f"推理墙钟 {msg['wall_seconds']}s", flush=True)
                print(f"  平均每帧检出 {n_boxes / max(1, n_frames):.2f} 个目标；检出分布 {hist}")
                break
            if msg.get("event") != "frame":
                print(f"[driver] {msg}", flush=True)
                continue
            img = cv2.imdecode(np.frombuffer(base64.b64decode(msg["png"]), np.uint8), cv2.IMREAD_COLOR)
            r = model.predict(img, imgsz=a.imgsz, device="cpu", conf=a.conf, verbose=False)[0]
            boxes = []
            for xyxy, cf, cl in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist()):
                x1, y1, x2, y2 = xyxy
                boxes.append({"x": round(x1, 1), "y": round(y1, 1), "w": round(x2 - x1, 1),
                              "h": round(y2 - y1, 1), "cls": names[int(cl)], "conf": round(cf, 3)})
            proc.stdin.write(json.dumps({"op": "boxes", "boxes": boxes}) + "\n")
            proc.stdin.flush()
            n_frames += 1
            n_boxes += len(boxes)
            hist[len(boxes)] = hist.get(len(boxes), 0) + 1
            if n_frames % 100 == 0:
                print(f"  {n_frames} 帧 · {(time.time() - t0) / n_frames * 1000:.0f}ms/帧 · "
                      f"本帧 {len(boxes)} 框 · frame#{msg['fc']} ram(lx,cam,vy)={msg['ram']}", flush=True)
    finally:
        try:
            proc.stdin.write(json.dumps({"op": "stop"}) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
        proc.wait(timeout=180)


if __name__ == "__main__":
    main()
