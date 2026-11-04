"""Render the short promo video (MP4) and README GIF from real mapsql-mcp outputs.

Every map shown is a literal render_map output in docs/images; tool names, counts and the refusal message are the
real ones from the demo databases. Needs Pillow and imageio-ffmpeg (``pip install imageio-ffmpeg``).

    python scripts/make_demo_video.py --lang en --out docs/demo_en.mp4 --gif docs/images/demo.gif
    python scripts/make_demo_video.py --lang tr --out docs/demo_tr.mp4
"""

from __future__ import annotations

import argparse
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "images"
W = H = 1080
FPS = 15

NAVY_TOP, NAVY_BOTTOM = (11, 22, 48), (20, 44, 86)
GOLD, BLUE, WHITE, GREY, RED, GREEN = (227, 176, 75), (142, 197, 255), (240, 244, 250), (150, 165, 190), (255, 120, 110), (120, 220, 150)

TEXT = {
    "tr": {
        "tagline": "Haritaya soru sor,\ncevabı harita olarak al.",
        "sub": "PostGIS · Oracle Spatial · MCP",
        "q1": "Kızılay'a 500 m içindeki eczaneleri haritada göster.",
        "c1": "20 eczane · mesafeler metre cinsinden",
        "q2": "Mahalleleri kafe sayısına göre renklendir.",
        "c2": "Oracle Spatial · PostGIS ile birebir aynı sonuç",
        "q3": "DELETE FROM ankara_pois",
        "c3": "Güvenli: her sorgu ayrıştırılır, yalnız okuma",
        "end1": "Açık kaynak · MIT · 807 test",
        "you": "Siz",
        "ai": "Asistan → mapsql-mcp",
    },
    "en": {
        "tagline": "Ask your spatial database,\nget the answer as a map.",
        "sub": "PostGIS · Oracle Spatial · MCP",
        "q1": "Show the pharmacies within 500 m of Kızılay on a map.",
        "c1": "20 pharmacies · geodesic metres",
        "q2": "Colour the neighbourhoods by number of cafes.",
        "c2": "Oracle Spatial · identical to the PostGIS result",
        "q3": "DELETE FROM ankara_pois",
        "c3": "Safe: every query is parsed, read-only",
        "end1": "Open source · MIT · 807 tests",
        "you": "You",
        "ai": "Assistant → mapsql-mcp",
    },
}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


MONO = None
for _n in ("consola.ttf", "DejaVuSansMono.ttf"):
    try:
        MONO = ImageFont.truetype(_n, 30)
        break
    except OSError:
        pass
MONO = MONO or font(30)


def background() -> Image.Image:
    bg = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(bg)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(NAVY_TOP, NAVY_BOTTOM)))
    for i in range(7):  # faint contour ellipses, same identity as the other promo visuals
        r = 260 + i * 95
        d.ellipse([W - 160 - r, -120 - r // 2, W - 160 + r, -120 + r], outline=(36, 62, 108), width=2)
    return bg


BG = background()


def footer(d: ImageDraw.ImageDraw) -> None:
    d.text((60, H - 70), "github.com/mveyseldelibas/mapsql-mcp", font=font(28), fill=GREY)
    d.text((W - 60, H - 70), "mapsql-mcp", font=font(28, True), fill=GOLD, anchor="ra")


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def bubble(d: ImageDraw.ImageDraw, y: int, who: str, text: str, color, fnt) -> int:
    d.text((60, y), who, font=font(26, True), fill=GREY)
    lines, line = [], ""
    for w in text.split():
        cand = (line + " " + w).strip()
        if d.textlength(cand, font=fnt) > W - 180:
            lines.append(line)
            line = w
        else:
            line = cand
    lines.append(line)
    h = 30 + 46 * len(lines)
    d.rounded_rectangle([60, y + 38, W - 60, y + 38 + h], radius=22, fill=(30, 52, 92), outline=color, width=2)
    for i, ln in enumerate(lines):
        d.text((90, y + 52 + i * 46), ln, font=fnt, fill=WHITE)
    return y + 38 + h


def title_frame(L: dict, t: float) -> Image.Image:
    im = BG.copy()
    d = ImageDraw.Draw(im)
    a = ease(t * 2)
    d.text((W // 2, 300), "mapsql-mcp", font=font(110, True), fill=tuple(int(c * a) for c in GOLD), anchor="mm")
    for i, ln in enumerate(L["tagline"].split("\n")):
        d.text((W // 2, 470 + i * 78), ln, font=font(60), fill=tuple(int(c * ease(t * 2 - 0.4)) for c in WHITE), anchor="mm")
    d.text((W // 2, 700), L["sub"], font=font(40), fill=tuple(int(c * ease(t * 2 - 0.8)) for c in BLUE), anchor="mm")
    footer(d)
    return im


def scene_frame(L: dict, q: str, calls: list[str], result, caption: str, t: float, ok=True) -> Image.Image:
    """t in [0,1]: question types in, tool calls appear, result (map image or text) fades in."""
    im = BG.copy()
    d = ImageDraw.Draw(im)
    n = int(len(q) * ease(t / 0.3))
    y = bubble(d, 60, L["you"], q[:n] + ("▌" if t < 0.3 else ""), BLUE, font(38))
    y += 30
    d.text((60, y), L["ai"], font=font(26, True), fill=GREY)
    y += 40
    for i, c in enumerate(calls):
        if t > 0.32 + i * 0.07:
            d.rounded_rectangle([60, y, 60 + d.textlength(c, font=MONO) + 40, y + 50], radius=12, fill=(18, 34, 64), outline=GOLD, width=2)
            d.text((80, y + 9), c, font=MONO, fill=GOLD)
            y += 62
    a = ease((t - 0.5) / 0.2)
    if a > 0:
        y += 10
        if isinstance(result, Image.Image):
            box_w = W - 120
            box_h = min(H - y - 170, int(box_w * result.height / result.width))
            img = result.resize((int(box_h * result.width / result.height), box_h), Image.LANCZOS)
            x = (W - img.width) // 2
            im.paste(Image.blend(im.crop((x, y, x + img.width, y + img.height)), img.convert("RGB"), a), (x, y))
            y += img.height + 18
        else:
            d.rounded_rectangle([60, y, W - 60, y + 80], radius=14, fill=(60, 24, 30), outline=RED, width=2)
            d.text((84, y + 22), result, font=MONO, fill=RED)
            y += 100
        col = GREEN if ok else RED
        d.text((W // 2, min(y + 10, H - 140)), caption, font=font(36, True), fill=tuple(int(c * a) for c in col), anchor="ma")
    footer(d)
    return im


def end_frame(L: dict, t: float) -> Image.Image:
    im = BG.copy()
    d = ImageDraw.Draw(im)
    d.text((W // 2, 360), "mapsql-mcp", font=font(100, True), fill=GOLD, anchor="mm")
    d.text((W // 2, 480), "github.com/mveyseldelibas/mapsql-mcp", font=font(44), fill=WHITE, anchor="mm")
    d.text((W // 2, 560), L["end1"], font=font(40), fill=BLUE, anchor="mm")
    d.text((W // 2, 640), "Claude · Cursor · VS Code", font=font(34), fill=GREY, anchor="mm")
    footer(d)
    return im


def timeline(L: dict):
    pharm = Image.open(IMG / "pharmacies_near_kizilay.png")
    cafes = Image.open(IMG / "oracle_cafes_per_neighbourhood.png")
    yield (lambda L, t: title_frame(L, 1.0), (L,), 0.4)  # full title first: players use frame 0 as the cover
    yield (title_frame, (L,), 3.0)
    yield (scene_frame, (L, L["q1"], ["list_layers()", "within_distance(500 m)", "render_map()"], pharm, L["c1"]), 7.0)
    yield (scene_frame, (L, L["q2"], ["spatial_join(contains, count)", "render_map(choropleth)"], cafes, L["c2"]), 7.0)
    yield (scene_frame, (L, L["q3"], ["run_sql(...)"], "Only SELECT statements are allowed.", L["c3"]), 5.0)
    yield (end_frame, (L,), 3.5)


def frames(lang: str, size: int = W):
    L = TEXT[lang]
    for fn, args, secs in timeline(L):
        n = int(secs * FPS)
        for i in range(n):
            t = i / max(1, n - 1)
            im = fn(*args, t) if fn is not scene_frame else fn(*args, t=min(1.0, t * 1.25))
            yield im if size == W else im.resize((size, size), Image.LANCZOS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=["tr", "en"], default="en")
    ap.add_argument("--out", required=True)
    ap.add_argument("--gif", help="also write a small looping GIF (README)")
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    w = imageio_ffmpeg.write_frames(str(out), (W, H), fps=FPS, codec="libx264", quality=8, pix_fmt_out="yuv420p",
                                    macro_block_size=8, output_params=["-movflags", "+faststart"])
    w.send(None)
    for im in frames(a.lang):
        w.send(im.tobytes())
    w.close()
    print("video", out, round(out.stat().st_size / 1e6, 2), "MB")
    if a.gif:
        gif = [im.quantize(colors=96, method=Image.Quantize.MEDIANCUT) for i, im in enumerate(frames(a.lang, 640)) if i % 3 == 0]
        gif[0].save(a.gif, save_all=True, append_images=gif[1:], duration=int(1000 * 3 / FPS), loop=0, optimize=True)
        print("gif", a.gif, round(Path(a.gif).stat().st_size / 1e6, 2), "MB")


if __name__ == "__main__":
    main()
