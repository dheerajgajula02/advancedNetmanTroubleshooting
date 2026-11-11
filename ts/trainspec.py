"""Which models can be fine-tuned, on which GPUs, and how long that takes. The figures are the hackathon console's."""

from __future__ import annotations

import math
import re

LLMS = {
    # id: (title, params in B, HF repo, license note)
    "qwen3-0.6b": ("Qwen3 0.6B", 0.6, "Qwen/Qwen3-0.6B", "Apache-2.0"),
    "qwen3-1.7b": ("Qwen3 1.7B", 1.7, "Qwen/Qwen3-1.7B", "Apache-2.0"),
    "qwen3-4b": ("Qwen3 4B", 4.0, "Qwen/Qwen3-4B", "Apache-2.0"),
    "qwen3-8b": ("Qwen3 8B", 8.2, "Qwen/Qwen3-8B", "Apache-2.0"),
    "llama3.2-1b": ("Llama 3.2 1B Instruct", 1.24, "unsloth/Llama-3.2-1B-Instruct", "Llama 3.2 Community"),
    "llama3.2-3b": ("Llama 3.2 3B Instruct", 3.2, "unsloth/Llama-3.2-3B-Instruct", "Llama 3.2 Community"),
    "gemma3-1b": ("Gemma 3 1B IT", 1.0, "unsloth/gemma-3-1b-it", "Gemma"),
    "gemma3-4b": ("Gemma 3 4B IT", 4.3, "unsloth/gemma-3-4b-it", "Gemma"),
}

METHODS = {
    "lora": {"title": "LoRA", "what": "Small trainable adapters on a frozen model. The usual choice."},
    "qlora": {"title": "QLoRA", "what": "LoRA on a 4-bit copy of the model: about a third of the GPU memory, "
                                        "roughly 25% slower, nearly the same quality."},
    "full": {"title": "Full fine-tune", "what": "Trains every weight. Needs much more GPU memory; only for "
                                                "models up to 1.7B here."},
}

# name pattern -> (memory bandwidth GB/s, fp32 TFLOPS, dense bf16/fp16 tensor TFLOPS, has bf16)
GPU_SPECS = [
    (r"MIG", (450, 30, 60, True)),                       # a slice of a bigger card: first, before its parent
    (r"H200", (4800, 67, 989, True)), (r"GH200", (4000, 67, 989, True)), (r"H100 NVL", (3900, 60, 835, True)),
    (r"H100.*(SXM|HBM3)|H100 80GB HBM3", (3350, 67, 989, True)), (r"H100", (2000, 51, 756, True)),
    (r"A100.*(SXM|80)", (2039, 19.5, 312, True)), (r"A100", (1555, 19.5, 312, True)),
    (r"B200", (8000, 80, 2250, True)),
    (r"PRO 6000|RTXPro6000|RTX PRO Server 6000", (1792, 126, 250, True)),
    (r"PRO 4500", (896, 55, 140, True)), (r"PRO 4000", (672, 46, 120, True)),
    (r"5090", (1792, 105, 210, True)), (r"5080", (960, 56, 113, True)),
    (r"4090", (1008, 83, 165, True)), (r"4080", (717, 49, 97, True)), (r"4070 Ti", (504, 40, 80, True)),
    (r"3090 Ti", (1008, 40, 80, True)), (r"3090", (936, 35.6, 71, True)), (r"3080 Ti", (912, 34, 68, True)),
    (r"3080", (760, 30, 60, True)), (r"3070", (448, 20, 40, True)),
    (r"6000 Ada|RTX6000Ada", (960, 91, 182, True)), (r"5000 Ada", (576, 65, 130, True)),
    (r"4000 Ada", (360, 26.7, 53, True)), (r"2000 Ada", (224, 12, 24, True)),
    (r"L40S", (864, 91.6, 362, True)), (r"L40", (864, 90.5, 181, True)), (r"L4\b", (300, 30.3, 121, True)),
    (r"A40\b", (696, 37.4, 75, True)), (r"A6000", (768, 38.7, 77, True)), (r"A5000", (768, 27.8, 55, True)),
    (r"A4500", (640, 23.7, 47, True)), (r"A4000", (448, 19.2, 38, True)), (r"A10G?\b", (600, 31, 62, True)),
    (r"V100", (900, 15.7, 125, False)), (r"T4", (320, 8.1, 65, False)), (r"P4\b|P100", (300, 9, 0, False)),
]
WIND_DOWN_MIN = 3.0
SETUP_MIN = 9.0
CHARS_PER_TOKEN = 3.7


def gpu_spec(name: str):
    for pat, spec in GPU_SPECS:
        if re.search(pat, name or "", re.I):
            return spec
    return None


def llm_vram_gb(params_b: float, method: str, max_len: int, batch: int) -> float:
    acts = 0.5 * batch * max_len / 1024 * max(1.0, params_b / 2) ** 0.5   # with gradient checkpointing
    if method == "full":
        return params_b * 16 + acts + 2           # bf16 weights + fp32 Adam states + grads
    if method == "qlora":
        return params_b * 0.6 + acts + 1.5
    return params_b * 2.05 + acts + 1.5


def llm_tokens_per_s(params_b: float, method: str, gpu_name: str) -> float | None:
    spec = gpu_spec(gpu_name)
    if not spec:
        return None
    _, tf32, tensor, bf16 = spec
    eff = (tensor or tf32) * 1e12 * (0.32 if bf16 else 0.2)
    flops_per_token = (6 if method == "full" else 4) * params_b * 1e9   # LoRA: no weight gradients
    if method == "qlora":
        eff *= 0.75
    return eff / flops_per_token


def suggested_limit(usd: float) -> float:
    """A round number above the estimate's high end, for the limit box (the user can type anything)."""
    hi = usd * 1.35
    step = 0.5 if hi < 5 else 1 if hi < 20 else 5
    return max(step, math.ceil(hi / step) * step)
