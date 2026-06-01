"""
main.py — API REST para o analisador de CRC

Endpoints:
  POST /analisar          — recebe PDF + rendimento, devolve análise completa
  POST /analise-ia        — gera relatório narrativo com Claude
  POST /simular-consolidacao — simula consolidação de créditos pessoais
  GET  /health            — verificação de estado

Instalar: pip install -r requirements.txt
Correr:   uvicorn main:app --reload --port 8000
"""

import os
import tempfile
import re
import json
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import anthropic

from parser import parse_crc, analisar_crc

app = FastAPI(
    title="CRC Analyser API",
    description="Análise automática do Mapa de Responsabilidades de Crédito do Banco de Portugal",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_anthropic_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY não configurada")
    return anthropic.Anthropic(api_key=api_key)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


@app.post("/analisar")
async def analisar(
    pdf: UploadFile = File(...),
    rendimento_mensal: float = Form(...),
):
    """Analisa o PDF do CRC e devolve métricas financeiras completas."""
    if not pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="O ficheiro deve ser um PDF.")
    if rendimento_mensal <= 0:
        raise HTTPException(status_code=400, detail="O rendimento deve ser positivo.")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await pdf.read())
        tmp_path = tmp.name

    try:
        crc = parse_crc(tmp_path)
        if not crc.get("contratos"):
            raise HTTPException(status_code=422, detail="Não foi possível extrair contratos do PDF.")
        return analisar_crc(crc, rendimento_mensal)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar o PDF: {str(e)}")
    finally:
        os.unlink(tmp_path)


@app.post("/analise-ia")
async def analise_ia(
    pdf: UploadFile = File(...),
    rendimento_mensal: float = Form(...),
):
    """
    Gera uma análise narrativa personalizada com Claude.
    Devolve stream de texto para aparecer progressivamente no frontend.
    """
    if not pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="O ficheiro deve ser um PDF.")
    if rendimento_mensal <= 0:
        raise HTTPException(status_code=400, detail="O rendimento deve ser positivo.")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await pdf.read())
        tmp_path = tmp.name

    try:
        crc = parse_crc(tmp_path)
        if not crc.get("contratos"):
            raise HTTPException(status_code=422, detail="Não foi possível extrair contratos.")
        analise = analisar_crc(crc, rendimento_mensal)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)

    client = _get_anthropic_client()

    # prepara resumo dos contratos para o prompt
    contratos_resumo = "\n".join([
        f"- {c['instituicao']}: {c['produto']} | "
        f"Dívida: {c['total_em_divida']:.2f}€ | "
        f"Prestação: {c['prestacao']:.2f}€/mês | "
        f"Tipo negociação: {c['tipo_negociacao']} | "
        f"Fim: {c['fim']} | "
        f"Responsabilidade: {c['tipo_responsabilidade']}"
        for c in analise["contratos"]
    ])

    prompt = f"""És um consultor financeiro especialista em crédito bancário português. 
Analisa o seguinte perfil de crédito e elabora um relatório personalizado em português europeu (não brasileiro).

DADOS DO TITULAR:
- Nome: {analise['titular']}
- Referente a: {analise['referente_a']}
- Rendimento líquido mensal: {rendimento_mensal:.2f}€

MÉTRICAS FINANCEIRAS:
- Score de saúde financeira: {analise['metricas']['score_saude']}/100
- Taxa de esforço: {analise['metricas']['taxa_esforco_pct']}% (limite prudencial: 35%)
- Rácio dívida/rendimento anual: {analise['metricas']['racio_divida_rendimento']}× (alerta acima de 3×)
- Total em dívida efectiva: {analise['metricas']['divida_efetiva']:.2f}€
- Prestação mensal total: {analise['metricas']['prestacao_mensal']:.2f}€
- Incumprimentos: {analise['metricas']['incumprimento_total']:.2f}€
- Número de contratos: {analise['metricas']['n_contratos']}
- Número de instituições: {analise['metricas']['n_instituicoes']}

CONTRATOS ACTIVOS:
{contratos_resumo}

Elabora um relatório com estas secções (usa markdown com ## para títulos):

## Resumo da Situação Financeira
Avaliação geral clara e directa em 2-3 parágrafos. Explica o que significa o score, a taxa de esforço e o rácio dívida/rendimento para esta pessoa concretamente.

## Pontos de Atenção
Lista os principais riscos e preocupações identificados, explicando o impacto prático de cada um (ex: o que significam os créditos renegociados, ser avalista, ter múltiplos devedores).

## Plano de Acção Prioritário
3 a 5 acções concretas e específicas ordenadas por prioridade, com estimativa de impacto. Sê específico — menciona as instituições e valores reais.

## Perspectiva a Médio Prazo
Com base nos prazos dos contratos, como evolui a situação nos próximos 2-3 anos? Quando terminam os créditos mais pesados?

Usa linguagem clara, directa e empática. Evita jargão excessivo. O relatório deve ser útil para uma pessoa leiga mas também para apresentar a um banco ou intermediário de crédito."""

    def stream_response():
        with client.messages.stream(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for text in stream.text_stream:
                yield text

    return StreamingResponse(stream_response(), media_type="text/plain")


@app.post("/simular-consolidacao")
async def simular_consolidacao(
    pdf: UploadFile = File(...),
    rendimento_mensal: float = Form(...),
    taxa_nova: float = Form(default=7.5),
    prazo_anos: int = Form(default=7),
):
    """
    Simula a consolidação de todos os créditos pessoais num único crédito.
    Compara prestação actual vs nova e calcula poupança total.
    """
    if not pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="O ficheiro deve ser um PDF.")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await pdf.read())
        tmp_path = tmp.name

    try:
        crc = parse_crc(tmp_path)
        analise = analisar_crc(crc, rendimento_mensal)
    finally:
        os.unlink(tmp_path)

    contratos = analise["contratos"]

    # separa créditos pessoais (excluindo avalista e cartões)
    creditos_pessoais = [
        c for c in contratos
        if "pessoal" in c["produto"].lower()
        and "avalista" not in c["tipo_responsabilidade"].lower()
        and "fiador" not in c["tipo_responsabilidade"].lower()
        and c["total_em_divida"] > 0
    ]

    cartoes = [
        c for c in contratos
        if "cartão" in c["produto"].lower()
        and c["total_em_divida"] > 0
    ]

    # totais actuais
    total_divida_pessoal = sum(c["total_em_divida"] for c in creditos_pessoais)
    prestacao_actual_pessoal = sum(
        c["prestacao"] for c in creditos_pessoais if c["periodicidade"] == "Mensal"
    )
    total_divida_cartoes = sum(c["total_em_divida"] for c in cartoes)

    # simulação só créditos pessoais
    sim_pessoal = _calcular_consolidacao(
        total_divida_pessoal, taxa_nova, prazo_anos, prestacao_actual_pessoal
    )

    # simulação créditos pessoais + cartões
    total_tudo = total_divida_pessoal + total_divida_cartoes
    prestacao_actual_tudo = prestacao_actual_pessoal + sum(
        c["prestacao"] for c in cartoes if c["periodicidade"] == "Mensal"
    )
    sim_tudo = _calcular_consolidacao(
        total_tudo, taxa_nova, prazo_anos, prestacao_actual_tudo
    )

    # taxa de esforço resultante
    outras_prestacoes = analise["metricas"]["prestacao_mensal"] - prestacao_actual_tudo

    return {
        "situacao_actual": {
            "n_creditos_pessoais": len(creditos_pessoais),
            "n_cartoes_com_saldo": len(cartoes),
            "total_divida_pessoal": round(total_divida_pessoal, 2),
            "total_divida_cartoes": round(total_divida_cartoes, 2),
            "prestacao_mensal_actual": round(analise["metricas"]["prestacao_mensal"], 2),
            "taxa_esforco_actual": analise["metricas"]["taxa_esforco_pct"],
        },
        "parametros_simulacao": {
            "taxa_nova_pct": taxa_nova,
            "prazo_anos": prazo_anos,
        },
        "opcao_1_so_pessoais": {
            "descricao": f"Consolida {len(creditos_pessoais)} créditos pessoais ({total_divida_pessoal:,.2f}€)",
            **sim_pessoal,
            "taxa_esforco_resultante": round(
                (sim_pessoal["nova_prestacao_mensal"] + outras_prestacoes + total_divida_cartoes * 0.03)
                / rendimento_mensal * 100, 1
            ),
        },
        "opcao_2_pessoais_e_cartoes": {
            "descricao": f"Consolida créditos pessoais + cartões ({total_tudo:,.2f}€)",
            **sim_tudo,
            "taxa_esforco_resultante": round(
                (sim_tudo["nova_prestacao_mensal"] + outras_prestacoes)
                / rendimento_mensal * 100, 1
            ),
        },
        "contratos_incluidos": [
            {"instituicao": c["instituicao"], "produto": c["produto"],
             "divida": c["total_em_divida"], "prestacao": c["prestacao"]}
            for c in creditos_pessoais
        ],
        "cartoes_incluidos": [
            {"instituicao": c["instituicao"], "produto": c["produto"],
             "divida": c["total_em_divida"]}
            for c in cartoes
        ],
    }


def _calcular_consolidacao(total_divida, taxa_anual_pct, prazo_anos, prestacao_actual):
    """Calcula métricas de consolidação para um dado montante e condições."""
    if total_divida <= 0:
        return {
            "nova_prestacao_mensal": 0,
            "poupanca_mensal": 0,
            "custo_total_actual": 0,
            "custo_total_novo": 0,
            "poupanca_total_juros": 0,
        }

    taxa_mensal = taxa_anual_pct / 100 / 12
    n = prazo_anos * 12

    if taxa_mensal == 0:
        nova_prestacao = total_divida / n
    else:
        nova_prestacao = (total_divida * taxa_mensal * (1 + taxa_mensal) ** n) / ((1 + taxa_mensal) ** n - 1)

    poupanca_mensal = prestacao_actual - nova_prestacao

    # custo total estimado (prestação × meses restantes médio vs novo prazo)
    custo_novo = nova_prestacao * n
    # estimativa conservadora do custo actual (assume prazo médio restante de 5 anos)
    prazo_actual_estimado = 5 * 12
    custo_actual = prestacao_actual * prazo_actual_estimado

    return {
        "nova_prestacao_mensal": round(nova_prestacao, 2),
        "poupanca_mensal": round(poupanca_mensal, 2),
        "custo_total_actual_estimado": round(custo_actual, 2),
        "custo_total_novo": round(custo_novo, 2),
        "poupanca_total_estimada": round(custo_actual - custo_novo, 2),
    }
