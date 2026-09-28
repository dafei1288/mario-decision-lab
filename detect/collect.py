"""detect/collect.py — 采集 + 自监督标注（真值 = OAM），用 poetry venv 的 python（有 playwright）。

  python detect/collect.py --frames 1200      # 用「座位 venv」（需 playwright）

每帧（**同一帧**内取画面与内存，不中途 step）：
  ① `__png()`        → detect/images/NNNNN.png   （256×240，就是 YOLO 要学的画面）
  ② OAM              → detect/oam/NNNNN.json      （标注的**真值来源**，两条都存，见下）
  ③ OAM → YOLO 标注  → detect/labels/NNNNN.txt    （规则见 oamlabel.py）

**真值取 PPU 自己那份 OAM**（`nes.ppu.spriteMem[0:64]`，= 这一帧渲染时用的那份），不是 CPU 的影子页
$0200..$023F。影子页是**游戏正在写的活数据**，帧末读它会撞上"还没写完"的中间态，实测：
  * 帧 00465：影子里一只板栗仔都没有，画面里明明有两只在走；
  * 帧 00161/00680 这类：影子页把板栗仔写在 y=248（=隐藏），而 PPU 那份是 y=184（=画面上那只）。
两条都存进 json（`oam_ppu` / `oam_cpu`），`--oam-source` 选谁当标注真值（默认 `ppu`）。
用 ppu 还有一个好处：**不需要再按 ±1 帧对齐**（影子的位置比画面早一帧，实测见 report）。

**读满 256 字节 = 64 个精灵槽**（不是 64 字节 = 16 槽）。SMB 用满整个 $0200 页，把马里奥放前几槽、
敌人往后放；只读前 16 槽会**整只漏掉第 2 只板栗仔**（实测帧 00465：画面 x≈49 与 x≈65 两只并排走，
前 16 槽里只有第 1 只）。`smb_state.read_oam` 只读 64 字节，所以那边也看不到它——本目录不改那个文件，
在 detect/ 里读满。

按键由 `smb_fallback.SmbPolicy`（已验证能跑通 1-1 的反应式脚本：一路 RIGHT+B、坑/障碍前起跳）决定，
每 `--hold` 帧决策一次；执行用 `__set(btns, 1)` **逐帧**推进（跳是边沿触发，必须逐帧看得见按键变化）。

`--relabel` 不走浏览器：只用已存的 oam/*.json 重算 labels/ 与 dataset/（改规则后免重采）。
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import shutil
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
IMAGES, OAMD, LABELS, DATASET = HERE / "images", HERE / "oam", HERE / "labels", HERE / "dataset"
SNAP_JS = """() => ({png: window.__png(), oam: window.__ram(0x0200, 256),
                     oam_ppu: Array.from(window.__nes.ppu.spriteMem),
                     m: [window.__ram(0x0086, 1)[0], window.__ram(0x071C, 1)[0],
                         window.__ram(0x009F, 1)[0]]})"""
WARMUP = 120                       # 先跑 120 帧离开标题画面（标题画面也有马里奥精灵，但不算关卡数据）


def capture(frames: int, hold: int, warmup: int) -> None:
    for d in (IMAGES, OAMD, LABELS):
        d.mkdir(parents=True, exist_ok=True)
        for old in d.iterdir():
            old.unlink()
    pol = SmbPolicy(hold_frames=hold)
    t0 = time.time()
    diff = 0
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page(viewport={"width": 512, "height": 480}, device_scale_factor=1)
        page.goto(f"{BASE}/nes/viewer.html")
        page.evaluate("() => new Promise(res => window.__boot('%s', res))" % quote(ROM, safe="/"))
        page.evaluate("([b, n]) => window.__set(b, n)", [["START"], 8])
        page.evaluate("([b, n]) => window.__set(b, n)", [["RIGHT", "B"], warmup])

        btns, act = ["RIGHT", "B"], "right_b"
        for i in range(frames):
            snap = page.evaluate(SNAP_JS)
            png = base64.b64decode(snap["png"].split(",", 1)[1])
            (IMAGES / f"{i:05d}.png").write_bytes(png)
            lx, cam, vy = snap["m"]
            diff += 0 if snap["oam"] == snap["oam_ppu"] else 1
            (OAMD / f"{i:05d}.json").write_text(json.dumps(
                {"frame": i, "oam_ppu": snap["oam_ppu"], "oam_cpu": snap["oam"],
                 "lx": lx, "cam": cam, "vy": vy, "act": act}), encoding="utf-8")
            if i % hold == 0:                       # 决策周期：只看当前帧（与画面同帧）
                act, why = pol.decide(png_stats(png), lx, cam=cam, vy=vy)
                btns = list(ACTIONS[act][0])
            page.evaluate("([b, n]) => window.__set(b, n)", [btns, 1])
            if i % 200 == 0:
                print(f"  采集 {i:4d}/{frames} act={act:9s} level_x={lx:3d} cam={cam:3d} "
                      f"({time.time() - t0:.0f}s)", flush=True)
        b.close()
    print(f"采集完成：{frames} 帧 / {time.time() - t0:.0f}s → {IMAGES}")
    print(f"  其中 CPU 影子页 $0200 与 PPU OAM 不一致的帧：{diff}/{frames}")


def label_and_split(val_frac: float = 0.15, seed: int = 0, source: str = "ppu") -> dict:
    """已存 OAM → labels/ + dataset/（train/val）+ data.yaml。

    `source="ppu"`（默认）：用 **PPU 自己那份 OAM**（`nes.ppu.spriteMem[0:64]`）标同帧画面 —— 它就是
    这一帧渲染时用的那份，**不需要任何时间轴对齐**。实测（check_labels.py，82 个干净样本）配同帧
    的 x 偏移 100% 为 0。

    `source="cpu"`：用 CPU 影子页 $0200..$023F，必须**拿上一帧的 OAM**（SMB 在 NMI 里 DMA 的是下一帧的
    位置）；这样只有 96.3% 的 x 偏移为 0，而且影子页会撞上"敌人槽还没写完"的中间态：
    实测帧 00465 影子里一只板栗仔都没有（画面里有两只在走）、帧 00680 影子里板栗仔在 y=248（隐藏）
    而 PPU 那份是 y=184。⇒ 影子页当标注真值会让部分板栗仔**整只丢掉**（模型检测到了反而算误检）。
    """
    files = sorted(f.stem for f in OAMD.glob("*.json"))
    if not files:
        sys.exit("没有任何 oam/*.json —— 先跑采集（去掉 --relabel）")
    LABELS.mkdir(exist_ok=True)
    key = "oam_ppu" if source == "ppu" else "oam_cpu"
    shift = 0 if source == "ppu" else 1
    stats = {"frames": len(files), "boxes": {c: 0 for c in OL.CLASSES},
             "frames_with": {c: 0 for c in OL.CLASSES}, "unknown_groups": 0, "clipped": 0,
             "empty_frames": 0, "merged": 0, "blank_only": 0, "source": source,
             "shadow_differs_frames": 0, "shadow_only_boxes": 0}
    oam_of = {}
    for s in files:
        rec = json.loads((OAMD / f"{s}.json").read_text(encoding="utf-8"))
        oam_of[s] = rec.get(key) or rec.get("oam")           # 兼容只存了一条的旧 json
        if rec.get("oam_ppu") and rec.get("oam_cpu") and rec["oam_ppu"] != rec["oam_cpu"]:
            stats["shadow_differs_frames"] += 1
    for idx, stem in enumerate(files):
        src = files[idx - shift] if idx >= shift else stem
        boxes, st = OL.boxes_from_oam(oam_of[src])
        for k in ("unknown_groups", "clipped", "merged", "blank_only"):
            stats[k] += st[k if k != "unknown_groups" else "unknown"]
        (LABELS / f"{stem}.txt").write_text(OL.labels_text(boxes), encoding="utf-8")
        for b in boxes:
            stats["boxes"][b["cls"]] += 1
        for c in OL.CLASSES:
            if any(b["cls"] == c for b in boxes):
                stats["frames_with"][c] += 1
        if not boxes:
            stats["empty_frames"] += 1
        # 影子页标法会丢多少（只有两条 OAM 都存了才算）
        rec = json.loads((OAMD / f"{stem}.json").read_text(encoding="utf-8"))
        if rec.get("oam_cpu"):
            cpu_src = files[idx - 1] if idx else stem
            cpu_boxes = OL.boxes_from_oam(
                json.loads((OAMD / f"{cpu_src}.json").read_text(encoding="utf-8")).get("oam_cpu"))[0]
            stats["shadow_only_boxes"] += sum(1 for b in cpu_boxes if b["cls"] == "goomba") - \
                sum(1 for b in boxes if b["cls"] == "goomba")

    rnd = random.Random(seed)
    order = sorted(files, key=lambda s: rnd.random())
    n_val = max(1, round(len(order) * val_frac))
    val, train = order[:n_val], order[n_val:]
    for split in ("train", "val"):
        for sub in (DATASET / "images" / split, DATASET / "labels" / split):
            sub.mkdir(parents=True, exist_ok=True)
            for old in sub.iterdir():
                old.unlink()
    for stem in train + val:
        split = "val" if stem in set(val) else "train"
        shutil.copyfile(IMAGES / f"{stem}.png", DATASET / "images" / split / f"{stem}.png")
        shutil.copyfile(LABELS / f"{stem}.txt", DATASET / "labels" / split / f"{stem}.txt")
    (HERE / "split.json").write_text(json.dumps(
        {"seed": seed, "val_frac": val_frac, "train": train, "val": val}, indent=1), encoding="utf-8")
    names = "\n".join(f"  {i}: {c}" for i, c in enumerate(OL.CLASSES))
    (HERE / "data.yaml").write_text(
        f"# 自监督标注：真值 = OAM（见 oamlabel.py）。classes 只有两类，其余 tile 未定案 ⇒ 不标。\n"
        f"path: {DATASET.as_posix()}\ntrain: images/train\nval: images/val\n"
        f"nc: {len(OL.CLASSES)}\nnames:\n{names}\n", encoding="utf-8")
    stats["train"] = len(train)
    stats["val"] = len(val)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=1200)
    ap.add_argument("--hold", type=int, default=6)
    ap.add_argument("--warmup", type=int, default=WARMUP)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--relabel", action="store_true", help="不驱动浏览器，只从已存 OAM 重算标注与数据集")
    ap.add_argument("--oam-source", choices=("ppu", "cpu"), default="ppu",
                    help="ppu=PPU 自己那份 OAM（渲染用的，默认）；cpu=CPU 影子页 $0200（需按上一帧对齐）")
    a = ap.parse_args()

    if not a.relabel:
        capture(a.frames, a.hold, a.warmup)
    st = label_and_split(a.val_frac, a.seed, a.oam_source)
    f, n = st["frames"], st["frames"] or 1
    print(f"\n标注完成：{f} 帧（train {st['train']} / val {st['val']}）· 真值 = {st['source']} OAM")
    for c in OL.CLASSES:
        print(f"  {c:7s} 框 {st['boxes'][c]:5d}（{st['boxes'][c] / n:.2f}/帧）"
              f" 出现帧 {st['frames_with'][c]:5d}（{st['frames_with'][c] / n * 100:.1f}%）")
    print(f"  未定案组 {st['unknown_groups']} · 同类合并的组 {st['merged']} · 只有空白行 {st['blank_only']}"
          f" · 越界裁剪 {st['clipped']} · 无框帧 {st['empty_frames']}")
    print(f"  影子页($0200)与 PPU OAM 不同的帧 {st['shadow_differs_frames']}/{f}"
          f" · 影子页标法比 PPU 标法少的板栗仔框 {st['shadow_only_boxes']}")
    print(f"  → {HERE / 'data.yaml'}")


if __name__ == "__main__":
    main()
