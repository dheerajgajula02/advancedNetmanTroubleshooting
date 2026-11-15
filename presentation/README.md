# Presentation

A 4-minute click-through of the project, in two parts:

1. **The live network.** How a problem gets in (monitor, ticket, alarm, sweep), the first look against known-good, the rules, the AI check and fix, what the AI is given (RAG), how it learns, and the ServiceNow link.
2. **The gym (NetOps RL, built for the Meta hackathon).** Lab copies, injected faults, the AI working them, the lab grading it, the data, training our own model, and the exam on faults it never saw.

Every screen is a real screenshot from the running system, taken on 14 November 2026.

## Opening it

- **In a browser:** open `preview.html`. Use → and ← to step, and Enter (or a click) to open a step's screens. Inside a step, → and ← move between its screens and 1–9 jump to one. Esc closes it.
- **As slides:** `Netman-Troubleshooter.pptx` has 33 slides: each step, then its screens and facts, then the closing numbers. The spoken lines are in the speaker notes.

## Files

| File | What it is |
|---|---|
| `preview.html` | The deck as a standalone page |
| `index.html` | The same deck without the page wrapper (the published version) |
| `Netman-Troubleshooter.pptx` | The PowerPoint version |
| `make_pptx.py` | Rebuilds the PowerPoint from `index.html` (`python3 make_pptx.py`, needs `python-pptx`) |
| `shots/` | The screenshots |
