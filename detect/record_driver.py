"""detect/record_driver.py — **浏览器侧**（poetry venv，有 playwright）：驱动 detect/boxes.html、录制、落 mp4。

为什么拆成两个进程：ML venv 有 torch/ultralytics 但**没有 playwright**，poetry venv 有 playwright
但没 torch。所以浏览器归本进程，YOLO 推理归 ML 侧（detect/record_boxes.py），中间走 stdio 行 JSON：

  驱动 → stdout   {"event":"ready","viewport":..} / {"event":"frame","i":N,"fc":帧号,"png":"<base64>","ram":[..]}
                  {"event":"done","mp4":"...","webm":"...","seconds":..,"frames":N}
  stdin → 驱动    {"op":"boxes","boxes":[{x,y,w,h,cls,conf}...]}   # 画框（**然后才推下一帧**）
                  {"op":"stop"}                                     # 提前收尾

按键：与 collect.py 同一套脚本（smb_fallback.SmbPolicy，一路 RIGHT+B、坑/障碍前起跳），每帧 `__set(btns,1)`
逐帧推进（跳是边沿触发）。`device_scale_factor=1`（≠1 会录出白帧）、webm→h264 显式 bt709（否则紫偏色）
—— 与 nes_play.py / smb-bot/record_clear.py 的既有做法一致。
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright          # noqa: E402
from nes_play import ACTIONS, png_stats                  # noqa: E402
from smb_fallback import SmbPolicy                       # noqa: E402

BASE = "http://127.0.0.1:8791"
ROM = "/roms/Super Mario Bros. (Japan, USA).nes"
HERE = Path(__file__).resolve().parent
SNAP_JS = """() => ({png: window.__png(), m: [window.__ram(0x0086, 1)[0],
                     window.__ram(0x071C, 1)[0], window.__ram(0x009F, 1)[0]]})"""


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def mux(webm: Path, dst: Path) -> None:
    """webm(VP8/9, BT.601) → h264，显式 bt709（否则紫/绿色偏）。"""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm),
                    "-vf", "scale=out_color_matrix=bt709:out_range=tv",
                    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                    "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(dst)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=1500)
    ap.add_argument("--hold", type=int, default=6)
    ap.add_argument("--warmup", type=int, default=120)
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--viewport", default="940,520")
    a = ap.parse_args()
    vw, vh = (int(v) for v in a.viewport.split(","))
    outdir = Path(a.out)
    viddir = outdir / "video"
    viddir.mkdir(parents=True, exist_ok=True)
    for old in viddir.glob("*.webm"):
        old.unlink()

    pol = SmbPolicy(hold_frames=a.hold)
    t0 = time.time()
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": vw, "height": vh}, device_scale_factor=1,
                            record_video_dir=str(viddir), record_video_size={"width": vw, "height": vh})
        page = ctx.new_page()
        page.on("pageerror", lambda e: print(f"[pageerror] {e}", file=sys.stderr, flush=True))
        page.goto(f"{BASE}/detect/boxes.html")
        page.evaluate("() => new Promise(res => window.__boot('%s', res))" % quote(ROM, safe="/"))
        if not page.evaluate("() => typeof window.__boxes === 'function'"):
            emit({"event": "error", "why": "__boxes 没定义（页面加载失败？）"})
            b.close()
            return
        page.evaluate("([b, n]) => window.__set(b, n)", [["START"], 8])
        page.evaluate("([b, n]) => window.__set(b, n)", [["RIGHT", "B"], a.warmup])
        emit({"event": "ready", "viewport": [vw, vh], "frames": a.frames})

        btns, act = ["RIGHT", "B"], "right_b"
        for i in range(a.frames):
            page.evaluate("([b, n]) => window.__set(b, n)", [btns, 1])
            snap = page.evaluate(SNAP_JS)
            if i % a.hold == 0:
                act, why = pol.decide(png_stats(base64.b64decode(snap["png"].split(",", 1)[1])),
                                      snap["m"][0], cam=snap["m"][1], vy=snap["m"][2])
                btns = list(ACTIONS[act][0])
            emit({"event": "frame", "i": i, "fc": page.evaluate("() => window.__frameCount()"),
                  "png": snap["png"].split(",", 1)[1], "ram": snap["m"], "act": act})
            reply = sys.stdin.readline()
            if not reply:
                break
            msg = json.loads(reply)
            if msg.get("op") == "stop":
                break
            if msg.get("op") == "boxes":
                page.evaluate("(l) => window.__boxes(l)", msg.get("boxes") or [])
            elif msg.get("op") == "ui":
                page.evaluate("(d) => window.__ui(d)", msg.get("data") or {})
        page.close()
        ctx.close()                                    # 关上下文才落盘 webm
        webm = Path(page.video.path())
        b.close()
    dst = outdir / "annotated.mp4"
    mux(webm, dst)
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(dst)], capture_output=True, text=True).stdout.strip()
    emit({"event": "done", "mp4": str(dst), "webm": str(webm),
          "seconds": round(float(dur), 2) if dur else None,
          "wall_seconds": round(time.time() - t0, 1), "frames": a.frames})


if __name__ == "__main__":
    main()
