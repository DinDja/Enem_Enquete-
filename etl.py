# -*- coding: utf-8 -*-
"""
ETL — Enquete de Participação ENEM 2026 (SEC Bahia)

Fontes (todas locais, nunca versionadas):
  1. Exports da enquete  SEC_ENEM*.xlsx
     - aba de interações (colunas Text/From)  -> recortes por hora, DDD,
       operadora, UF e cruzamento completo por telefone
     - aba de lista de respostas SIM ("Nome do estudante"/"Número do telefone")
       -> cruzamento atualizado dos que declararam comparecer
  2. dados_relatorio.json -> totais oficiais do relatório da plataforma
     (status de envio e respostas) — usados nos KPIs quando presentes
  3. Base de concluintes Pé-de-Meia (telefones) -> cruzamento por NTE

Gera: data.json (só agregados) e dashboard.html (autocontida, offline).
Uso:  python etl.py
"""
import base64
import glob
import json
import os
import re
import sys
from datetime import datetime

import pandas as pd

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
BASE = os.path.dirname(os.path.abspath(__file__))

# Dias de aplicação do ENEM 2026 (domingos oficiais — Inep)
EXAM_DAYS = [
    {"id": "d1", "label": "1º domingo", "data": "2026-11-08", "label_curto": "1º domingo (08/11)"},
    {"id": "d2", "label": "2º domingo", "data": "2026-11-15", "label_curto": "2º domingo (15/11)"},
]

# Regiões aproximadas por DDD (origem do número — pode haver portabilidade)
DDD_REGIAO = {
    "71": "RMS — Salvador e região metropolitana",
    "72": "Extremo Norte — Alagoinhas, Catu",
    "73": "Sul — Ilhéus, Itabuna, Porto Seguro",
    "74": "Norte/Nordeste — Juazeiro, Senhor do Bonfim, Paulo Afonso",
    "75": "Centro-Leste — Feira de Santana, Santo Antônio de Jesus",
    "77": "Oeste/Sudoeste — Barreiras, Vitória da Conquista",
    "79": "Sergipe — Aracaju e região",
}
FORA_BA = "Fora da Bahia (outros DDDs)"
SEM_NUM = "Não identificado"

ARQ_PDM = os.path.join(BASE, "Alunos concluintes Programa Pé-de-Meia.xlsx")
ARQ_RELATORIO = os.path.join(BASE, "dados_relatorio.json")
ARQ_DATA = os.path.join(BASE, "data.json")


# --------------------------------------------------------------------------
# NORMALIZAÇÃO
# --------------------------------------------------------------------------
def normalize_phone(valor):
    """Normaliza para DDD+9 dígitos (celular) ou DDD+8 (fixo). None se inválido."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    d = re.sub(r"\D", "", str(valor))
    if not d:
        return None
    d = d.lstrip("0")
    if len(d) in (12, 13) and d.startswith("55"):
        d = d[2:]
    if len(d) == 10:  # DDD + 8 dígitos
        d = d[:2] + "9" + d[2:]
    if len(d) == 12:  # 55 + DDD + 8
        d = d[:2] + "9" + d[2:]
    if len(d) == 11 and d[2] in "6789":
        return d  # celular
    if len(d) == 10 and d[2] in "2345":
        return d  # fixo
    return None


def parse_ts(s):
    return pd.to_datetime(s, format="%d/%m/%Y %H:%M:%S")


def operadora(network_name):
    p = str(network_name or "").split(" // ")[0]
    return {"TIM Brasil": "TIM"}.get(p, p)


def uf_da_rede(network_name):
    p = str(network_name or "").split(" // ")[-1]
    uf = p.split(" - ")[0]
    return uf if re.fullmatch(r"[A-Z]{2}", uf) else None


def dia_do_dado(ts):
    """Associa a resposta a um dia de aplicação: janela [prova, prova+7)."""
    d = ts.date()
    for ex in EXAM_DAYS:
        ini = datetime.strptime(ex["data"], "%Y-%m-%d").date()
        fim = ini + pd.Timedelta(days=7).to_pytimedelta()
        if ini <= d < fim:
            return ex["id"]
    return None


# --------------------------------------------------------------------------
# CARREGAMENTO
# --------------------------------------------------------------------------
def load_enquete():
    """Lê os exports: interações detalhadas e lista de respostas SIM."""
    arquivos = sorted(glob.glob(os.path.join(BASE, "SEC_ENEM*.xlsx")))
    frames, phones_sim, fontes = [], set(), []
    for a in arquivos:
        xl = pd.ExcelFile(a)
        tem_inter = tem_cand = False
        for sheet in xl.sheet_names:
            cols = xl.parse(sheet, nrows=2)
            cols_lidas = [str(c) for c in cols.columns]
            if "Text" in cols_lidas and "From" in cols_lidas and not tem_inter:
                df = xl.parse(sheet)
                df["arquivo_fonte"] = os.path.basename(a)
                frames.append(df)
                tem_inter = True
            elif any("Nome do estudante" in c for c in cols_lidas) and not tem_cand:
                df = xl.parse(sheet)
                col_tel = [c for c in df.columns if "fone" in str(c).lower()][0]
                tels = df[col_tel].map(normalize_phone).dropna()
                phones_sim.update(tels)
                tem_cand = True
        if tem_inter or tem_cand:
            fontes.append(os.path.basename(a))

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not df.empty:
        df["resp"] = (
            df["Text"].astype(str).str.extract(r"BUTTON - Text: (SIM|NÃO)")[0]
            .replace({"NÃO": "NAO"})
        )
        df["ts"] = parse_ts(df["Done At"])
        df["tel"] = df["From"].astype(str).map(normalize_phone)
        df["ddd"] = df["From"].astype(str).str[2:4]
        df["operadora"] = df["Network Name"].map(operadora)
        df["uf"] = df["Network Name"].map(uf_da_rede)
        df["dia"] = df["ts"].map(dia_do_dado)
        df = df[df["resp"].notna()]
        # deduplica interações repetidas entre exports
        df = df.drop_duplicates(subset=["From", "Done At", "Text"])
    return df, phones_sim, fontes


def load_relatorio():
    """Totais oficiais do relatório da plataforma (dados_relatorio.json)."""
    if not os.path.exists(ARQ_RELATORIO):
        return None
    with open(ARQ_RELATORIO, encoding="utf-8") as f:
        return json.load(f)


def load_pdm():
    if not os.path.exists(ARQ_PDM):
        return None
    df = pd.read_excel(ARQ_PDM, sheet_name="Planilha1", header=2)
    df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed")]
    df["teln"] = df["TELEFONE"].map(normalize_phone)
    return df


def load_bp():
    """Base Bolsa Presença — usada somente quando a base passar a ter telefones.

    Atualmente não é chamada: a base não traz telefones e nenhum dado dela
    aparece na dashboard. Quando houver telefones, o cruzamento será feito
    como em cruzamento_pdm().
    """
    meta = {"tem_telefone": False, "alunos": None, "ntes": None, "obs": []}
    for a in glob.glob(os.path.join(BASE, "BP_ESTUDANTES*.json.gz")):
        try:
            with gzip.open(a, "rt", encoding="utf-8") as f:
                data = json.load(f)
            rows = data["sheets"][0]["rows"]
            hdr = [str(x) for x in rows[0]]
            meta["alunos"] = len(rows) - 1
            meta["ntes"] = len({str(r[1]) for r in rows[1:] if len(r) > 1})
            candidatos = [i for i, h in enumerate(hdr)
                          if re.search(r"(TEL|FONE|CEL|WHATS)", h, re.I)]
            if candidatos:
                i = candidatos[0]
                if any(r[i] not in (None, "", " ") for r in rows[1:500]):
                    meta["tem_telefone"] = True
                    meta["coluna_telefone"] = hdr[i]
            if not meta["tem_telefone"]:
                meta["obs"].append(
                    "A base Bolsa Presença não possui telefone — o cruzamento por número fica "
                    "indisponível até que os telefones sejam incluídos na base."
                )
        except Exception:  # noqa
            meta["obs"].append("Falha ao ler a base Bolsa Presença.")
        break
    return meta


import gzip  # noqa: E402  (usado em load_bp)


# --------------------------------------------------------------------------
# AGREGAÇÕES
# --------------------------------------------------------------------------
def pct(a, b):
    return round(100 * a / b, 1) if b else None


def agregados_dia(dfd):
    """Todos os recortes para um recorte temporal (um dia ou geral)."""
    total = len(dfd)
    sim = int((dfd["resp"] == "SIM").sum())
    nao = int((dfd["resp"] == "NAO").sum())
    out = {
        "kpis": {
            "interacoes": total,
            "sim": sim,
            "nao": nao,
            "pct_sim": pct(sim, total),
            "pct_nao": pct(nao, total),
            "numeros_unicos": int(dfd["tel"].nunique()),
            "numeros_multi_resposta": int((dfd["tel"].value_counts() > 1).sum()),
            "primeira_resposta": dfd["ts"].min().strftime("%d/%m/%Y %H:%M") if total else None,
            "ultima_resposta": dfd["ts"].max().strftime("%d/%m/%Y %H:%M") if total else None,
        },
        "serie_horas": [],
        "por_ddd": [],
        "por_operadora": [],
        "por_uf": [],
    }

    g = dfd.groupby([dfd["ts"].dt.strftime("%Y-%m-%dT%H:00"), "resp"]).size().unstack(fill_value=0)
    for t, row in g.iterrows():
        out["serie_horas"].append({
            "t": t, "sim": int(row.get("SIM", 0)), "nao": int(row.get("NAO", 0)),
            "total": int(row.get("SIM", 0)) + int(row.get("NAO", 0)),
        })

    gg = dfd.groupby("ddd")["resp"].value_counts().unstack(fill_value=0)
    for ddd, row in gg.iterrows():
        s, n = int(row.get("SIM", 0)), int(row.get("NAO", 0))
        key = ddd if isinstance(ddd, str) and re.fullmatch(r"\d{2}", ddd) else SEM_NUM
        reg = DDD_REGIAO.get(key, FORA_BA if key != SEM_NUM else SEM_NUM)
        out["por_ddd"].append({
            "ddd": key, "regiao": reg, "sim": s, "nao": n,
            "total": s + n, "pct_sim": pct(s, s + n),
        })
    out["por_ddd"].sort(key=lambda x: -x["total"])

    gg = dfd.groupby("operadora")["resp"].value_counts().unstack(fill_value=0)
    for op, row in gg.iterrows():
        s, n = int(row.get("SIM", 0)), int(row.get("NAO", 0))
        out["por_operadora"].append({
            "operadora": op or SEM_NUM, "sim": s, "nao": n,
            "total": s + n, "pct_sim": pct(s, s + n),
        })
    out["por_operadora"].sort(key=lambda x: -x["total"])

    gg = dfd.groupby(dfd["uf"].fillna(SEM_NUM))["resp"].value_counts().unstack(fill_value=0)
    for uf, row in gg.iterrows():
        s, n = int(row.get("SIM", 0)), int(row.get("NAO", 0))
        out["por_uf"].append({
            "uf": uf, "sim": s, "nao": n, "total": s + n, "pct_sim": pct(s, s + n),
        })
    out["por_uf"].sort(key=lambda x: -x["total"])
    return out


def cruzamento_pdm(dfd_dia, pdm):
    """Cruzamento completo com concluintes Pé-de-Meia por telefone (agregado)."""
    if pdm is None or dfd_dia.empty:
        return None
    tel_base = set(pdm["teln"].dropna().unique())
    match = dfd_dia[dfd_dia["tel"].isin(tel_base)]
    nte_map = pdm.dropna(subset=["teln"]).drop_duplicates("teln").set_index("teln")["NTE"]
    tel_resp = match.dropna(subset=["tel"]).drop_duplicates("tel")
    por_nte = tel_resp.groupby(tel_resp["tel"].map(nte_map))["resp"].value_counts().unstack(fill_value=0)
    out = {
        "fonte": "interacoes",
        "base": {
            "alunos_base": int(len(pdm)),
            "cpfs_base": int(pdm["CPF"].nunique()),
            "telefones_validos": int(pdm["teln"].notna().sum()),
            "telefones_distintos": len(tel_base),
            "ntes": int(pdm["NTE"].nunique()),
            "municipios": int(pdm["MUNICIPIO"].nunique()),
        },
        "telefones_com_resposta": len(set(match["tel"].dropna())),
        "identificados_sim": int((match["resp"] == "SIM").sum()),
        "sim": int((match["resp"] == "SIM").sum()),
        "nao": int((match["resp"] == "NAO").sum()),
        "pct_sim": pct((match["resp"] == "SIM").sum(), len(match)),
        "por_nte": [],
    }
    for nte, row in por_nte.iterrows():
        s, n = int(row.get("SIM", 0)), int(row.get("NAO", 0))
        out["por_nte"].append({
            "nte": nte or SEM_NUM, "sim": s, "nao": n,
            "total": s + n, "pct_sim": pct(s, s + n),
        })
    out["por_nte"].sort(key=lambda x: -x["total"])
    return out


def cruzamento_pdm_sim(phones_sim, pdm, total_sim):
    """Cruzamento apenas do lado SIM (telefones que declararam comparecer)."""
    if pdm is None or not phones_sim:
        return None
    nte_map = pdm.dropna(subset=["teln"]).drop_duplicates("teln").set_index("teln")["NTE"]
    identificados = phones_sim & set(pdm["teln"].dropna().unique())
    por_nte = pd.Series([nte_map[t] for t in identificados]).value_counts()
    out = {
        "fonte": "lista_sim",
        "base": {
            "alunos_base": int(len(pdm)),
            "cpfs_base": int(pdm["CPF"].nunique()),
            "telefones_validos": int(pdm["teln"].notna().sum()),
            "telefones_distintos": int(pdm["teln"].dropna().nunique()),
            "ntes": int(pdm["NTE"].nunique()),
            "municipios": int(pdm["MUNICIPIO"].nunique()),
        },
        "telefones_com_resposta": len(phones_sim),
        "identificados_sim": len(identificados),
        "pct_dos_sim": pct(len(identificados), total_sim or len(phones_sim)),
        "pct_da_base": pct(len(identificados), int(pdm["teln"].dropna().nunique())),
        "por_nte": [
            {"nte": nte, "sim": int(q), "total": int(q)}
            for nte, q in por_nte.items()
        ],
    }
    return out


def carrega_recortes_anteriores():
    """Se não há mais interações detalhadas, reusa os recortes do data.json anterior."""
    if not os.path.exists(ARQ_DATA):
        return None
    try:
        with open(ARQ_DATA, encoding="utf-8") as f:
            prev = json.load(f)
        return prev.get("geral")
    except Exception:  # noqa
        return None


# --------------------------------------------------------------------------
# MONTAGEM DO DATA.JSON
# --------------------------------------------------------------------------
def main():
    print("Carregando enquete...")
    df, phones_sim, fontes = load_enquete()
    print(f"  interações detalhadas: {len(df)} | telefones SIM na lista: {len(phones_sim)}")

    print("Carregando relatório da plataforma...")
    rel = load_relatorio()
    if rel:
        print(f"  {rel['respostas']['total']} respostas | {rel['envio']['total']} envios")

    print("Carregando Pé-de-Meia...")
    pdm = load_pdm()
    print(f"  base: {len(pdm) if pdm is not None else 0} alunos")

    # ---- KPIs oficiais (relatório) ou das interações ----
    if rel:
        r_env, r_res = rel["envio"], rel["respostas"]
        kpis = {
            "interacoes": r_res["total"],
            "sim": r_res["sim"],
            "nao": r_res["nao"],
            "pct_sim": pct(r_res["sim"], r_res["total"]),
            "pct_nao": pct(r_res["nao"], r_res["total"]),
            "envios": r_env["total"],
            "entregues": r_env["entregue"],
            "taxa_entrega": pct(r_env["entregue"], r_env["total"]),
            "taxa_resposta": pct(r_res["total"], r_env["total"]),
            "taxa_resposta_entregues": pct(r_res["total"], r_env["entregue"]),
            "envio_detalhe": {
                "Entregue": r_env["entregue"],
                "Rejeitado": r_env["rejeitado"],
                "Expirado": r_env["expirado"],
                "Não pode ser entregue": r_env["nao_entregue"],
            },
            "fonte": "relatorio",
        }
    else:
        kpis = None  # cai nos KPIs das interações

    # ---- recortes detalhados (interações ou herdados do processamento anterior) ----
    if not df.empty:
        geral_rows = agregados_dia(df)
        kpis_rows = geral_rows["kpis"]
        recortes = {k: geral_rows[k] for k in ("serie_horas", "por_ddd", "por_operadora", "por_uf")}
        rec_ref = kpis_rows["ultima_resposta"]
    else:
        prev = carrega_recortes_anteriores()
        if prev:
            recortes = {k: prev[k] for k in ("serie_horas", "por_ddd", "por_operadora", "por_uf")}
            rec_ref = prev["kpis"].get("ultima_resposta") or prev.get("ref_recortes")
        else:
            recortes = {"serie_horas": [], "por_ddd": [], "por_operadora": [], "por_uf": []}
            rec_ref = None
        if kpis is None and prev:  # sem relatório nem interações novas
            kpis = prev["kpis"]

    # ---- cruzamento Pé-de-Meia ----
    if not df.empty:
        pdmeia = cruzamento_pdm(df, pdm)
    else:
        pdmeia = cruzamento_pdm_sim(phones_sim, pdm, kpis["sim"] if kpis else None)

    # ---- montagem ----
    dia1_kpis = dict(kpis) if kpis else {}
    dias = []
    for ex in EXAM_DAYS:
        sub = df[df["dia"] == ex["id"]] if not df.empty else df
        # dia tem dados se há interações na sua janela; sem detalhe por dia,
        # apenas o 1º dia carrega os totais (a campanha começou em 09/11)
        tem_dados = len(sub) > 0 or (kpis is not None and not dias)
        entry = {
            "id": ex["id"], "label": ex["label"], "data": ex["data"],
            "label_curto": ex["label_curto"],
            "tem_dados": tem_dados,
        }
        if entry["tem_dados"]:
            if len(sub) > 0:
                d_rows = agregados_dia(sub)
                entry.update(d_rows)
            else:
                # recortes herdados são todos do 1º domingo até agora
                entry["kpis"] = dia1_kpis
                entry.update(recortes)
                entry["ref_recortes"] = rec_ref
            entry["pdmeia"] = pdmeia
        dias.append(entry)

    geral = {"id": "geral", "kpis": kpis, **recortes, "ref_recortes": rec_ref, "pdmeia": pdmeia}

    avisos = [
        "Os recortes detalhados (por hora, DDD, operadora e UF) são do levantamento de "
        f"{rec_ref or '—'}; os totais gerais refletem o levantamento de 12/11/2026.",
        "DDD reflete a origem do número; portabilidade e números antigos podem distorcer o recorte geográfico.",
        "Responder 'SIM' é declaração do contato (pode ser o próprio aluno ou quem tem o número dele no telefone).",
        "Dados exclusivamente agregados — nenhum nome, CPF ou número de telefone individual é incluído na dashboard.",
    ]

    data = {
        "meta": {
            "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "titulo": "ENEM 2026 — Enquete de Participação (WhatsApp) · SEC Bahia",
            "relatorio": rel["referencia"] if rel else None,
            "janela_relatorio": rel["janela"] if rel else None,
            "avisos": avisos,
        },
        "dias": dias,
        "geral": geral,
    }
    with open(ARQ_DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    # ---- dashboard.html ----
    tpl_path = os.path.join(BASE, "template.html")
    out_html = os.path.join(BASE, "dashboard.html")
    tpl = open(tpl_path, encoding="utf-8").read()
    chartjs_path = os.path.join(BASE, "chart.umd.min.js")
    if os.path.exists(chartjs_path):
        chart_tag = "<script>" + open(chartjs_path, encoding="utf-8").read() + "</script>"
    else:
        chart_tag = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>'
    html = tpl.replace("__CHARTJS__", chart_tag)
    html = html.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    html = html.replace("__GERADO_EM__", data["meta"]["gerado_em"])
    logo_path = os.path.join(BASE, "logo_sec.png")
    if os.path.exists(logo_path):
        b64 = base64.b64encode(open(logo_path, "rb").read()).decode()
        html = html.replace("__LOGO__", "data:image/png;base64," + b64)
    else:
        html = html.replace("__LOGO__",
                            "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)

    # ---- resumo no console ----
    k = kpis
    print("\n==== RESUMO ====")
    print(f"Respostas: {k['interacoes']} | SIM {k['sim']} ({k['pct_sim']}%) | NÃO {k['nao']} ({k['pct_nao']}%)")
    if k.get("envios"):
        print(f"Envios: {k['envios']} | entrega {k['taxa_entrega']}% | resposta {k['taxa_resposta']}% "
              f"({k['taxa_resposta_entregues']}% entre entregues)")
    if pdmeia:
        print(f"PdM identificados (SIM): {pdmeia['identificados_sim']} ({pdmeia.get('pct_dos_sim')}% dos SIM)")
    print(f"\nOK -> {ARQ_DATA}\nOK -> {out_html}")


if __name__ == "__main__":
    main()
