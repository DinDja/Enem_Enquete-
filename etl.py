# -*- coding: utf-8 -*-
"""
ETL — Enquete de Participação ENEM 2026 (SEC Bahia)

Lê os exports da campanha WhatsApp (SEC_ENEM*.xlsx), a base de concluintes
Pé-de-Meia (com telefones) e a base Bolsa Presença (se tiver telefones),
cruza por número de telefone normalizado e gera:
  - data.json       (métricas agregadas — nenhum dado pessoal)
  - dashboard.html  (dashboard autocontida, funciona offline)

Uso:  python etl.py
Ao receber o export do 2º domingo, basta colocar o novo SEC_ENEM*.xlsx na pasta
e rodar novamente.
"""
import base64
import glob
import gzip
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
ARQ_BP = glob.glob(os.path.join(BASE, "BP_ESTUDANTES*.json.gz"))


# --------------------------------------------------------------------------
# NORMALIZAÇÃO
# --------------------------------------------------------------------------
def normalize_phone(valor):
    """Normaliza para DDD+9 dígitos (celular) ou DDD+8 (fixo). Retorna None se inválido."""
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
    arquivos = sorted(glob.glob(os.path.join(BASE, "SEC_ENEM*.xlsx")))
    if not arquivos:
        sys.exit("Nenhum arquivo SEC_ENEM*.xlsx encontrado.")
    frames = []
    for a in arquivos:
        df = pd.read_excel(a, sheet_name=0)
        if "Text" not in df.columns or "From" not in df.columns:
            print(f"  [aviso] {os.path.basename(a)} sem colunas esperadas — ignorado")
            continue
        df["arquivo_fonte"] = os.path.basename(a)
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)

    df["resp"] = (
        df["Text"].astype(str).str.extract(r"BUTTON - Text: (SIM|NÃO)")[0].replace({"NÃO": "NAO"})
    )
    df["ts"] = parse_ts(df["Done At"])
    df["tel"] = df["From"].astype(str).map(normalize_phone)
    df["ddd"] = df["From"].astype(str).str[2:4]
    df["operadora"] = df["Network Name"].map(operadora)
    df["uf"] = df["Network Name"].map(uf_da_rede)
    df["dia"] = df["ts"].map(dia_do_dado)
    df = df[df["resp"].notna()]
    return df, arquivos


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
    for a in ARQ_BP:
        try:
            with gzip.open(a, "rt", encoding="utf-8") as f:
                data = json.load(f)
            rows = data["sheets"][0]["rows"]
            hdr = [str(x) for x in rows[0]]
            meta["alunos"] = len(rows) - 1
            meta["ntes"] = len({str(r[1]) for r in rows[1:] if len(r) > 1})
            # procura coluna com cara de telefone
            candidatos = [i for i, h in enumerate(hdr)
                          if re.search(r"(TEL|FONE|CEL|WHATS)", h, re.I)]
            if candidatos:
                i = candidatos[0]
                tem = any(r[i] not in (None, "", " ") for r in rows[1:500])
                if tem:
                    meta["tem_telefone"] = True
                    meta["coluna_telefone"] = hdr[i]
            if not meta["tem_telefone"]:
                meta["obs"].append(
                    "A base Bolsa Presença não possui telefone — o cruzamento por número fica "
                    "indisponível até que os telefones sejam incluídos na base."
                )
        except Exception:  # noqa
            meta["obs"].append("Falha ao ler a base Bolsa Presença.")
        break  # usa só o arquivo mais recente
    return meta


# --------------------------------------------------------------------------
# AGREGAÇÕES
# --------------------------------------------------------------------------
def pct(a, b):
    return round(100 * a / b, 1) if b else None


def agg_subgrupo(sub):
    sim = int((sub["resp"] == "SIM").sum())
    nao = int((sub["resp"] == "NAO").sum())
    return {"total": sim + nao, "sim": sim, "nao": nao, "pct_sim": pct(sim, sim + nao)}


def agregados_dia(dfd):
    """Todos os recortes para um recorte temporal (um dia ou geral)."""
    total = len(dfd)
    sim = int((dfd["resp"] == "SIM").sum())
    nao = int((dfd["resp"] == "NAO").sum())
    numeros = dfd.groupby("tel")["resp"].agg(lambda s: s.value_counts().index[0])
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
            "datas_coleta": sorted({d.strftime("%d/%m/%Y") for d in dfd["ts"]}),
        },
        "serie_horas": [],
        "por_ddd": [],
        "por_operadora": [],
        "por_uf": [],
    }

    # série por hora
    g = dfd.groupby([dfd["ts"].dt.strftime("%Y-%m-%dT%H:00"), "resp"]).size().unstack(fill_value=0)
    for t, row in g.iterrows():
        out["serie_horas"].append({
            "t": t, "sim": int(row.get("SIM", 0)), "nao": int(row.get("NAO", 0)),
            "total": int(row.get("SIM", 0)) + int(row.get("NAO", 0)),
        })

    # por DDD
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

    # por operadora
    gg = dfd.groupby("operadora")["resp"].value_counts().unstack(fill_value=0)
    for op, row in gg.iterrows():
        s, n = int(row.get("SIM", 0)), int(row.get("NAO", 0))
        out["por_operadora"].append({
            "operadora": op or SEM_NUM, "sim": s, "nao": n,
            "total": s + n, "pct_sim": pct(s, s + n),
        })
    out["por_operadora"].sort(key=lambda x: -x["total"])

    # por UF
    gg = dfd.groupby(dfd["uf"].fillna(SEM_NUM))["resp"].value_counts().unstack(fill_value=0)
    for uf, row in gg.iterrows():
        s, n = int(row.get("SIM", 0)), int(row.get("NAO", 0))
        out["por_uf"].append({
            "uf": uf, "sim": s, "nao": n, "total": s + n, "pct_sim": pct(s, s + n),
        })
    out["por_uf"].sort(key=lambda x: -x["total"])
    return out


def cruzamento_pdm(dfd_dia, pdm):
    """Cruzamento com concluintes Pé-de-Meia por telefone (agregado)."""
    if pdm is None:
        return None
    tel_base = set(pdm["teln"].dropna().unique())
    match = dfd_dia[dfd_dia["tel"].isin(tel_base)]
    nte_map = pdm.dropna(subset=["teln"]).drop_duplicates("teln").set_index("teln")["NTE"]
    tel_resp = match.dropna(subset=["tel"]).drop_duplicates("tel")
    por_nte = tel_resp.groupby(tel_resp["tel"].map(nte_map))["resp"].value_counts().unstack(fill_value=0)
    base = {
        "alunos_base": int(len(pdm)),
        "cpfs_base": int(pdm["CPF"].nunique()),
        "telefones_validos": int(pdm["teln"].notna().sum()),
        "telefones_distintos": len(tel_base),
        "ntes": int(pdm["NTE"].nunique()),
        "municipios": int(pdm["MUNICIPIO"].nunique()),
    }
    out = {
        "base": base,
        "telefones_com_resposta": len(set(match["tel"].dropna())),
        "respostas": int(len(match)),
        "sim": int((match["resp"] == "SIM").sum()),
        "nao": int((match["resp"] == "NAO").sum()),
        "pct_sim": pct((match["resp"] == "SIM").sum(), len(match)),
        "por_nte": [],
    }
    for nte, row in por_nte.iterrows():
        s, n = int(row.get("SIM", 0)), int(row.get("NAO", 0))
        out["por_nte"].append({
            "nte": nte or SEM_NUM, "telefones": s + n, "sim": s, "nao": n,
            "total": s + n, "pct_sim": pct(s, s + n),
        })
    out["por_nte"].sort(key=lambda x: -x["total"])
    return out


# --------------------------------------------------------------------------
# MONTAGEM DO DATA.JSON
# --------------------------------------------------------------------------
def main():
    print("Carregando enquete...")
    df, arquivos = load_enquete()
    print(f"  {len(df)} interações | {len(arquivos)} arquivo(s) fonte")

    print("Carregando Pé-de-Meia...")
    pdm = load_pdm()
    print(f"  base: {len(pdm) if pdm is not None else 0} alunos")

    dias = []
    for ex in EXAM_DAYS:
        sub = df[df["dia"] == ex["id"]]
        tem = len(sub) > 0
        entry = {
            "id": ex["id"], "label": ex["label"], "data": ex["data"],
            "label_curto": ex["label_curto"], "tem_dados": tem,
        }
        if tem:
            entry.update(agregados_dia(sub))
            entry["pdmeia"] = cruzamento_pdm(sub, pdm)
        dias.append(entry)

    geral = agregados_dia(df)
    geral["id"] = "geral"
    geral["pdmeia"] = cruzamento_pdm(df, pdm)

    data = {
        "meta": {
            "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "titulo": "ENEM 2026 — Enquete de Participação (WhatsApp) · SEC Bahia",
            "avisos": [
                "A base contém apenas interações RECEBIDAS (sem contagem de envios): não é possível calcular taxa de resposta (respostas/enviados).",
                "DDD reflete a origem do número; portabilidade e números antigos podem distorcer o recorte geográfico.",
                f"{geral['kpis']['numeros_multi_resposta']} números enviaram mais de uma resposta; o KPI de interações mantém todas e 'números únicos' deduplica.",
                "Dados exclusivamente agregados — nenhum nome, CPF ou número de telefone individual é incluído na dashboard.",
            ],
        },
        "dias": dias,
        "geral": geral,
    }
    out_json = os.path.join(BASE, "data.json")
    with open(out_json, "w", encoding="utf-8") as f:
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
    k = geral["kpis"]
    print("\n==== RESUMO GERAL ====")
    print(f"Interações: {k['interacoes']} | números únicos: {k['numeros_unicos']}")
    print(f"SIM (compareci): {k['sim']} ({k['pct_sim']}%) | NÃO: {k['nao']} ({k['pct_nao']}%)")
    for d in dias:
        if d["tem_dados"]:
            pk = d["kpis"]
            pp = d["pdmeia"]
            print(f"\n[{d['label_curto']}] interações {pk['interacoes']} | SIM {pk['pct_sim']}%")
            if pp:
                print(f"  PdM identificados: {pp['telefones_com_resposta']} | SIM {pp['sim']} | NÃO {pp['nao']}")
        else:
            print(f"\n[{d['label_curto']}] sem dados ainda")
    print(f"\nOK -> {out_json}\nOK -> {out_html}")


if __name__ == "__main__":
    main()
