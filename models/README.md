# 本目录放 Laya 模型（842MB），不进版本库

```bash
huggingface-cli download convaiinnovations/laya --local-dir models/laya
```

下载后 `models/laya/` 应包含：`rl_agent_api.py`、`rl_common.py`、`rl_agent_config.json`、
`model.safetensors`、`encoder/`、`tokenizer/`（作者 API 与权重同目录，`probe_laya.py` 会把该目录插进 `sys.path`）。

放在别处也行：`set LAYA_MODEL_DIR=D:\path\to\laya`（环境变量优先于默认路径）。
