"""Builds the PowerPoint copy of the click-through deck: the flowchart grows one node per slide, and after each
node a slide with the real screen it stands for. Two chapters, each its own flowchart: the live network, then the
gym built for the Meta hackathon. Shapes are native, so it stays sharp and editable."""

import re
import sys
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

HERE = Path(__file__).parent
SRC = (HERE / "index.html").read_text()

BG, SURF, SURF2, LINE = "0A0B0D", "111317", "171A1F", "343B46"
TEXT, TEXT2, MUTED = "E8EAEE", "B6BCC6", "7F8792"
ACCENT, AI, OK, EXT = "93B4FF", "B79CFF", "5FC493", "E3B35F"
SANS, MONO = "Inter", "JetBrains Mono"


def js_list(name):
    """Read the NODES / EDGES literals out of the page so both copies say the same thing."""
    body = re.search(rf"const {name} = \[(.*?)\n\];", SRC, re.S).group(1)
    items = []
    for chunk in re.findall(r"\{(.*?)\}\s*,?\s*(?=\n\s*\{|\Z)", body, re.S):
        item = {}
        for key, val in re.findall(r'(\w+):\s*("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|\[\[.*?\]\]|\[.*?\])', chunk, re.S):
            if val.startswith('"'):
                item[key] = val[1:-1].replace('\\"', '"')
            elif val in ("true",):
                item[key] = True
            elif val.startswith("["):
                item[key] = re.findall(r'"((?:[^"\\]|\\.)*)"', val)
            else:
                item[key] = float(val)
        items.append(item)
    return items


NODES = js_list("NODES")
EDGES = js_list("EDGES")
FINALE = js_list("FINALE")
CHAPTERS = {1: "Part 1 · The live network", 2: "Part 2 · The gym · NetOps RL, built for the Meta hackathon"}
for item in NODES + EDGES:
    item["ch"] = int(item.get("ch", 1))
FINALE_SAY = re.search(r'const FINALE_SAY = "(.*?)";', SRC).group(1)
for n in NODES:  # shots come as a flat list of label, path, label, path...
    flat = n.get("shots") or []
    n["shots"] = list(zip(flat[0::2], flat[1::2]))
    n["facts"] = [re.sub(r"</?b>", "", f) for f in n.get("facts", [])]

prs = Presentation()
prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)  # 16:9
W, H = prs.slide_width, prs.slide_height
BLANK = prs.slide_layouts[6]

# The flowchart's 1600 x 900 grid, placed in the slide's stage area.
SX, SY, SW, SH = Emu(380000), Emu(520000), Emu(11432000), Emu(5000000)


def gx(x):
    return Emu(int(SX + x / 1600 * SW))


def gy(y):
    return Emu(int(SY + y / 900 * SH))


def rgb(h):
    return RGBColor.from_string(h)


def fill(shape, color):
    shape.fill.solid()
    shape.fill.fore_color.rgb = rgb(color)


def text(slide, x, y, w, h, s, size=14, color=TEXT, bold=False, font=SANS, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap, tf.vertical_anchor = True, anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    lines = s if isinstance(s, list) else [s]
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        r = p.add_run()
        r.text = line
        r.font.size, r.font.bold, r.font.name = Pt(size), bold, font
        r.font.color.rgb = rgb(color)
    return box


def background(slide):
    bg = slide.background.fill
    bg.solid()
    bg.fore_color.rgb = rgb(BG)


def header(slide, step, total, ch):
    text(slide, Emu(380000), Emu(170000), Emu(3000000), Emu(300000), "Netman Troubleshooter", 12, TEXT, True)
    text(slide, Emu(2500000), Emu(170000), Emu(6000000), Emu(300000), CHAPTERS[ch], 12, ACCENT if ch == 1 else EXT, font=MONO)
    left, span = 8600000, 3200000  # the progress bars share this width, however many steps there are
    pitch = span // total
    for i in range(total):
        later = i >= len(NODES) or NODES[i]["ch"] == 2
        bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(left + i * pitch), Emu(250000), Emu(int(pitch * 0.75)), Emu(36000))
        fill(bar, (EXT if later else ACCENT) if i <= step else LINE)
        bar.line.fill.background()


def arrow_line(shape, color, dashed=False):
    ln = shape.line
    ln.color.rgb = rgb(color)
    ln.width = Pt(2)
    lnxml = ln._get_or_add_ln()
    if dashed:
        dash = etree.SubElement(lnxml, qn("a:prstDash"))
        dash.set("val", "dash")
    tail = etree.SubElement(lnxml, qn("a:tailEnd"))
    tail.set("type", "triangle")
    tail.set("w", "med")
    tail.set("len", "med")


def path_points(d):
    """M x,y L x,y or M x,y C x1,y1 x2,y2 x,y, as points (curves sampled)."""
    nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", d)]
    if "C" in d:
        (x0, y0, x1, y1, x2, y2, x3, y3) = nums
        pts = []
        for i in range(25):
            t = i / 24
            a, b, c, e = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
            pts.append((a * x0 + b * x1 + c * x2 + e * x3, a * y0 + b * y1 + c * y2 + e * y3))
        return pts
    return [(nums[0], nums[1]), (nums[2], nums[3])]


def edge(slide, e):
    color = {"ai": AI, "ext": EXT, "next": MUTED}.get(e.get("kind"), ACCENT)
    pts = path_points(e["d"])
    if len(pts) == 2:
        shape = slide.shapes.add_connector(1, gx(pts[0][0]), gy(pts[0][1]), gx(pts[1][0]), gy(pts[1][1]))
    else:
        fb = slide.shapes.build_freeform(gx(pts[0][0]), gy(pts[0][1]), scale=1.0)
        fb.add_line_segments([(gx(x), gy(y)) for x, y in pts[1:]], close=False)
        shape = fb.convert_to_shape()
        shape.fill.background()
    arrow_line(shape, color, bool(e.get("dashed")))
    if e.get("label"):
        anchor = e.get("anchor", "middle")
        w = Emu(2600000)
        x = gx(e["lx"]) if anchor == "start" else Emu(int(gx(e["lx"]) - w / 2))
        if anchor == "start":  # labels on the right-hand column stop at the slide's edge
            w = Emu(min(int(w), int(W - x - 150000)))
        text(slide, x, Emu(int(gy(e["ly"]) - 150000)), w, Emu(220000), e["label"], 10, TEXT2, font=MONO,
             align=PP_ALIGN.LEFT if anchor == "start" else PP_ALIGN.CENTER)


def node(slide, n, current):
    w, h = Emu(int(300 / 1600 * SW)), Emu(int(124 / 900 * SH))
    x, y = Emu(int(gx(n["x"]) - w / 2)), Emu(int(gy(n["y"]) - h / 2))
    box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    box.adjustments[0] = 0.08
    fill(box, SURF2)
    edge_color = AI if n.get("ai") else EXT if n.get("ext") else (ACCENT if current else LINE)
    box.line.color.rgb = rgb(edge_color if (current or n.get("ai") or n.get("ext")) else LINE)
    box.line.width = Pt(2.25 if current else 1)
    title = n["title"] + (f"   [{n['chip'][0]}]" if n.get("chip") else "")
    text(slide, Emu(x + 120000), Emu(y + 90000), Emu(w - 240000), Emu(300000), title, 14, TEXT, True)
    text(slide, Emu(x + 120000), Emu(y + 400000), Emu(w - 240000), Emu(h - 450000), n["sub"], 10, TEXT2)


def flow_slide(upto, say, finale=False):
    s = prs.slides.add_slide(BLANK)
    background(s)
    ch = 2 if finale else NODES[upto]["ch"]
    header(s, len(NODES) if finale else upto, len(NODES) + 1, ch)
    stage = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Emu(SX - 120000), Emu(SY - 80000), Emu(SW + 240000), Emu(SH + 160000))
    stage.adjustments[0] = 0.02
    fill(stage, SURF)
    stage.line.color.rgb = rgb("262B33")
    shown = {n["id"] for n in NODES[: upto + 1] if n["ch"] == ch}
    for e in EDGES:
        if e["ch"] == ch and e["to"] in shown:
            edge(s, e)
    for i, n in enumerate(NODES[: upto + 1]):
        if n["ch"] == ch:
            node(s, n, current=(i == upto and not finale))
    if upto == 0:
        text(s, gx(40), gy(560), Emu(3600000), Emu(1000000), ["Most outages are fixed by someone", "who has seen it before."], 22, TEXT, True)
        text(s, gx(40), gy(800), Emu(3600000), Emu(400000), "We built that someone, for a real lab network, and taught it to keep learning.", 11, TEXT2)
    if ch == 2 and not finale:
        text(s, gx(40), gy(640), Emu(7000000), Emu(250000), "PART 2 · NETOPS RL · BUILT FOR THE META HACKATHON", 10, EXT, True, MONO)
        text(s, gx(40), gy(690), Emu(7000000), Emu(450000), "A gym where AI practises on broken copies of the network.", 22, TEXT, True)
        text(s, gx(40), gy(800), Emu(7000000), Emu(400000), "Faults are injected on purpose, an AI fixes them, and the lab, never the AI, grades the result. Every attempt becomes training data for our own model.", 11, TEXT2)
    if finale:
        colors = {"ok": OK, "ai": AI, "ext": EXT, "flow": ACCENT}
        for k, f in enumerate(FINALE):
            x = gx(40 + k * 385)
            text(s, x, gy(680), Emu(2500000), Emu(420000), f["big"], 30, colors.get(f.get("kind"), TEXT), True)
            text(s, x, gy(770), Emu(2500000), Emu(400000), f["lbl"], 10, TEXT2)
    text(s, Emu(380000), Emu(6050000), Emu(11400000), Emu(600000), say, 15, TEXT, anchor=MSO_ANCHOR.MIDDLE)
    return s


def detail_slide(n):
    s = prs.slides.add_slide(BLANK)
    background(s)
    text(s, Emu(380000), Emu(170000), Emu(9000000), Emu(300000), n.get("where", ""), 11, MUTED, font=MONO)
    shots = [(label, HERE / p) for label, p in n["shots"] if (HERE / p).exists()]
    left_w = Emu(8000000)
    top = 560000
    if len(shots) > 2:  # a grid, two across up to four screens, three across up to nine, each with its tab name above it
        cols, label_h, gap = (2 if len(shots) <= 4 else 3), 240000, 110000
        cell_w = (8000000 - (cols - 1) * gap) // cols
        pitch = label_h + int(cell_w / 1.55) + gap
        for i, (label, img) in enumerate(shots[:9]):
            x, y = 380000 + (i % cols) * (cell_w + gap), top + (i // cols) * pitch
            text(s, Emu(x), Emu(y), Emu(cell_w), Emu(label_h), label, 10 if cols == 3 else 11, TEXT2, font=MONO)
            pic = s.shapes.add_picture(str(img), Emu(x), Emu(y + label_h), width=Emu(cell_w))
            if pic.height > pitch - label_h - gap:  # a tall screen keeps to its row
                ratio = (pitch - label_h - gap) / pic.height
                pic.width, pic.height = Emu(int(pic.width * ratio)), Emu(int(pic.height * ratio))
    elif shots:
        avail_h = 6100000 if len(shots) == 1 else 2950000
        for i, (_, img) in enumerate(shots):
            pic = s.shapes.add_picture(str(img), Emu(380000), Emu(top + i * (avail_h + 120000)), height=Emu(avail_h))
            if pic.width > left_w:  # keep it inside the left column
                ratio = left_w / pic.width
                pic.width, pic.height = left_w, Emu(int(pic.height * ratio))
    x = Emu(8700000)
    title = n["title"] + (f"  [{n['chip'][0]}]" if n.get("chip") else "")
    text(s, x, Emu(560000), Emu(3150000), Emu(800000), title, 22, AI if n.get("ai") else EXT if n.get("ext") else TEXT, True)
    text(s, x, Emu(1500000), Emu(3150000), Emu(4500000), ["• " + f for f in n["facts"]], 13, TEXT2)
    return s


def notes(slide, words):
    slide.notes_slide.notes_text_frame.text = words


for i, n in enumerate(NODES):
    s = flow_slide(i, n["say"])
    notes(s, n["say"])
    d = detail_slide(n)
    notes(d, n["say"] + "\n\n" + "\n".join(n["facts"]))
s = flow_slide(len(NODES) - 1, FINALE_SAY, finale=True)
notes(s, FINALE_SAY)

out = HERE / "Netman-Troubleshooter.pptx"
prs.save(out)
print(out, len(prs.slides), "slides")
