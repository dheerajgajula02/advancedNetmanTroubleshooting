#!/usr/bin/env python3
"""Fine-tune a small chat LLM (Qwen3 / Llama 3.2 / Gemma 3) on the next-call examples, then score it.

Same examples as Needle: the system prompt lists the tools, the user turn is the compact context, the assistant
turn is the next call as JSON. Loss is on the assistant turn only. LoRA, QLoRA (4-bit base) or a full fine-tune.
Plain transformers + peft with our own loop, so the STOP file, periodic atomic saves, progress.json and the
deadline behave exactly like the Needle trainer. A memory probe (the longest batch, forward + backward) runs right
after loading. Scoring (score.py) runs on the base model (adapters off) before training and on the tuned one after;
a STOP skips the second.
"""
import argparse
import contextlib
import json
import math
import os
import shutil
import sys
import time

import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from score import load_rows, parse_call, report  # noqa: E402

FORMAT = ('Reply with exactly one tool call as JSON: {"name": "<tool>", "arguments": {...}} and nothing else.')


def log(message):
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def status(stage):
    with open("status", "w") as fh:
        fh.write(stage + "\n")
    log(f"== {stage}")


def write_json(path, value):
    with open(path + ".tmp", "w") as fh:
        json.dump(value, fh)
    os.replace(path + ".tmp", path)


def messages(row):
    tools = "\n".join(json.dumps(t, separators=(",", ":")) for t in row["tools"])
    return [{"role": "system", "content": f"{row['system']}\n\nTools:\n{tools}\n\n{FORMAT}"},
            {"role": "user", "content": row["query"]}]


def prompt_ids(tok, row):
    kw = {"enable_thinking": False}                 # Qwen3: no <think> block; other templates ignore it
    ids = tok.apply_chat_template(messages(row), add_generation_prompt=True, tokenize=True, **kw)
    return list(ids["input_ids"] if isinstance(ids, dict) else ids)


def end_of_turn(tok):
    for t in ("<|im_end|>", "<|eot_id|>", "<end_of_turn>"):
        i = tok.convert_tokens_to_ids(t)
        if isinstance(i, int) and i != tok.unk_token_id and i >= 0:
            return i
    return tok.eos_token_id


def load_model(repo, method, dtype):
    from transformers import AutoModelForCausalLM, BitsAndBytesConfig
    kw = {"torch_dtype": torch.float32 if method == "full" else dtype, "attn_implementation": "sdpa"}
    if method == "qlora":
        kw["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                                       bnb_4bit_compute_dtype=dtype, bnb_4bit_use_double_quant=True)
    kw["device_map"] = {"": 0}
    try:
        return AutoModelForCausalLM.from_pretrained(repo, **kw)
    except ValueError:             # Gemma 3 4B ships as an image-text model; its text part is what we train
        from transformers import AutoModelForImageTextToText
        return AutoModelForImageTextToText.from_pretrained(repo, **kw)


@torch.no_grad()
def evaluate(model, tok, rows, out_dir, max_new=256, batch=8):
    """Greedy next call for each row, scored. Longest prompts first, so running out of GPU memory shows in the first
    batch, which then halves until it fits."""
    model.eval()
    cache = model.config.use_cache
    model.config.use_cache = True
    preds, started = {}, time.time()
    order = sorted(range(len(rows)), key=lambda i: -len(rows[i]["query"]))
    k = 0
    while k < len(order):
        if os.path.exists("STOP"):
            log("stop requested; scoring what finished")
            break
        idx = order[k:k + batch]
        enc = [prompt_ids(tok, rows[i]) for i in idx]
        width = max(map(len, enc))
        pad = tok.pad_token_id
        ids = torch.tensor([[pad] * (width - len(e)) + e for e in enc], device="cuda")
        att = torch.tensor([[0] * (width - len(e)) + [1] * len(e) for e in enc], device="cuda")
        try:
            out = model.generate(input_ids=ids, attention_mask=att, max_new_tokens=max_new, do_sample=False,
                                 pad_token_id=pad, eos_token_id=[end_of_turn(tok), tok.eos_token_id])
        except torch.OutOfMemoryError:
            del ids, att
            torch.cuda.empty_cache()
            if batch == 1:
                raise
            batch //= 2
            log(f"out of GPU memory generating at {width} tokens; scoring batch now {batch}")
            continue
        for j, i in enumerate(idx):
            text = tok.decode(out[j, width:], skip_special_tokens=True)
            call = parse_call(text)
            preds[i] = (call, None if call else f"unparsed: {text[:120]}")
        k += len(idx)
        if (k // batch) % 5 == 0:
            log(f"  scored {len(preds)}/{len(rows)}")
    summary, results = report(rows, preds, time.time() - started)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "predictions.jsonl"), "w") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    write_json(os.path.join(out_dir, "summary.json"), summary)
    log(json.dumps({k: summary[k] for k in ("examples", "overall", "report_status", "change_exact")}))
    model.config.use_cache = cache
    model.train()
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--method", choices=["lora", "qlora", "full"], default="lora")
    ap.add_argument("--data", default="data/train.jsonl")
    ap.add_argument("--eval", default="data/eval.jsonl")
    ap.add_argument("--tools", default="data/tools.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--epochs", type=float, default=1)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--grad-accum", type=int, default=4)
    ap.add_argument("--max-len", type=int, default=4096)
    ap.add_argument("--lora-rank", type=int, default=16)
    ap.add_argument("--lora-alpha", type=float, default=32)
    ap.add_argument("--lora-targets", default="all-linear")
    ap.add_argument("--save-every", type=float, default=300)
    ap.add_argument("--deadline", type=float, default=0)
    ap.add_argument("--eval-base", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--init-adapter", default="", help="continue this PEFT adapter folder (a previous run's) "
                                                       "instead of a fresh LoRA")
    args = ap.parse_args()
    torch.manual_seed(args.seed)
    os.makedirs(args.out, exist_ok=True)
    from transformers import AutoTokenizer

    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    tok = AutoTokenizer.from_pretrained(args.repo)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    tok.padding_side = "right"
    status("load")
    model = load_model(args.repo, args.method, dtype)
    log(f"{args.repo} on {torch.cuda.get_device_name(0)}, {sum(p.numel() for p in model.parameters()) / 1e9:.2f}B "
        f"params, {args.method}, {dtype}")
    eval_rows = load_rows(args.eval, args.tools) if os.path.exists(args.eval) else []

    if args.method in ("lora", "qlora"):
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        if args.method == "qlora":
            model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
        targets = "all-linear" if args.lora_targets == "all-linear" else ["q_proj", "k_proj", "v_proj", "o_proj"]
        if args.init_adapter:
            from peft import PeftModel
            model = PeftModel.from_pretrained(model, args.init_adapter, is_trainable=True)
            log(f"continuing the adapter in {args.init_adapter}")
        else:
            model = get_peft_model(model, LoraConfig(r=args.lora_rank, lora_alpha=args.lora_alpha, lora_dropout=0.0,
                                                     target_modules=targets, task_type="CAUSAL_LM"))
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    if hasattr(model, "enable_input_require_grads"):
        model.enable_input_require_grads()
    model.config.use_cache = False
    trainable = [p for p in model.parameters() if p.requires_grad]
    log(f"trainable {sum(p.numel() for p in trainable) / 1e6:.1f}M parameters")

    status("tokenize")
    eot = end_of_turn(tok)
    data, weights, skipped = [], [], 0
    for row in load_rows(args.data, args.tools):
        p = prompt_ids(tok, row)
        target = tok(json.dumps(row["answers"][0], ensure_ascii=False), add_special_tokens=False)["input_ids"] + [eot]
        if len(p) + len(target) > args.max_len:
            skipped += 1
            continue
        data.append((p + target, len(p)))
        weights.append(max(1, int(row.get("weight", 1))))
    per_pass = sum(weights)
    total = int(per_pass * args.epochs)
    log(f"data {len(data)} examples ({per_pass} per pass with repeats), {skipped} longer than {args.max_len} "
        f"tokens skipped; {total} to train on")

    opt = torch.optim.AdamW(trainable, lr=args.lr, weight_decay=0.0, betas=(0.9, 0.999))
    total_updates = max(1, math.ceil(total / (args.batch_size * args.grad_accum)))

    def lr_at(u):
        if u < args.warmup:
            return args.lr * (u + 1) / max(1, args.warmup)
        frac = (u - args.warmup) / max(1, total_updates - args.warmup)
        return args.lr * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * min(1.0, frac))))

    saves = 0

    def save(reason):
        nonlocal saves
        dest = os.path.join(args.out, "adapter" if args.method != "full" else "model")
        tmp = dest + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        model.save_pretrained(tmp, safe_serialization=True)
        tok.save_pretrained(tmp)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
        saves += 1
        log(f"saved {os.path.basename(dest)} ({reason}) after {seen} examples")

    micro = args.batch_size
    # Gemma 3 4B loads as an image-text model, which wants token_type_ids (all text here) while training
    multimodal = "ConditionalGeneration" in type(getattr(model, "get_base_model", lambda: model)()).__name__

    def width_of(idx):
        return max(len(data[i][0]) for i in idx)

    def train_step(idx):
        """Forward/backward one batch, left-padded so every answer ends at the last position: logits are only
        computed for the last K positions (K = longest answer + 1), not the whole prompt; for a 152k vocabulary at
        4k tokens the full logits alone would be ~10 GB."""
        total_loss = 0.0
        for c in range(0, len(idx), micro):
            part = idx[c:c + micro]
            width = width_of(part)
            keep = max(len(data[i][0]) - data[i][1] for i in part) + 1
            ids = torch.full((len(part), width), tok.pad_token_id, dtype=torch.long)
            labels = torch.full((len(part), keep - 1), -100, dtype=torch.long)
            att = torch.zeros((len(part), width), dtype=torch.long)
            for r, i in enumerate(part):
                seq, plen = data[i]
                ids[r, width - len(seq):] = torch.tensor(seq)
                att[r, width - len(seq):] = 1
                n = len(seq) - plen
                labels[r, keep - 1 - n:] = torch.tensor(seq[plen:])
            pos = (att.cumsum(-1) - 1).clamp(min=0)
            with torch.autocast("cuda", dtype=dtype):
                extra = {"token_type_ids": torch.zeros_like(ids).cuda()} if multimodal else {}
                logits = model(input_ids=ids.cuda(), attention_mask=att.cuda(), position_ids=pos.cuda(),
                               logits_to_keep=keep, **extra).logits
            loss = torch.nn.functional.cross_entropy(logits[:, :-1].float().reshape(-1, logits.shape[-1]),
                                                     labels.cuda().reshape(-1), ignore_index=-100)
            (loss * len(part) / len(idx) / args.grad_accum).backward()
            total_loss += float(loss) * len(part) / len(idx)
        return total_loss

    # Memory probe before anything slow: the batch_size longest examples, forward + backward + an optimizer step at
    # lr 0 (nothing changes; Adam's states get allocated). Out of memory halves the micro-batch; a run that cannot fit
    # one example fails here, a minute after loading, instead of after the base scoring.
    status("probe")
    worst = sorted(range(len(data)), key=lambda i: -len(data[i][0]))[:args.batch_size]
    for g in opt.param_groups:
        g["lr"] = 0.0
    while True:
        torch.cuda.reset_peak_memory_stats()
        try:
            train_step(worst)
            opt.step()
            break
        except torch.OutOfMemoryError:
            opt.zero_grad(set_to_none=True)
            torch.cuda.empty_cache()
            if micro == 1:
                raise SystemExit(f"out of GPU memory on a single {width_of(worst)}-token example: pick a card with "
                                 "more memory or a shorter max length")
            micro //= 2
            log(f"out of GPU memory at {width_of(worst)} tokens; micro-batch now {micro}")
    opt.zero_grad(set_to_none=True)
    opt.state.clear()
    peak, card = torch.cuda.max_memory_allocated() / 2**30, torch.cuda.get_device_properties(0).total_memory / 2**30
    log(f"memory probe: {len(worst)} x {width_of(worst)} tokens (micro-batch {micro}) peaks at {peak:.1f} of {card:.1f} GB")
    write_json(os.path.join(args.out, "probe.json"), {"tokens": width_of(worst), "batch": len(worst), "micro": micro,
                                                      "peak_gb": round(peak, 2), "card_gb": round(card, 2)})
    if eval_rows and args.eval_base:
        status("eval_base")
        with model.disable_adapter() if hasattr(model, "disable_adapter") else contextlib.nullcontext():
            evaluate(model, tok, eval_rows, os.path.join(args.out, "eval-base"))

    status("train")
    import random
    rnd = random.Random(args.seed)
    expanded = [i for i, w in enumerate(weights) for _ in range(w)]
    metrics = open(os.path.join(args.out, "metrics.jsonl"), "a")
    started = last_save = time.time()
    step = seen = updates = 0
    losses, stopped = [], None
    model.train()
    passes = math.ceil(args.epochs)
    for epoch in range(passes):
        order = expanded[:]
        rnd.shuffle(order)
        if epoch == passes - 1 and args.epochs < passes:
            order = order[: total - seen]
        # length-sorted chunks of 64 batches: little padding, still shuffled at the chunk level
        chunk = args.batch_size * 64
        groups = []
        for c in range(0, len(order), chunk):
            part = sorted(order[c:c + chunk], key=lambda i: len(data[i][0]))
            groups += [part[b:b + args.batch_size] for b in range(0, len(part), args.batch_size)]
        rnd.shuffle(groups)
        for idx in groups:
            if os.path.exists("STOP"):
                stopped = "stop requested"
                break
            if args.deadline and time.time() + 60 > args.deadline:
                stopped = "deadline"
                break
            try:
                loss = train_step(idx)
            except torch.OutOfMemoryError:
                # halve the micro-batch and keep the effective batch; this accumulation window's gradients are dropped
                opt.zero_grad(set_to_none=True)
                torch.cuda.empty_cache()
                if micro == 1:
                    raise
                micro //= 2
                log(f"out of GPU memory at {width_of(idx)} tokens; micro-batch now {micro}")
                loss = train_step(idx)
            losses.append(float(loss))
            step += 1
            seen += len(idx)
            if step % args.grad_accum == 0:
                for g in opt.param_groups:
                    g["lr"] = lr_at(updates)
                torch.nn.utils.clip_grad_norm_(trainable, 1.0)
                opt.step()
                opt.zero_grad(set_to_none=True)
                updates += 1
            if step % 10 == 0 or step == 1:
                rate = seen / max(1e-6, time.time() - started)
                rec = {"step": step, "epoch": round(seen / per_pass, 4), "examples": seen, "total": total,
                       "loss": round(sum(losses[-20:]) / len(losses[-20:]), 4), "lr": lr_at(updates),
                       "ex_per_s": round(rate, 3), "t": time.time()}
                metrics.write(json.dumps(rec) + "\n")
                metrics.flush()
                write_json("progress.json", {**rec, "saves": saves, "eta_s": round((total - seen) / max(rate, 1e-6))})
            if time.time() - last_save > args.save_every:
                save("periodic")
                last_save = time.time()
        if stopped:
            break
    save(stopped or "finished")
    metrics.close()
    summary = {"steps": step, "updates": updates, "examples_seen": seen, "planned": total,
               "unique_examples": len(data), "skipped_long": skipped,
               "final_loss": round(sum(losses[-20:]) / max(1, len(losses[-20:])), 4) if losses else None,
               "minutes": round((time.time() - started) / 60, 1), "stopped": stopped}
    write_json(os.path.join(args.out, "train_summary.json"), summary)
    write_json("progress.json", {"step": step, "examples": seen, "total": total, "saves": saves,
                                 "loss": summary["final_loss"], "done": True})
    if eval_rows and not stopped:
        status("eval")
        model.config.use_cache = True
        evaluate(model, tok, eval_rows, os.path.join(args.out, "eval-tuned"))
    log(f"done {json.dumps(summary)}")


if __name__ == "__main__":
    main()
