"""Probe: convaiinnovations/laya (English root checkpoint) on the game-probe platformer cases.

Route B: HF root English checkpoint, loaded through the author's own rl_agent_api.RLAgent.system_one
(Jev-compatible signature). Contract: scratch/game-probe/README.md  (lane B = in-memory state text).

Usage:
  python probe_laya.py --control none     --out results-laya.json
  python probe_laya.py --control nostate  --out results-laya-nostate.json
"""
import argparse
import hashlib
import json
import os
import random
import re
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.environ.get("LAYA_MODEL_DIR") or os.path.join(HERE, "models", "laya")
CASES = os.path.join(HERE, "cases", "cases.jsonl")

INSTRUCTIONS = "pick the single best immediate action"
ACTION_ORDER = ["right", "left", "jump", "right_jump", "wait"]
MAP_ROW = re.compile(r"^[.#=GE@]{32}$")


# --------------------------------------------------------------- control group
def nostate(text):
    """Control group. MUST stay byte-identical to the unified runner's batch_probe.strip_state() so that
    self-run files and harness-run files are the same prompt: drop every pure-map line, rewrite the player line."""
    out = []
    for ln in text.splitlines():
        if ln and set(ln) <= set(".#=@EG"):
            continue
        if ln.startswith("player:"):
            out.append("player: unknown state")
            continue
        out.append(ln)
    return "\n".join(out) + "\n"


def questions_from(cands):
    """cands: [{id, text}, ...] -> the single Choice question the contract mandates."""
    return {"action": {"type": "choice", "instructions": INSTRUCTIONS,
                       "criteria": {c["id"]: c["text"] for c in cands}}}


def questions_for(case):
    return questions_from(case["candidates"])


# --------------------------------------------------------------- prompt geometry
def measure_truncation(tok, state, crit, cfg):
    """Exact, via the author's own build_sequence: how many state tokens/chars survive."""
    from rl_common import build_sequence, render_options
    q = {"t": "choice", "ins": INSTRUCTIONS, "crit": crit}
    mask = tok.mask_token
    st = tok(state.replace(mask, " "), add_special_tokens=False)["input_ids"]
    full, _ = build_sequence(tok, state, q, 10 ** 6, cfg["head_max_len"])
    prefix_len = len(full) - 1 - len(st)
    room = max(0, cfg["max_len"] - prefix_len - 1)
    kept = min(len(st), room)
    enc = tok(state.replace(mask, " "), add_special_tokens=False, return_offsets_mapping=True)
    offs = enc["offset_mapping"]
    kept_chars = offs[kept - 1][1] if kept > 0 else 0
    real, _ = build_sequence(tok, state, q, cfg["max_len"], cfg["head_max_len"])
    assert len(real) == prefix_len + kept + 1, (len(real), prefix_len, kept)
    return {"state_tokens": len(st), "state_room_tokens": room, "kept_tokens": kept,
            "kept_chars": int(kept_chars), "state_chars": len(state),
            "head_tokens": prefix_len, "truncated": len(st) > room,
            "n_options": len(render_options(q))}


# --------------------------------------------------------------- metrics
def ece_10bin(conf, correct):
    bins = [[] for _ in range(10)]
    for c, ok in zip(conf, correct):
        i = min(9, max(0, int(c * 10)))
        bins[i].append((c, 1.0 if ok else 0.0))
    n = len(conf)
    if n == 0:
        return float("nan"), []
    e, rows = 0.0, []
    for i, b in enumerate(bins):
        if not b:
            rows.append({"bin": i, "n": 0, "conf": None, "acc": None, "gap": None})
            continue
        mc = sum(x[0] for x in b) / len(b)
        ma = sum(x[1] for x in b) / len(b)
        e += (len(b) / n) * abs(mc - ma)
        rows.append({"bin": i, "n": len(b), "conf": round(mc, 4), "acc": round(ma, 4),
                     "gap": round(abs(mc - ma), 4)})
    return float(e), rows


# --------------------------------------------------------------- one full pass
def run_pass(agent, cases, ctrl, keep_raw=True):
    recs, errors = [], []
    for c in cases:
        state = c["state_text"] if ctrl == "none" else nostate(c["state_text"])
        t0 = time.perf_counter()
        try:
            res = agent.system_one(state, questions_for(c))
            ms = (time.perf_counter() - t0) * 1000.0
            ans = res["answers"]["action"]
            raw = dict(ans)
            raw["usage"] = res.get("usage")
            p = {k: float(v) for k, v in ans["probabilities"].items()}
        except Exception as e:  # noqa: BLE001 - report, never hide
            ms = (time.perf_counter() - t0) * 1000.0
            errors.append({"id": c["id"], "error": "%s: %s" % (type(e).__name__, e)})
            recs.append({"id": c["id"], "choice": None, "probs": {}, "latency_ms": round(ms, 1),
                         "raw": {"error": str(e)}})
            continue
        tot = sum(p.values())
        pn = {k: (p.get(k, 0.0) / tot if tot > 0 else 0.0) for k in ACTION_ORDER}
        recs.append({"id": c["id"],
                     "choice": max(pn, key=pn.get),
                     "probs": {k: round(pn[k], 6) for k in ACTION_ORDER},
                     "latency_ms": round(ms, 1),
                     "raw": raw if keep_raw else None})
    return recs, errors


def summarize(cases, recs, errors, repeat_mismatch, cfg, trunc, wall_s, ctrl):
    truth = {c["id"]: set(c["truth_set"]) for c in cases}
    try:  # honest source of truth for the baseline: the cases generator's own summary
        with open(os.path.join(HERE, "cases", "summary.json"), encoding="utf-8") as f:
            base = json.load(f).get("majority_class_baseline", 0.2424)
    except Exception:  # noqa: BLE001
        base = 0.2424
    ok = [r for r in recs if r["choice"] is not None]
    correct = [r["choice"] in truth[r["id"]] for r in ok]
    conf = [max(r["probs"].values()) for r in ok]
    ece, rows = ece_10bin(conf, correct)
    # per_class is keyed by the TRUE action (the contract's "left has too few samples" note only reads that way:
    # it is a statement about how many cases have `left` as truth). n = cases in that truth class, correct = solved.
    per_class = {}
    for r in ok:
        for t in truth[r["id"]]:
            d = per_class.setdefault(t, {"n": 0, "correct": 0})
            d["n"] += 1
            d["correct"] += int(r["choice"] in truth[r["id"]])
    pred_hist = {}
    for r in ok:
        pred_hist[r["choice"]] = pred_hist.get(r["choice"], 0) + 1
    lats = [r["latency_ms"] for r in ok]
    return {
        "n": len(ok),
        "accuracy": round(sum(correct) / len(ok), 4) if ok else None,
        "majority_baseline": base,
        "per_class": {k: per_class[k] for k in ACTION_ORDER if k in per_class},
        "per_class_semantics": "keyed by the TRUE action: n = cases whose truth is that action, correct = choice in truth_set",
        "prediction_hist": {k: pred_hist[k] for k in ACTION_ORDER if k in pred_hist},
        "ece_10bin": round(ece, 4) if ok else None,
        "ece_bins": rows,
        "mean_latency_ms": round(statistics.mean(lats), 1) if lats else None,
        "median_latency_ms": round(statistics.median(lats), 1) if lats else None,
        "settings": {
            "checkpoint": "convaiinnovations/laya (repo root = English, ModernBERT-large 421M + 2-layer decision head)",
            "temperature": "cfg.temperature_by_options['choice:3-5'] = %r (applied to the raw marker logits before softmax)"
                             % cfg.get("temperature_by_options", {}).get("choice:3-5"),
            "device": ("cuda" if __import__("torch").cuda.is_available() else "cpu"),
            "prompt_shape": (
                "author's own rl_agent_api.RLAgent.system_one(state, questions); "
                "state = state_text VERBATIM (contract lane B, incl. trailing 'question: ...' line); "
                "one Choice question id='action', instructions='%s' (fixed string, NOT part of state_text), "
                "criteria = {action_id: candidates[].text} in cases.jsonl order (right,left,jump,right_jump,wait). "
                "rl_common.build_sequence -> [CLS] 'choice question: <instructions>' [SEP] [MASK]+opt x5 [SEP] "
                "state_text[:room] [SEP], max_len=%d head_max_len=%d; probs = softmax(logits/temperature) over the 5 "
                "markers, API rounds to 4dp, we renormalise and re-key to action names. "
                "NO extra prompt text: the question line the contract mandates is already the last line of state_text."
                % (INSTRUCTIONS, cfg["max_len"], cfg["head_max_len"])),
            "max_len": cfg["max_len"],
            "head_max_len": cfg["head_max_len"],
            "state_truncation": "build_sequence keeps state_text[:room] tokens (truncate_left=False) => the TAIL is dropped",
            "control_transform": (
                "none: state_text passed verbatim (contract lane B)"
                if ctrl == "none" else
                "nostate: byte-identical to scratch/game-probe/batch_probe.py strip_state() (drop every pure-map "
                "line, rewrite the player line to 'player: unknown state'); all other lines untouched"),
        },
        "repeat_mismatch": repeat_mismatch,
        "errors": errors,
        "control": ctrl,
        "extras": {
            "wall_clock_s": round(wall_s, 1),
            "truncation": trunc,
            "seeds": "deterministic: model.eval(), no dropout, no sampling",
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", choices=["none", "nostate"], default="none")
    ap.add_argument("--out", default=None, help="output file (required unless --stdin)")
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0, help="smoke-test only; 0 = all cases")
    ap.add_argument("--raw-diagnostic", action="store_true",
                    help="extra pass with temperature forced to 1.0 (answers: is the flat distribution the "
                         "post-hoc temperature or the encoder?) - recorded under summary.extras.untempered")
    ap.add_argument("--stdin", "--stdin-loop", dest="stdin", action="store_true",
                    help="resident single-shot mode: load the model ONCE, then read one JSON request line "
                         "{\"state_text\":..,\"candidates\":[{id,text},..]} per line from stdin and print one JSON "
                         "line {\"choice\":..,\"probs\":{..}} per request to stdout (flushed immediately); exit at EOF")
    ap.add_argument("--model-dir", default=MODEL_DIR)
    ap.add_argument("--threads", type=int, default=0)
    a = ap.parse_args()

    import numpy as np
    import torch
    if a.threads:
        torch.set_num_threads(a.threads)
    random.seed(0)
    np.random.seed(0)
    torch.manual_seed(0)

    sys.path.insert(0, a.model_dir)
    from rl_agent_api import RLAgent
    import rl_common

    t_load = time.perf_counter()
    agent = RLAgent(a.model_dir, device=("cuda" if __import__("torch").cuda.is_available() else "cpu"))
    load_s = time.perf_counter() - t_load

    if a.stdin:  # single-shot: read JSON line(s) from stdin, print one JSON line per request, exit at EOF
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            cands = req["candidates"]
            res = agent.system_one(req["state_text"], questions_from(cands))
            ans = res["answers"]["action"]
            p = {k: float(ans["probabilities"].get(k, 0.0)) for k in ans["probabilities"]}
            tot = sum(p.values())
            ids = [c["id"] for c in cands]
            probs = {k: round(p[k] / tot, 6) if tot > 0 else 0.0 for k in ids}
            sys.stdout.write(json.dumps({"choice": max(probs, key=probs.get), "probs": probs}) + "\n")
            sys.stdout.flush()
        return

    with open(CASES, encoding="utf-8") as f:
        cases = [json.loads(l) for l in f if l.strip()]
    n_all = len(cases)
    if a.limit:
        cases = cases[: a.limit]
    cases_md5 = hashlib.md5(open(CASES, "rb").read()).hexdigest()
    if not a.out:
        ap.error("--out is required unless --stdin")

    crit = {c["id"]: c["text"] for c in cases[0]["candidates"]}
    t = {k: [] for k in ("state_tokens", "kept_tokens", "kept_chars", "room")}
    first = None
    for c in cases:
        st = c["state_text"] if a.control == "none" else nostate(c["state_text"])
        m = measure_truncation(agent.tok, st, crit, agent.cfg)
        t["state_tokens"].append(m["state_tokens"])
        t["kept_tokens"].append(m["kept_tokens"])
        t["kept_chars"].append(m["kept_chars"])
        t["room"].append(m["state_room_tokens"])
        if first is None:
            first = m
    trunc = {"applies_to": "control=%s" % a.control, "first_case_id": cases[0]["id"],
             "prompt_head_tokens": first["head_tokens"], "state_room_tokens": first["state_room_tokens"],
             "state_tokens_min": min(t["state_tokens"]), "state_tokens_max": max(t["state_tokens"]),
             "state_tokens_median": statistics.median(t["state_tokens"]),
             "kept_tokens_median": statistics.median(t["kept_tokens"]),
             "kept_chars_median": statistics.median(t["kept_chars"]),
             "state_chars": first["state_chars"],
             "kept_fraction_of_chars": round(statistics.median(t["kept_chars"]) / first["state_chars"], 4),
             "cut_at_char": int(statistics.median(t["kept_chars"])),
             "state_text_cut_tail_preview": None,
             "truncated_cases": sum(1 for x, y in zip(t["state_tokens"], t["room"]) if x > y),
             "tail_dropped_note": None}

    wall0 = time.perf_counter()
    recs, errors = run_pass(agent, cases, a.control)
    rep, _ = run_pass(agent, cases, a.control, keep_raw=False) if a.passes > 1 else (recs, [])
    wall = time.perf_counter() - wall0

    by_id = {r["id"]: r for r in rep}
    mism = [{"id": r["id"], "pass1": r["choice"], "pass2": by_id[r["id"]]["choice"]}
            for r in recs if by_id[r["id"]]["choice"] != r["choice"]]

    untempered = None
    if a.control == "none" and trunc["cut_at_char"] < trunc["state_chars"]:
        s = cases[0]["state_text"]
        cut = trunc["cut_at_char"]
        trunc["state_text_cut_tail_preview"] = s[cut:cut + 160]
        trunc["state_text_kept_tail_preview"] = s[max(0, cut - 80):cut]
        rows = [l for l in s.split("\n") if MAP_ROW.match(l) or l.startswith(("player:", "enemy", "goal:", "question:"))]
        kept = s[:cut]
        trunc["dropped_lines"] = [l for l in rows if l not in kept]
        trunc["kept_lines"] = [l for l in rows if l in kept]
        trunc["tail_dropped_note"] = ("state_text is cut at char %d/%d (%.1f%% kept): the model never sees the "
                                      "player/enemy/goal/question lines when they fall past the cut"
                                      % (cut, trunc["state_chars"], 100.0 * cut / trunc["state_chars"]))

    summ = summarize(cases, recs, errors, len(mism), agent.cfg, trunc, wall, a.control)

    if a.raw_diagnostic:
        kept_tb, kept_t = agent.temperature_by_options, agent.temperature
        agent.temperature_by_options, agent.temperature = {}, [1.0, 1.0, 1.0]
        raw_recs, raw_errs = run_pass(agent, cases, a.control, keep_raw=False)
        agent.temperature_by_options, agent.temperature = kept_tb, kept_t
        truth = {c["id"]: set(c["truth_set"]) for c in cases}
        rc = [r["choice"] in truth[r["id"]] for r in raw_recs if r["choice"]]
        conf = [max(r["probs"].values()) for r in raw_recs if r["choice"]]
        re_, _ = ece_10bin(conf, rc)
        byid = {x["id"]: x for x in raw_recs}
        summ["extras"]["untempered"] = {
            "what": "one extra pass with cfg temperatures forced to 1.0 (raw marker-logit softmax, no post-hoc "
                    "scaling); the contract's probs use the shipped per-(qtype,option-count) temperature",
            "shipped_temperature_choice_3_5": agent.cfg.get("temperature_by_options", {}).get("choice:3-5"),
            "accuracy": round(sum(rc) / len(rc), 4) if rc else None,
            "ece_10bin": round(re_, 4) if rc else None,
            "mean_top_prob": round(statistics.mean(conf), 4) if conf else None,
            "choice_changed_vs_primary": sum(1 for r in recs if r["choice"] != byid[r["id"]]["choice"]),
            "per_case_top_prob": [{"id": r["id"], "choice": r["choice"], "top": max(r["probs"].values())}
                                  for r in raw_recs],
            "errors": raw_errs,
        }

    summ["extras"]["model_load_s"] = round(load_s, 1)
    summ["extras"]["passes"] = a.passes
    if a.limit:
        summ["settings"]["sample_rule"] = ("SMOKE-TEST SUBSET ONLY: first %d of %d cases (not a full run)"
                                           % (a.limit, n_all))
    summ["extras"]["repeat_mismatches"] = mism
    summ["extras"]["route"] = "B: HF root English checkpoint + rl_agent_api.RLAgent (pip laya NOT used)"
    summ["extras"]["cases_file"] = {"path": "scratch/game-probe/cases/cases.jsonl", "md5": cases_md5,
                                    "n_lines": n_all, "n_used": len(cases)}
    out = {"cases_md5": cases_md5, "control": a.control, "summary": summ, "cases": recs}
    with open(os.path.join(HERE, a.out), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in summ.items() if k != "ece_bins"}, ensure_ascii=False, indent=1))
    print("wrote", a.out)


if __name__ == "__main__":
    main()
