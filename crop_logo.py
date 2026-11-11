# -*- coding: utf-8 -*-
"""Recorta o bloco esquerdo da logo SEC 2025 (PDF oficial) e amostra cores da marca."""
import pymupdf

doc = pymupdf.open("logo_sec_2025.pdf")
page = doc[0]

# --- localiza a linha divisória vertical (traço fino e alto) ---
divider_x = None
for d in page.get_drawings():
    r = d["rect"]
    if r.width < 4 and r.height > 80:
        divider_x = (r.x0 + r.x1) / 2
        print("divisoria em x =", round(divider_x, 1), "| rect:", r)
        break

# --- bbox do bloco esquerdo (conteúdo à esquerda da divisória) ---
import itertools
xs, ys = [], []
for d in page.get_drawings():
    r = d["rect"]
    if r.width > page.rect.width * 0.9 or r.height > page.rect.height * 0.9:
        continue  # moldura / fundo
    if divider_x and r.x1 > divider_x:
        continue  # bloco do slogan
    xs += [r.x0, r.x1]
    ys += [r.y0, r.y1]
    if d.get("fill") and max(d["fill"]) - min(d["fill"]) > 0.08:
        c = tuple(round(v * 255) for v in d["fill"])
        if c not in [(255, 255, 255)]:
            print("  cor vetor:", c, "| rect:", round(r.x0), round(r.y0), round(r.x1), round(r.y1))

bbox = pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))
print("bbox bloco esquerdo:", bbox)

pad = 6
clip = pymupdf.Rect(max(0, bbox.x0 - pad), max(0, bbox.y0 - pad),
                    min(page.rect.x1, bbox.x1 + pad), min(page.rect.y1, bbox.y1 + pad))
pix = page.get_pixmap(dpi=300, clip=clip, alpha=False)
pix.save("logo_sec.png")
print("logo_sec.png:", pix.width, "x", pix.height)

# --- amostra de cores em pontos da área BAHIA (para achar o verde) ---
w, h, n = pix.width, pix.height, pix.n
def sample(fx, fy):
    x, y = int(fx * w), int(fy * h)
    p = pix.pixel(x, y)
    return "#%02X%02X%02X" % (p[0], p[1], p[2]), (x, y)
# varre uma grade e imprime cores saturadas encontradas
from collections import Counter
cores = Counter()
for fy in range(30, 75, 2):
    for fx in range(5, 60, 2):
        p = pix.pixel(int(fx / 100 * w), int(fy / 100 * h))
        if max(p[:3]) - min(p[:3]) > 40:  # cor saturada
            cores["#%02X%02X%02X" % (p[0], p[1], p[2])] += 1
for c, k in cores.most_common(14):
    print("cor da marca:", c, "x", k)

# --- otimiza para embutir na dashboard ---
from PIL import Image
img = Image.open("logo_sec.png").convert("RGB")
img = img.resize((900, round(img.height * 900 / img.width)), Image.LANCZOS)
img.save("logo_sec.png", optimize=True)
print("logo_sec.png otimizada:", img.size)
