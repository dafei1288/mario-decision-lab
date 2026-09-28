# detect/ —— 真 YOLO 检测流水线（自监督 OAM 标注 → 训练 → 检测 → 带框录制）

**真值来源是 OAM**（不是识图模型）；下面每个数字都能用同目录脚本复算。

## 1. 文件与用法

| 文件 | 跑在哪个 python | 干什么 |
|---|---|---|
| `oamlabel.py` | 两个都行（纯标准库） | OAM 256 字节 → 生物分组 → 类别 → YOLO 框（含 4 条实测修正） |
| `probe_tiles.py` | 座位 venv | 量"OAM 分组 ↔ tile ↔ 谁是马里奥"→ `probe/oam.jsonl` |
| `collect.py` | 座位 venv | 驱动 viewer.html 跑 1200 帧 → `images/` `oam/` `labels/` + `data.yaml`；`--relabel` 免重采重算 |
| `check_labels.py` | ML venv | 标注验收（时间轴对齐 / 影子页 vs PPU，全部用画面像素独立核对）→ `label_check.json` |
| `preview_gt.py` | ML venv | 把真值框画回画面（肉眼 QC）→ `preview/gt-*.png` |
| `train.py` | ML venv | YOLOv8n CPU 训练 → `runs/<name>/` + `train_summary.json` |
| `eval.py` | ML venv | 留出集验收：ultralytics val + 逐帧 IoU≥0.5 比对 + 失败帧（`--viz` 出图）→ `eval_report.json` |
| `boxes.html` | 浏览器 `/detect/boxes.html` | 复制 viewer.html 的模拟器封装 + **新增 `window.__boxes(list)`** 叠加框 |
| `record_driver.py` | 座位 venv | 浏览器侧：驱动 boxes.html、逐帧按键、Playwright 录制、webm→mp4(bt709) |
| `record_boxes.py` | ML venv | ML 侧：每帧 YOLO 推理 → 回传框 → 驱动画框 → 再推进一步 |
| `check_video.py` | ML venv | 成片抽帧自检 → `video_check.json` + `out/frames/*.png` |

```bash
# $SEAT = 座位 venv 的 python（playwright）；$ML = ML venv 的 python（torch/ultralytics）
$SEAT detect/probe_tiles.py --frames 600 ; $SEAT detect/collect.py --frames 1200
$ML detect/check_labels.py ; $ML detect/train.py --epochs 20
$ML detect/eval.py --viz ; $ML detect/record_boxes.py --frames 1500 ; $ML detect/check_video.py
```

## 2. 为什么只有 goomba / mario 两类

`smb_state.TILE_KIND` 里**只有板栗仔（0x70..0x73）定过案**，其余 tile（乌龟、蘑菇、道具、得分数字…）
在本仓没有证据链 ⇒ 宁缺毋滥，只标两类：`goomba` = 组里出现 0x70..0x73；
`mario` = 组里 tile 全在 `MARIO_TILES`（**先判 goomba**，因为空白填充 tile 0xFC 两边都有）。其余不标。

实测（1200 帧）：**未定案组 58 个，一个都没标**。tile 直方图（probe 600 帧）显示它们是两个家族：
`(0xEF,0xEF,0xFC,0xFC) ×25`、`(0x85,0x85,0x86,0x86) ×10`，位置在马里奥左上方半空（x≈101..112, y≈137..142），
像得分/特效类——**没定案就不标**。

## 3. 定案规则（4 条，全是量出来的）

1. **0xFC 是空白填充 tile**。马里奥精灵块是 16×32（2 列 × 4 行，小马里奥只用下面 2 行），多余行写 0xFC；
   帧 00161 马里奥块 y=176/184 两行全 0xFC，画面对应 y=177..192 **一片纯天空**。
2. **一个生物会被分组规则切成两组**（马里奥 16×24 + 16×8）。先分类再合并"同类、同列、纵向相邻"的组
   （合并不会把贴在一起的马里奥与板栗仔并起来）。实测合并 819 次。
3. **读满 256 字节 = 64 个精灵槽，不是 64 字节 = 16 槽**。SMB 用满整个 $0200 页且**敌人在高槽位**：
   帧 00465 两只板栗仔在槽 12-17 与 24-29，帧 00463 两只都在槽 34+。
   只看前 16 槽 ⇒ 板栗仔框 **221 →（读满后）738**，无框帧 234 → 82。
   （`smb_state.read_oam` 只读 64 字节，所以那边也看不到第 2 只；本目录不改那个文件。）
4. **真值取 PPU 自己那份 OAM**（`nes.ppu.spriteMem`，= 这一帧渲染用的那份），不是 CPU 影子页 $0200。
   影子页是游戏正在写的活数据，帧末读它拿到的是**下一帧**的位置。两条都存进 `oam/*.json`
   （`oam_ppu` / `oam_cpu`），`--oam-source` 切换；默认 ppu，**不需要任何时间轴对齐**。

框的几何：可见框 = **非空白 tile 条目的包围盒**，宽高各取 min 16px（NES 精灵画在 OAM y+1 处）。

## 4. 标注验收（`check_labels.py` → `label_check.json`）

```
时间轴      : x 偏移为 0 的比例  same 96.8% · prev 16.3% · next 16.2%   (n=252)
影子页对抗  : 同帧两种读法不一致的 112 帧 → 画面像素判定 PPU 对 98、影子页对 1、都不对 13
板栗仔框    : vs 画面棕色像素块 dx 中位 0.0 · dy +1.0 · dw 0.0（dh -10 是掩码只取棕色、脚是浅色）
马里奥框    : 红像素重心落在框内 879/889 = 98.9%
```

## 5. 数据集

- 1200 帧 = 游戏内 **25.0 秒**（60Hz）；驱动脚本 `smb_fallback.SmbPolicy`（一路 RIGHT+B、坑/障碍前起跳）
- `goomba` **738 框 / 出现在 50.3% 的帧**（相邻两只并排走的情形也在）；`mario` 946 框 / 74.2% 的帧
- 无框帧 82（全是死亡/重开过渡：画面里连一个红像素都没有）
- **train 1020 / val 180（85/15，seed 0）**；`dataset/images{labels}/{train,val}`，`data.yaml` 指过去
- 画面**确定性**已验证：两次独立采集的同一批 PNG md5 完全一致（`00001/00600/01199` 三个抽样点）

## 6. 训练（`train.py`，CPU；`runs/smb/train_summary.json`）

```
python detect/train.py --epochs 20 --time-budget-min 24 --name smb
data=detect/data.yaml  model=yolov8n.pt  imgsz=256  batch=16  device=cpu
请求 20 轮 → 实际跑满 35 轮 / 1458.6 s = 24.31 min（ultralytics 的 time= 会按实测每轮耗时重算总轮数）
留出集 180 帧（其中 172 帧有真值、8 帧无真值）

最终 val（best.pt）: mAP50 = 0.9902 · mAP50-95 = 0.9485
  goomba  P 1.000  R 0.9871  mAP50 0.9950  mAP50-95 0.9801
  mario   P 0.9879 R 0.9388  mAP50 0.9855  mAP50-95 0.9169
权重: detect/runs/smb/weights/best.pt
曲线/混淆矩阵: detect/runs/smb/{results.png,BoxPR_curve.png,BoxF1_curve.png,confusion_matrix.png}
```

对照（**修正标注之前**的第一版，权重留在 `runs/smb-v1-16slots/`）：22 轮 / 24.4 min，
mAP50 0.6564 · mAP50-95 0.6324，板栗仔 P 0.3077 / R 0.9412。⇒ 影响最大的不是超参，是**标注读法**。

## 7. 留出集验收（`eval.py` → `eval_report.json`）

```
ultralytics val(conf 0.25, iou 0.5): mAP50 0.9650 · mAP50-95 0.9348
   goomba P 1.000 R 0.991 mAP50 0.995 mAP50-95 0.980 · mario P 1.000 R 0.931 mAP50 0.935 mAP50-95 0.889

逐帧比对（IoU≥0.5 命中，180 帧）
  goomba  TP 113  FP 0    FN 1   P 1.000  R 0.991  F1 0.996
  mario   TP 142  FP 10   FN 5   P 0.934  R 0.966  F1 0.950
  逐帧一致率 172/180 = 95.6%（该帧每类检出数与真值完全一致且无多余框）
  无真值帧误检 0/8
失败帧 8 张（完整清单在 eval_report.json；可视化 preview/fail-*.png）：
  00172 真值2/检出4 (mario 漏1/多3) · 00374 真值4/检出6 (mario 多2) · 00360 真值4/检出4 (mario 漏1/多1)
  00373 真值5/检出5 (mario 漏1/多1) · 00368 同 · 00369 同 · 00371 真值5/检出6 (mario 多1) · 00497 真值2/检出1 (goomba 漏1)
```
失败模式很清楚：**板栗仔已经几乎完美**，剩下的全是马里奥的"重复框/过渡帧瞎猜"（FP 10）。

## 8. 带框录制（`boxes.html` + `record_driver.py` + `record_boxes.py`）

- **`boxes.html`**：复制 viewer.html 的模拟器封装（`__boot/__step/__hold/__set/__png/__ram/__ui`），
  画面区固定 512×480（256×240 的整数 2×），**新增 `window.__boxes(list)`**：把
  `[{x,y,w,h,cls,conf}]`（256×240 画布像素坐标）画成彩色框 —— goomba 红 `#ff3b30`、mario 绿 `#2fff7a`。
  描边**画在生物框外侧**（`strokeRect(x-2,y-2,w+4,h+4)`，线宽 2）⇒ 覆盖的是框外那圈像素、**不压生物**；
  类别标签画在框**上方**（放不下才放下方）。侧栏同步给出结构化清单（坐标/尺寸/置信度），
  这就是"让决策模型看见结构化目标"的接口。
- **两个进程**：ML venv 有 torch 但**没有 playwright** ⇒ 浏览器归 `record_driver.py`（座位 venv）、
  推理归 `record_boxes.py`（ML venv），中间走 stdio 行 JSON。
- **严格帧同步**：`__set(btns,1)` 推 1 帧 → 回传该帧 PNG → YOLO 推理 → `__boxes(...)` 画框 → 再推下一帧。
- 录制：`device_scale_factor=1`（≠1 会录出白帧）、viewport/record_video_size = 940×520、
  webm→h264 显式 bt709（与 `nes_play.py` / 既有做法一致）。
- 结果：**`detect/out/annotated.mp4`** —— 1500 帧（游戏内 25.0 s）、**48 ms/帧**、平均 2.20 框/帧、
  940×520 / 25 fps / 1788 帧 / **时长 71.52 s**（≥20 s ✓）。

### 成片 3 帧自检（`check_video.py`，数字判据）

| 帧 | 框 | 判据 |
|---|---|---|
| `detect/out/frames/t=20s.png` | goomba @(443,393,38×26) + mario @(231,375,50×42) | 框内棕色像素 **109**（bbox 横向偏移 +2）；马里奥红像素包围盒**被框包住 ✓** |
| `detect/out/frames/t=30s.png` | mario @(240,255,34×36) | 框内红像素 62，红包围盒**被框包住 ✓** |
| `detect/out/frames/t=40s.png` | mario @(237,281,38×38) | 框内红像素 135，红包围盒**被框包住 ✓** |

判据说明：描边颜色与画面自带颜色**互斥**（goomba 红框 r>200/b>25 vs 马里奥红 r=188/b=0；
mario 绿框 g>190/b>70 vs 管道浅绿 b=0、灌木绿 g=171），且要求是**空心矩形**（外圈占比 >0.85、内圈填充 <0.15），
所以不会把精灵本身或背景当成框。

## 9. 未确定 / 没做到

1. **只有 goomba / mario 两类**：其余 tile 未定案（未定案组 58 个，见 §2）。蘑菇/乌龟/道具/得分数字一律没标。
2. **0xFC = 空白 tile 是从画面推断的**（那个精灵位置一片纯天空），没去 CHR ROM 核对。
3. **CPU 训练轮数**：请求 20 轮，实际 35 轮（ultralytics 的 `time=` 会按实测每轮耗时把总轮数**重算**到填满
   时间预算）。按实测 ~42 s/轮，**40 轮 ≈ 28 min 会超 25 min 上限**，所以用 24 min 预算把轮数交给它自己定。
4. **马里奥仍有 10 个 FP / 5 个 FN**（重复框、死亡过渡帧瞎猜）；板栗仔漏 1。失败帧 8 张清单见 §7。
5. **时间轴对齐不是 100%**（96.8%）；影子页对抗里还有 13 帧两种读法都对不上画面，没深究。
6. **板栗仔框 dh 偏差**：像素掩码只取棕色、脚是浅色 ⇒ 高度核对系统性偏小（宽度/横向位置是对的）。
7. **成片是慢放的**：每帧约 48 ms 的 CPU 逐帧推理 ⇒ 游戏内 25.0 s 录成 71.5 s。
8. **检测没有接回决策**：只提供了 `__boxes` 接口 + 面板结构化清单，没真正闭环到决策模型。
9. **只测了 conf=0.25 一个点**（IoU 0.5），没做阈值扫描 / NMS 之后的二次校验。
10. **数据分布单一**：只有 1-1 前中段 25 秒（撞死会重开、反复经过同一段），没有后续关卡、没有乌龟/蘑菇/道具。
11. 画面里出现的 32×16 绿管口这类**背景**（nametable，不是精灵）本来就没有 OAM 条目，不会进标注——
    这类"没有精灵的对象"（砖块、管道、金币）**不在本流水线能力范围**。
