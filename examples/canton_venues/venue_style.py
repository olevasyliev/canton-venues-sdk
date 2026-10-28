"""A venue's share card drawn in that venue's own visual language, still plainly ours.

Each venue gets one row in ``STYLES``: palette, the open-licensed font closest to its own, label
case and tracking, tile radius and border. ``render`` is the one function that draws every venue
from its row. Palettes come from each venue's own CSS (custom properties and the computed colours
of its pages) and pixels sampled from its pages, read 2026-10-07; ``source`` names where.

Only OFL fonts are embedded (``fonts/*-OFL.txt``): the venue's own family where it is open
(IBM Plex Mono, Reddit Sans, Aldrich, Inter, VT323, Chakra Petch, Bebas Neue, DM Mono,
Montserrat), never a venue's proprietary font file, logo or wordmark.

It stays our card: our mark and name sit at the top, and the foot of the card names
cantonvenues.com and says the card is independent of the venue, on the card's own background.

    python venue_style.py --api ./api --out card.png --venue temple
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import venue_pages as vp

# fonts whose glyphs run small or narrow for their size are scaled to match the rest
FONT_SCALE = {"VT323-Regular": 1.4, "BebasNeue-Regular": 1.3}
# the foot strip, in 1x pixels: the same padding above its two lines (from the rule) and below them
# (to the card's edge), the space between them, and where the stat tiles sit above it
STRIP_PAD = 28
STRIP_GAP = 12
TILE_H = 124
TILE_GAP = 26
FRAME_IN = 12  # a framed card's frame closes this far above the strip

# one row per venue slug (vp.VENUES). Colours: bg/bg2 the card (bg2 makes a gradient), ink the
# headline, muted the subtitle, accent the eyebrow, rule the hairline under the top rail; tile*
# the stat boxes; up marks a #1. Fonts are files in fonts/ without ".ttf"; "names" is the face for
# a tile of token names when the number face has no lowercase (Bebas would print USDCx as USDCX).
STYLES = {
    "temple": {
        "source": "https://app.templedigitalgroup.com/stats (app CSS --app-background, --color-text-primary, "
                  "--color-positive; coral #e07a5a from the stats chart; pixels)",
        "bg": "#161616", "ink": "#f2f1e9", "muted": "#9a9a9a", "accent": "#e07a5a", "rule": "#2a2a2a",
        "tile": "#0e0e0e", "tile_edge": "#2a2a2a", "tile_label": "#8a8a8a", "tile_ink": "#f2f1e9",
        "tile_note": "#9a9a9a", "up": "#27b48b",
        "head": "IBMPlexMono-Light", "label": "IBMPlexMono-Regular", "num": "IBMPlexMono-Light",
        "body": "IBMPlexMono-Light", "head_case": None, "label_case": "upper", "track": 0.2,
        "radius": 0, "border": 1,
    },
    "cantex": {
        "source": "https://www.cantex.io (CSS --colors-primary-10 #00236a, --colors-grey-10/50/60, hero pixels "
                  "#006fc8, swap card border #21d8d8; Reddit Sans)",
        "bg": "#00236a", "bg2": "#006fc8", "ink": "#ffffff", "muted": "#d4d7e4", "accent": "#21d8d8",
        "rule": "#2f5aa8",
        "tile": "#ffffff", "tile_edge": "#ffffff", "tile_label": "#546c91", "tile_ink": "#001236",
        "tile_note": "#546c91", "up": "#018e8e",
        "head": "RedditSans-Bold", "label": "RedditSans-Medium", "num": "RedditSans-Bold",
        "body": "RedditSans-Medium", "head_case": None, "label_case": None, "track": 0,
        "radius": 12, "border": 0,
    },
    "rocky": {
        "source": "https://rocky.exchange (CSS --bg, --text, --muted, --accent; headline and button gradient "
                  "pixels #f2a963 to #b3d3e3; Aldrich)",
        "bg": "#120d0a", "bg2": "#2a2018", "ink": "#f4efe6", "muted": "#a39c92", "accent": "#d8ab62",
        "rule": "#362e27", "head_grad": ("#f2a963", "#b3d3e3"),
        "tile": "#1b130e", "tile_edge": "#362e27", "tile_label": "#a39c92", "tile_ink": "#f4efe6",
        "tile_note": "#a39c92", "up": "#f09a4a",
        "head": "Aldrich-Regular", "label": "Aldrich-Regular", "num": "Aldrich-Regular", "body": "Inter-Regular",
        "head_case": "upper", "label_case": "upper", "track": 0.12, "radius": 0, "border": 1,
    },
    "tradecraft": {
        "source": "https://tradecraft.fi (computed colours: page #040404, panels #0d0d0d, text #84879a, "
                  "buttons #ff5050; Inter)",
        "bg": "#040404", "ink": "#ffffff", "muted": "#84879a", "accent": "#ff5050", "rule": "#252525",
        "tile": "#0d0d0d", "tile_edge": "#252525", "tile_label": "#8e9094", "tile_ink": "#ffffff",
        "tile_note": "#84879a", "up": "#ff5050",
        "head": "Inter-Bold", "label": "Inter-SemiBold", "num": "Inter-Bold", "body": "Inter-Regular",
        "head_case": "upper", "label_case": "upper", "track": 0.1, "radius": 16, "border": 1,
    },
    "ekiden": {
        "source": "https://ekiden.fi (computed colours: #000000 page, #e2ff04 text, frame and button; "
                  "VT323 and Chakra Petch from its CSS)",
        "bg": "#000000", "ink": "#e2ff04", "muted": "#c4dc0a", "accent": "#ffffff", "rule": "#e2ff04",
        "frame": "#e2ff04",
        "tile": "#000000", "tile_edge": "#e2ff04", "tile_label": "#a9bf03", "tile_ink": "#e2ff04",
        "tile_note": "#a9bf03", "up": "#e2ff04",
        "head": "ChakraPetch-Bold", "label": "VT323-Regular", "num": "VT323-Regular", "body": "VT323-Regular",
        "head_case": None, "label_case": None, "track": 0, "radius": 0, "border": 1,
    },
    "oneswap": {
        "source": "https://www.oneswap.cc (CSS --surface 24 24 27, --surface-above, --on-surface-*, "
                  "--accent-main; pink #ff68ff from the launch button; Bebas Neue and DM Mono)",
        "bg": "#18181b", "ink": "#fafafa", "muted": "#a1a1aa", "accent": "#ff68ff", "rule": "#3f3f46",
        "tile": "#201c24", "tile_edge": "#3f3f46", "tile_label": "#a1a1aa", "tile_ink": "#fafafa",
        "tile_note": "#a1a1aa", "up": "#32ffb4",
        "head": "BebasNeue-Regular", "label": "DMMono-Regular", "num": "BebasNeue-Regular",
        "body": "DMMono-Regular", "names": "DMMono-Medium", "head_case": "upper", "label_case": None, "track": 0, "radius": 0, "border": 1,
    },
    "pool-party": {
        "source": "https://poolparty.fun (CSS --c-canton2Dark #101010, --c-canton4Dark, --c-canton8Dark, "
                  "--c-brandDark #2563eb; Montserrat)",
        "bg": "#101010", "ink": "#ffffff", "muted": "#bcbcbc", "accent": "#5b8cf5", "rule": "#2b2b2b",
        "tile": "#171717", "tile_edge": "#2b2b2b", "tile_label": "#a3a3a3", "tile_ink": "#ffffff",
        "tile_note": "#a3a3a3", "up": "#5b8cf5",
        "head": "Montserrat-Bold", "label": "Montserrat-SemiBold", "num": "Montserrat-Bold",
        "body": "Montserrat-Light", "head_case": None, "label_case": None, "track": 0, "radius": 10, "border": 1,
    },
    "canborsa": {
        "source": "https://app.canborsa.com (CSS --background #050505, --card #0a0908, --foreground #f5ead4, "
                  "--muted-foreground #c9a476, --primary #e5a13a, --border #bf772540 on the page; its face is "
                  "Plus Jakarta Sans, Inter stands in)",
        "bg": "#050505", "ink": "#f5ead4", "muted": "#c9a476", "accent": "#e5a13a", "rule": "#33210d",
        "tile": "#0a0908", "tile_edge": "#33210d", "tile_label": "#c9a476", "tile_ink": "#f5ead4",
        "tile_note": "#c9a476", "up": "#e5a13a",
        "head": "Inter-Bold", "label": "Inter-SemiBold", "num": "Inter-Bold", "body": "Inter-Regular",
        "head_case": None, "label_case": "upper", "track": 0.06, "radius": 14, "border": 1,
    },
}



def font(name: str, size: float):
    from PIL import ImageFont
    return ImageFont.truetype(str(vp.FONTS / f"{name}.ttf"), max(1, round(size * FONT_SCALE.get(name, 1))))


def _case(text: str, case: str | None) -> str:
    return text.upper() if case == "upper" else text


def _width(d, text: str, f, track: float) -> float:
    return d.textlength(text, font=f) + track * f.size * len(text) if track else d.textlength(text, font=f)


def _text(d, xy, text: str, f, fill, track: float = 0, anchor: str = "ls") -> float:
    """Draw ``text`` with extra letter spacing (``track`` x size, as CSS ``letter-spacing``)."""
    if not track:
        d.text(xy, text, font=f, fill=fill, anchor=anchor)
        return xy[0] + d.textlength(text, font=f)
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill, anchor=anchor)
        x += d.textlength(ch, font=f) + track * f.size
    return x


def _fit(d, text: str, name: str, size: int, width: float, floor: int, track: float = 0):
    while size > floor and _width(d, text, font(name, size), track) > width:
        size -= 2
    return font(name, size)


def _gradient(size, a, b, diagonal: bool = False):
    """A two-colour linear gradient, top to bottom or top-left to bottom-right."""
    from PIL import Image, ImageChops
    w, h = size
    ramp = Image.linear_gradient("L").resize((w, h))
    if diagonal:
        across = Image.linear_gradient("L").rotate(90).resize((w, h))
        ramp = ImageChops.add(ramp, across, scale=2)
    return Image.composite(Image.new("RGB", size, b), Image.new("RGB", size, a), ramp)


def render(f: dict, head: dict, t: int, path: Path, style: dict | None = None) -> None:
    """The 1200x630 card for one venue in its own style, drawn at 2x and scaled down."""
    from PIL import Image, ImageDraw

    s = style or STYLES[f["venue"]["slug"]]
    c = {k: vp._hex(v) for k, v in s.items() if isinstance(v, str) and v.startswith("#")}
    S = 2
    W, H = 1200 * S, 630 * S
    M = 56 * S
    width = W - 2 * M
    img = (_gradient((W, H), c["bg"], c["bg2"], diagonal=True) if "bg2" in c
           else Image.new("RGB", (W, H), c["bg"]))
    d = ImageDraw.Draw(img)
    track = s["track"]
    # our strip at the foot: its two lines get the same space above (from the rule) and below (to the
    # card's edge), STRIP_PAD; the strip's height follows from the lines' own ink, so no line sits on
    # the edge whatever the venue's fonts
    sf = vp._font("SemiBold", 22 * S)
    note = footer_note(f, head)
    nf = _fit(d, note, "Inter-Regular", 18 * S, width, 14 * S)
    first = d.textbbox((0, 0), strip_line(), font=sf, anchor="ls")
    second = d.textbbox((0, 0), note, font=nf, anchor="ls")
    gap = STRIP_GAP * S  # from the first line's baseline to the top of the second's ink
    st = H - STRIP_PAD * S - (second[3] - second[1]) - gap - (-first[1]) - STRIP_PAD * S
    base1 = st + STRIP_PAD * S - first[1]
    base2 = base1 + gap - second[1]
    tiles_top = st - TILE_GAP * S - TILE_H * S  # the tiles keep their distance from the rule

    if "frame" in c:  # a hairline frame round the page, as the venue's own site has
        d.rectangle((14 * S, 14 * S, W - 14 * S, st - FRAME_IN * S), outline=c["frame"], width=S)

    # top rail: our mark and name on the left, the read time on the right in the venue's label face
    vp.draw_logo(d, M, 40 * S, 30 * S, c["ink"], c["bg"])
    d.text((M + 42 * S, 55 * S), "Canton Venues", font=vp._font("SemiBold", 24 * S), fill=c["ink"], anchor="lm")
    lab = font(s["label"], 16 * S)
    when = _case(vp.stamp(t), s["label_case"])
    tw = _width(d, when, lab, track)
    d.ellipse((W - M - tw - 22 * S, 50 * S, W - M - tw - 12 * S, 60 * S), fill=c["up"])
    _text(d, (W - M - tw, 61 * S), when, lab, c["muted"], track)
    d.line((M, 96 * S, W - M, 96 * S), fill=c["rule"], width=S)

    # eyebrow and the lead in the venue's own display face
    title = _case(head["title"], s["head_case"])
    tfont, lines = None, None
    for size in (60, 56, 52, 48, 44):
        tfont = font(s["head"], size * S)
        if d.textlength(title, font=tfont) <= width:
            lines = [title]
            break
    if not lines:
        for size in (56, 52, 48, 44, 40):
            tfont = font(s["head"], size * S)
            lines = vp._balanced(d, title, tfont, width)
            if lines:
                break
    lines = lines or vp._wrap(d, title, tfont, width, 2)
    step = round(tfont.size * (1.0 if s["head_case"] == "upper" else 1.14))
    block = 22 * S + 24 * S + len(lines) * step + 12 * S + 28 * S
    y = 96 * S + max(28 * S, (tiles_top - 18 * S - 96 * S - block) // 2)
    eyebrow = _case(f"{f['name']} / {f['kind']}", "upper")
    _text(d, (M, y + 18 * S), eyebrow, font(s["label"], 18 * S), c["accent"], max(track, 0.12))
    y += 22 * S + 24 * S
    for line in lines:
        if s.get("head_grad"):
            _gradient_text(img, (M, y), line, tfont, s["head_grad"])
        else:
            d.text((M, y), line, font=tfont, fill=c["ink"], anchor="lt")
        y += step
    d.text((M, y + 12 * S), head["sub"], font=_fit(d, head["sub"], s["body"], 24 * S, width, 15 * S),
           fill=c["muted"], anchor="lt")

    # stat tiles: label over a value over a small line, in the venue's own box shape
    panels = vp.stats(f)
    if panels:
        gap, top, ph = 14 * S, tiles_top, TILE_H * S
        pw = (width - gap * (len(panels) - 1)) // len(panels)
        pad = 20 * S
        inner = pw - 2 * pad
        for n, p in enumerate(panels):
            x0 = M + n * (pw + gap)
            box = (x0, top, x0 + pw, top + ph)
            if s["radius"]:
                d.rounded_rectangle(box, radius=s["radius"] * S, fill=c["tile"],
                                    outline=c["tile_edge"] if s["border"] else None, width=s["border"] * S)
            else:
                d.rectangle(box, fill=c["tile"], outline=c["tile_edge"] if s["border"] else None,
                            width=s["border"] * S)
            k = _case(p["k"], s["label_case"])
            _text(d, (x0 + pad, top + 32 * S), k, _fit(d, k, s["label"], 16 * S, inner, 12 * S, track),
                  c["tile_label"], track)
            face = s.get("names", s["num"]) if p.get("names") else s["num"]
            v, vf = vp.fit_value(d, p, lambda z, face=face: font(face, z), inner, 40 * S, 22 * S)
            d.text((x0 + pad, top + 48 * S), v, font=vf, fill=c["tile_ink"], anchor="lt")
            first = p["n"].startswith("#1 ")
            d.text((x0 + pad, top + 98 * S), p["n"], font=_fit(d, p["n"], s["body"], 17 * S, inner, 12 * S),
                   fill=c["up"] if first else c["tile_note"], anchor="lt")

    # our strip: where the data comes from, and that the card is not the venue's own material. It sits
    # on the card's own background under a rule, the domain in the venue's accent
    if "frame" not in c:  # a framed card's own frame already closes the content above
        d.line((M, st, W - M, st), fill=c["rule"], width=S)
    x = M
    for part, col in (("Live Canton DEX data from ", c["ink"]), ("cantonvenues.com", c["accent"])):
        d.text((x, base1), part, font=sf, fill=col, anchor="ls")
        x += d.textlength(part, font=sf)
    d.text((M, base2), note, font=nf, fill=c["muted"], anchor="ls")

    out = img.resize((1200, 630), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    out.save(tmp, "PNG", optimize=True)
    os.replace(tmp, path)


def footer_note(f: dict, head: dict) -> str:
    return vp.card_notes(f, head)


def strip_line() -> str:
    return "Live Canton DEX data from cantonvenues.com"


def _gradient_text(img, xy, text: str, f, colours) -> None:
    """``text`` filled with a left-to-right gradient, as Rocky sets its headline."""
    from PIL import Image, ImageDraw
    mask = Image.new("L", img.size, 0)
    md = ImageDraw.Draw(mask)
    md.text(xy, text, font=f, fill=255, anchor="lt")
    box = mask.getbbox()
    if not box:
        return
    w = box[2] - box[0]
    ramp = Image.linear_gradient("L").rotate(90).resize((w, box[3] - box[1]))
    a, b = (vp._hex(x) for x in colours)
    fill = Image.composite(Image.new("RGB", ramp.size, b), Image.new("RGB", ramp.size, a), ramp)
    layer = img.copy()
    layer.paste(fill, box[:2])
    img.paste(layer, (0, 0), mask)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", type=Path, required=True, help="directory holding venues.json and the rest")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--venue", default="temple")
    args = ap.parse_args()
    data = {n: json.loads((args.api / f"{n}.json").read_text())
            for n in ("venues", "tokens", "execution", "lp", "perps")}
    f = vp.facts(data)[args.venue]
    render(f, vp.headline(f), data["venues"]["t"], args.out)


if __name__ == "__main__":
    main()
