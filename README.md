# mario-decision-lab —— Laya 决策模型 + YOLO 检测器玩超级马里奥

把一个 421M 的「决策契约模型」（[Laya](https://huggingface.co/convaiinnovations/laya)，Jev 类：状态进、候选打分出、不生成文本）接进真实 NES《超级马里奥》1-1 的完整实验工程：真 ROM + 浏览器内 jsnes 模拟器 + YOLO 目标检测 + RAM 语义读取 + 规则层 + 决策模型。

**核心结论（本机实测）**：「AI 玩不好游戏」里大多数是座位/口径在骗你——把决策粒度、按键边沿、坐标口径修对，再把**判断**（危险与否）交给检测器+规则、把**动作**（安全集内挑一个）交给模型之后，分工架构 **400 步 0 掉命**通关 1-1 开局段；而纯模型大脑在同座位下会塌缩成常量动作。

## 架构：五个角色各司其职

```
        ┌────────────────────── jsnes 模拟器（nes/play.html / viewer.html）──────────────────────┐
        │                                                                                        │
  眼睛   │  YOLO 检测器（detect/）      仪表   RAM 语义（smb_state.py）                            │
  ┌─────┴───────────────────────┐      ┌────┴───────────────────────────┐                        │
  │ 画面 256×240 → 生物框         │      │ 0x0086 世界x / 0x071C 相机 /    │                        │
  │ goomba / mario（自监督标注）  │      │ 0x009F 垂直速度 / OAM 敌人…     │                        │
  └─────────────┬───────────────┘      └────────────┬───────────────────┘                        │
                └──────────────┬───────────────────┘                                            │
                               ▼                                                                 │
                     规则层＝裁判（smb_fallback.py）                                              │
                     画面统计+RAM → 坑/障碍/威胁判定 → **安全动作集**                              │
                               ▼                                                                 │
                     模型＝圈里挑的人（probe_laya.py，常驻子进程）                                  │
                     只在安全集内选：危险 ⇒ {小跳,大跳,退后}；安全 ⇒ {冲刺,走}                      │
                               ▼                                                                 │
                     执行层＝手柄（nes_play.py 的 ButtonPulse）                                    │
                     边沿触发修正：每次按键前先松 2 帧；跳有界窗口 30 帧 ──────────┘                │
                               │                                                                 │
                               └──── 按住键 N 帧 → 模拟器推进 ────┘
```

数据流一圈 = 6 帧（决策粒度必须细于一次满跳的飞行时间，详见 `docs/pitfalls.md`）。

## 目录

```
mario-decision-lab/
├─ nes_play.py          主循环：截屏→分析→决策→执行（座位/录制/面板全在这）
├─ smb_state.py         RAM 语义状态读取（每个地址带验证证据，见 docs/ram-map.md）
├─ smb_fallback.py      规则层/兜底脚本（反应式控制器，含全部实测判据）
├─ smb_macro.py         宏决策座位（macro / timing 两种粒度）
├─ smb_actions.py       时序座位的动作词汇表
├─ smb_promptwrap.py    提示词变体包装器（raw/fewshot/binary/table，不改执行层）
├─ probe_laya.py        Laya 模型适配器（--stdin-loop 常驻，协议见下）
├─ stub_adapter.py      假模型（自检链路用，不需要任何权重）
├─ random_adapter.py    随机大脑（下界对照）
├─ nes/                 jsnes 2.1.0（Apache-2.0）+ play.html / viewer.html 封装
├─ roms/                nova.nes（GPL-3.0 开源游戏）；商业 ROM 自备，见 roms/README.md
├─ detect/              YOLO 检测器全流程：OAM 自监督标注→训练→验收→带框录制（detect/README.md）
│  └─ runs/smb/weights/best.pt   训好的检测器（mAP50 0.965）
├─ models/laya/         【需自行下载】Laya 权重+作者 API（842MB，见下）
├─ docs/                架构 / 坑清单 / RAM 地图 / 实测数字
└─ yolov8n.pt           YOLOv8n 预训练权重（重训检测器用）
```

## 环境准备

需要**两个 Python 环境**（依赖互斥，刻意分开）：

```bash
# ① 座位 venv（驱动浏览器）：playwright
python -m venv venv-seat && venv-seat/Scripts/python -m pip install -r requirements-seat.txt
venv-seat/Scripts/python -m playwright install chromium

# ② ML venv（模型与检测）：torch / ultralytics
python -m venv venv-ml && venv-ml/Scripts/python -m pip install -r requirements-ml.txt
```

Laya 权重（842MB，HF 仓库快照，含作者的 `rl_agent_api.py`）：

```bash
huggingface-cli download convaiinnovations/laya --local-dir models/laya
# 或放到别处后：set LAYA_MODEL_DIR=D:\path\to\laya
```

## 快速开始

```bash
# 0. 起静态服务（页面要 fetch jsnes.js 与 ROM），全程保持
python -m http.server 8791 --bind 127.0.0.1

# 1. 自检链路：不用模型，固定按键，验证"按键真的控制了游戏"（开源 ROM，开箱即跑）
venv-seat/Scripts/python nes_play.py --rom /roms/nova.nes --dry-run --cycles 8

# 2. 假模型过一遍完整决策回路（协议自检）
venv-seat/Scripts/python nes_play.py --rom /roms/nova.nes --cycles 6 \
  --adapter "venv-seat/Scripts/python stub_adapter.py"

# 3. 马里奥：纯脚本大脑（规则层完整策略，对照上界）——需自备 ROM，见 roms/README.md
venv-seat/Scripts/python nes_play.py --rom "/roms/Super Mario Bros. (Japan, USA).nes" \
  --macro --macro-mode timing --macro-brain script_guard \
  --pulse-release 0 --jump-window 9999 --cycles 400 --tag script-clear

# 4. 分工架构 + Laya（判断层给安全集，模型在集内挑；带可视化面板+录像）
set HF_HUB_OFFLINE=1 & set TRANSFORMERS_OFFLINE=1 & set USE_TF=0
venv-seat/Scripts/python nes_play.py --rom "/roms/Super Mario Bros. (Japan, USA).nes" \
  --macro --macro-mode timing --macro-brain model \
  --adapter "venv-ml/Scripts/python probe_laya.py --stdin-loop" \
  --cycles 400 --tag laya-split --viewer --record

# 5. 提示词变体实验（不改执行层，只包一层 prompt）
venv-seat/Scripts/python nes_play.py --rom "/roms/Super Mario Bros. (Japan, USA).nes" \
  --macro --macro-mode timing --macro-brain model --cycles 300 \
  --adapter "venv-seat/Scripts/python smb_promptwrap.py --variant fewshot --model-cmd \"venv-ml/Scripts/python probe_laya.py --stdin-loop\"" \
  --tag prompt-fewshot
```

每轮产物落 `nes-play-<tag>/`（逐周期 PNG + `nes-play-<tag>.jsonl` 轨迹 + 可选录像）。
**模型适配器协议**（一行一答，常驻）：stdin `{"state_text":..,"candidates":[{"id","text"},..]}` →
stdout `{"choice":..,"probs":{..}}`。换任何模型只要实现这个协议。

## YOLO 检测器

1200 帧游戏画面，**真值来自模拟器 PPU 的 OAM（精灵内存）自动标注**——零人工标注。
留出集 180 帧：mAP50 0.965 / mAP50-95 0.935；逐帧 IoU≥0.5 比对 goomba F1 0.996、mario F1 0.950。
采集→标注→训练→验收→带框录制全流程与数字见 **`detect/README.md`**。

## 实测结论（详见 docs/results.md）

| 实验 | 结果 |
|---|---|
| 纯模型大脑（Laya，同座位） | 塌缩成常量动作；四档 prompt 手段「无危险窗口跳率」1.00→0.54，单调降但到不了可用 |
| 危险判定两步问答 | 0/20——真值全无危险，它 20/20 报「有危险」 |
| **分工架构（规则判危险 + 模型挑动作）** | **400 步 0 掉命**（small_jump 244 / sprint 156），过旗杆进下一关 |

## 版权与合规红线

- **jsnes**：Apache-2.0（`nes/LICENSE`）；**nova.nes**：GPL-3.0 开源游戏，可自由分发；
- **超级马里奥 ROM 不在本工程内**（任天堂版权），本地自备，勿提交勿分发（`.gitignore` 已拦 `*.nes`，`nova.nes` 例外放行）；
- yolov8n.pt / ultralytics：AGPL-3.0，商用注意；
- 公开发布内容时：商业游戏画面用短片段+标注权利方；本工程产出的数字均为本机自算，引用请标「本机实测」。

## 文档索引

| 文档 | 内容 |
|---|---|
| `docs/architecture.md` | 五角色分工、三种座位、按键执行语义 |
| `docs/pitfalls.md` | 全部坑清单：真机 6 坑 + 座位/口径 7 种失败模式（都会伪装成「模型不行」） |
| `docs/ram-map.md` | SMB RAM 地址表，每个地址带「怎么验证它对」的证据 |
| `docs/results.md` | 全部实测数字（模型硬测量 / 分工架构 / 检测器） |
| `detect/README.md` | 检测器流水线专档 |
| `roms/README.md` | ROM 来源与版权 |
