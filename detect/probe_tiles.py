"""detect/probe_tiles.py — 量一遍"OAM 分组 ↔ tile ↔ 谁是马里奥"，为标注规则定案拿证据。

用 poetry venv 的 python（有 playwright）：
  python detect/probe_tiles.py --frames 600      # 用「座位 venv」（需 playwright）

做的事：驱动 viewer.html 跑一段（按键由 smb_fallback.SmbPolicy 决定，与 collect.py 同一套脚本），
每帧记 RAM 0x0086/0x071C/0x009F + **PPU 的整份 OAM 256 字节（64 个精灵槽）** → detect/probe/oam.jsonl。
**不训练、不标注**，只是把规则要靠的原始数字落盘（tile 直方图 / 谁是马里奥 / 分组是否切碎）。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright          # noqa: E402
from nes_play import ACTIONS, png_stats                  # noqa: E402
from smb_fallback import SmbPolicy                       # noqa: E402
import oamlabel as OL                                    # noqa: E402

BASE = "http://127.0.0.1:8791"
ROM = "/roms/Super Mario Bros. (Japan, USA).nes"
HERE = Path(__file__).parent
SNAP_JS = """() => ({png: window.__png(),
                     oam: Array.from(window.__nes.ppu.spriteMem),
                     m: [window.__ram(0x0086, 1)[0], window.__ram(0x071C, 1)[0],
                         window.__ram(0x009F, 1)[0]]})"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--hold", type=int, default=6)
    a = ap.parse_args()

    outdir = HERE / "probe"
    outdir.mkdir(parents=True, exist_ok=True)
    log_path = outdir / "oam.jsonl"
    pol = SmbPolicy(hold_frames=a.hold)
    n_goomba = n_mario = n_unknown = 0
    t0 = time.time()

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page(viewport={"width": 512, "height": 480}, device_scale_factor=1)
        page.goto(f"{BASE}/nes/viewer.html")
        page.evaluate("() => new Promise(res => window.__boot('%s', res))" % quote(ROM, safe="/"))
        page.evaluate("([b, n]) => window.__set(b, n)", [["START"], 8])

        with log_path.open("w", encoding="utf-8") as log:
            btns, act, png = ["RIGHT", "B"], "right_b", None
            for f in range(a.frames):
                snap = page.evaluate(SNAP_JS)
                png = snap["png"]
                lx, cam, vy = snap["m"]
                boxes, st = OL.boxes_from_oam(snap["oam"])
                n_goomba += sum(1 for x in boxes if x["cls"] == "goomba")
                n_mario += sum(1 for x in boxes if x["cls"] == "mario")
                n_unknown += st["unknown"]
                log.write(json.dumps({"f": f, "lx": lx, "cam": cam, "vy": vy, "oam": snap["oam"],
                                      "boxes": [{k: x[k] for k in ("cls", "x", "y", "w", "h")} for x in boxes],
                                      "n_unknown": st["unknown"], "act": act}) + "\n")
                if f % a.hold == 0:                     # 决策周期：与 collect.py 同一策略
                    stats = png_stats(base64.b64decode(png.split(",", 1)[1]))
                    act, why = pol.decide(stats, lx, cam=cam, vy=vy)
                    btns = list(ACTIONS[act][0])
                page.evaluate("([b, n]) => window.__set(b, n)", [btns, 1])
                if f % 100 == 0:
                    print(f"  f={f:4d} act={act:9s} lx={lx:3d} cam={cam:3d} "
                          f"goomba={n_goomba} mario={n_mario} unknown组={n_unknown} "
                          f"({time.time() - t0:.0f}s)")
        b.close()
    print(f"\n探针完成 → {log_path}   {a.frames} 帧 / {time.time() - t0:.0f}s")
    print(f"  累计 goomba 框 {n_goomba} · mario 框 {n_mario} · 未定案组 {n_unknown}")


if __name__ == "__main__":
    main()
