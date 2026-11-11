#!/usr/bin/env bash
# Runs on the rented GPU from /root/job (started by ts/training.py). The controller reads status,
# progress.json and out/metrics.jsonl, and asks for a stop by creating STOP: the trainer then saves and exits.
# Needs: REPO METHOD EPOCHS LR WARMUP BATCH ACCUM MAX_LEN RANK ALPHA TARGETS SAVE_EVERY SEED EVAL_BASE. TRAIN_DEADLINE is a safety net.
set -uo pipefail
cd "$(dirname "$0")"
export PYTHONUNBUFFERED=1 HF_HUB_DISABLE_TELEMETRY=1 TOKENIZERS_PARALLELISM=false
mkdir -p out
status() { echo "$1" > status; echo "$(date +%T) == $1"; }
step() { echo "$(date +%T) -- $1"; }
fail() { status "failed: $1"; exit 1; }
stopped() { [[ -e STOP ]]; }
why() { grep -E "Error|error:|out of GPU memory|Killed" out/train.log 2>/dev/null | tail -n 1 | cut -c1-220; }
deadline=()
[[ -n "${TRAIN_DEADLINE:-}" ]] && deadline=(--deadline "$TRAIN_DEADLINE")

status setup
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | tee out/gpu.txt
gunzip -f data/*.gz 2>/dev/null
PY=python3
PIP=(python3 -m pip install -q --progress-bar off)
step "install packages"
timeout 1200 "${PIP[@]}" "transformers>=4.51,<5" "peft>=0.15" "accelerate>=1.6" "bitsandbytes>=0.45" \
    safetensors sentencepiece protobuf "huggingface_hub[hf_xet]" || fail "pip install (exit $?)"
$PY -c "import torch; assert torch.cuda.is_available(); print(torch.__version__, torch.cuda.get_device_name(0))" \
    > out/devices.txt 2>&1 || { cat out/devices.txt; fail "no working GPU in torch"; }
step "model weights"
timeout 1800 $PY -c "from huggingface_hub import snapshot_download; snapshot_download('$REPO', allow_patterns=['*.json', '*.safetensors', '*.model', '*.txt', '*.jinja'])" \
    > out/download.log 2>&1 || fail "model download"
$PY llm_train.py --repo "$REPO" --method "$METHOD" --epochs "$EPOCHS" --lr "$LR" --warmup "$WARMUP" \
    --batch-size "$BATCH" --grad-accum "$ACCUM" --max-len "$MAX_LEN" --lora-rank "$RANK" --lora-alpha "$ALPHA" \
    --lora-targets "$TARGETS" --save-every "$SAVE_EVERY" --seed "$SEED" --eval-base "${EVAL_BASE:-1}" \
    "${deadline[@]}" 2>&1 | grep --line-buffered -v -i "warn" > out/train.log
[[ -d out/adapter || -d out/model ]] || fail "training saved no model: $(why)"
stopped && { status stopped; exit 0; }
status done
