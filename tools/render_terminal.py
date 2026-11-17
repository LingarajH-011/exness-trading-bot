#!/usr/bin/env python3
"""Render real command output as a terminal-style PNG for README evidence.

Usage:
    some-command 2>&1 | python3 tools/render_terminal.py out.png --cmd "some-command" --note "sample data"

The image shows exactly the text it is given. It is a picture of real output, not a mock-up.
Requires: pip install pillow
"""
import argparse
import os
import sys
import textwrap

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "C:/Windows/Fonts/consola.ttf",
]
BG, BAR, FG, DIM, PROMPT = "#0d1117", "#161b22", "#c9d1d9", "#8b949e", "#7ee787"
FAIL, PASS = "#ff7b72", "#7ee787"


def load_font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def colour_for(line):
    s = line.strip()
    if s.startswith("PASS") or (" passed" in s and " failed" not in s):
        return PASS
    if s.startswith("FAIL") or "FAILED" in s or "CRITICAL" in s or " HIGH " in f" {s} " or " failed" in s:
        return FAIL
    return FG


def render(text, out, cmd, note, width_chars):
    scale = 2
    size = 15 * scale
    font = load_font(size)
    small = load_font(12 * scale)
    lines = []
    for raw in text.rstrip("\n").split("\n"):
        raw = raw.expandtabs(4)
        lines.extend(textwrap.wrap(raw, width_chars, drop_whitespace=False, replace_whitespace=False) or [""])

    char_w = font.getlength("M")
    line_h = int(size * 1.45)
    pad = 20 * scale
    bar_h = 36 * scale
    foot_h = 30 * scale if note else 0
    cmd_lines = 1 if cmd else 0
    width = int(char_w * (max([len(l) for l in lines] + [len(cmd or "") + 2, 60])) + pad * 2)
    height = bar_h + pad + line_h * (len(lines) + cmd_lines) + pad + foot_h

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, bar_h], fill=BAR)
    for i, c in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        cx = pad + i * 22 * scale
        d.ellipse([cx, bar_h // 2 - 6 * scale, cx + 12 * scale, bar_h // 2 + 6 * scale], fill=c)
    d.text((pad + 80 * scale, bar_h // 2 - 8 * scale), "terminal", font=small, fill=DIM)

    y = bar_h + pad
    if cmd:
        d.text((pad, y), "$ ", font=font, fill=PROMPT)
        d.text((pad + char_w * 2, y), cmd, font=font, fill=FG)
        y += line_h
    for line in lines:
        d.text((pad, y), line, font=font, fill=colour_for(line))
        y += line_h
    if note:
        d.text((pad, height - foot_h + 4 * scale), note, font=small, fill=DIM)
    img.save(out, optimize=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output")
    ap.add_argument("--cmd", help="command line to show above the output")
    ap.add_argument("--note", help="footer text, for example where/when it was captured")
    ap.add_argument("--width", type=int, default=110, help="wrap width in characters")
    args = ap.parse_args()
    render(sys.stdin.read(), args.output, args.cmd, args.note, args.width)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
