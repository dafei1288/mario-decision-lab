"""detect/boxes_ml.py — ML 侧小助手：从 stdin 读 {"png_b64": ...}，往 stdout 回 {"boxes":[...]}。

供座位侧驱动（poetry venv，无 ultralytics）通过子进程调用：一帧一问一答。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import cv2
import numpy as np


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=str(Path(__file__).parent / "runs" / "smb" / "weights" / "best.pt"))
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--imgsz", type=int, default=256)
    a = ap.parse_args()

    from ultralytics import YOLO
    m = YOLO(a.weights)
    print(json.dumps({"ready": True}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        buf = np.frombuffer(base64.b64decode(req["png_b64"]), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            print(json.dumps({"boxes": []}), flush=True)
            continue
        r = m.predict(img, imgsz=a.imgsz, conf=a.conf, verbose=False)[0]
        out = []
        for b in r.boxes:
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0].tolist()]
            out.append({"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1,
                        "cls": m.names[int(b.cls[0])], "conf": float(b.conf[0])})
        print(json.dumps({"boxes": out}), flush=True)


if __name__ == "__main__":
    main()
