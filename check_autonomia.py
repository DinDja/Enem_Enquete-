# -*- coding: utf-8 -*-
"""Confere se a dashboard é autocontida (nada depende dos arquivos ignorados)."""
import re

html = open("dashboard.html", encoding="utf-8").read()
print("Chart.js embutido (sem CDN):", "cdn.jsdelivr" not in html and html.count("<script>") == 2)
print("Dados embutidos (sem fetch):", "fetch(" not in html and "const DATA =" in html)
print("Logo embutida (base64):", "data:image/png;base64," in html)
ext = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
print("recursos externos:", ext if ext else "nenhum")
