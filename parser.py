"""
parser.py — Motor de extracção e análise do CRC (Banco de Portugal)

Estratégia de dois níveis:
  1. Parser regex (rápido, gratuito) — extrai contratos por padrão de texto
  2. Fallback Claude API — activado automaticamente se o nº de contratos
     extraídos não bater com o total indicado no quadro síntese do PDF

Desta forma o sistema funciona correctamente para qualquer versão ou
formato do CRC, mesmo com layouts inesperados.
"""
from __future__ import annotations

import json
import os
import re
import anthropic
import pdfplumber


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_money(s: str) -> float:
    if not s or s.strip() in ("-", "Não Aplicável", "N/A", ""):
        return 0.0
    cleaned = re.sub(r"[^\d,]", "", s.strip()).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _extrair_texto_pdf(pdf_path: str) -> tuple[str, list[str]]:
    """Devolve (texto_completo, lista_de_textos_por_pagina)."""
    paginas = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            paginas.append(page.extract_text() or "")
    return "\n\n".join(paginas), paginas


def _total_esperado(texto_completo: str) -> int:
    """Lê 'Nº total de produtos financeiros comunicados: N' do quadro síntese."""
    m = re.search(r"Nº total de produtos financeiros comunicados:\s*(\d+)", texto_completo)
    return int(m.group(1)) if m else 0


# ─────────────────────────────────────────────────────────────────────────────
# Nível 1 — Parser regex
# ─────────────────────────────────────────────────────────────────────────────

def _parse_regex(paginas: list[str]) -> list[dict]:
    contratos = []
    inst_atual = ""

    for i, text in enumerate(paginas):
        if not text:
            continue
        if "Quadro Síntese" in text or (
            "Fim de relatório" in text and "Total em dívida" not in text
        ):
            continue

        m = re.search(
            r"Informação comunicada pela instituição:\s+(.+?)(?:\s*\(\d+\))", text
        )
        if m:
            inst_atual = m.group(1).strip()

        # remove rodapé/legenda mas preserva contratos
        text_limpo = re.sub(r"\nLegenda\n.*", "", text, flags=re.DOTALL)
        text_limpo = re.sub(r"\nFim de relatório\n.*", "", text_limpo, flags=re.DOTALL)
        text_limpo = re.sub(r"\nA informação prestada.*", "", text_limpo, flags=re.DOTALL)

        blocos = re.split(r"(?=Tipo de responsabilidade\s+\S)", text_limpo)

        for bloco in blocos:
            if "Total em dívida" not in bloco:
                continue

            c: dict = {"instituicao": inst_atual}

            m = re.search(r"Tipo de responsabilidade\s+(.+?)(?:\n|Produto)", bloco)
            c["tipo_responsabilidade"] = m.group(1).strip() if m else ""

            m = re.search(r"Produto financeiro\s+(.+?)(?:Garantias|Tipo de negociação|\n)", bloco)
            c["produto"] = m.group(1).strip() if m else ""

            m = re.search(r"Tipo de negociação\s+([\s\S]+?)(?:Em litígio judicial)", bloco)
            c["tipo_negociacao"] = re.sub(r'\s+', ' ', m.group(1)).strip() if m else ""

            m = re.search(r"Início\s+([\d-]+)", bloco)
            c["inicio"] = m.group(1) if m else ""
            m = re.search(r"Fim\s+([\d-]+)", bloco)
            c["fim"] = m.group(1) if m else ""
            c["fim_efetivo"] = None if c["fim"] == "9999-12-31" else c["fim"]

            m = re.search(r"Em litígio judicial\s+(Sim|Não)", bloco)
            c["em_litigio"] = (m.group(1) == "Sim") if m else False

            m = re.search(r"Nº devedores no contrato\s+(\d+)", bloco)
            c["n_devedores"] = int(m.group(1)) if m else 1

            m = re.search(r"Total em dívida\s+([\d\s.,]+€)", bloco)
            c["total_em_divida"] = _parse_money(m.group(1)) if m else 0.0
            m = re.search(r"do qual, em incumprimento\s+([\d\s.,]+€)", bloco)
            c["em_incumprimento"] = _parse_money(m.group(1)) if m else 0.0
            m = re.search(r"Vencido\s+([\d\s.,]+€)", bloco)
            c["vencido"] = _parse_money(m.group(1)) if m else 0.0
            m = re.search(r"Abatido ao ativo\s+([\d\s.,]+€)", bloco)
            c["abatido_ao_ativo"] = _parse_money(m.group(1)) if m else 0.0
            m = re.search(r"Potencial\s+([\d\s.,]+€)", bloco)
            c["potencial"] = _parse_money(m.group(1)) if m else 0.0
            m = re.search(r"Prestação\s+([\d\s.,]+€)", bloco)
            c["prestacao"] = _parse_money(m.group(1)) if m else 0.0
            m = re.search(r"Periodicidade\s+(\w+)", bloco)
            c["periodicidade"] = m.group(1) if m else ""
            c["tem_garantias"] = bool(re.search(r"\d{4}\s+[\d\s.,]+€\s+\d+", bloco))

            contratos.append(c)

    return contratos


# ─────────────────────────────────────────────────────────────────────────────
# Nível 2 — Fallback Claude API
# ─────────────────────────────────────────────────────────────────────────────

PROMPT_EXTRACAO = """Tens à frente o texto extraído de um Mapa de Responsabilidades de Crédito (CRC) do Banco de Portugal.

Extrai TODOS os contratos de crédito e devolve um array JSON com exactamente esta estrutura por contrato:

{
  "instituicao": "nome da instituição",
  "produto": "tipo de produto financeiro",
  "tipo_responsabilidade": "Devedor / Avalista / fiador / etc",
  "tipo_negociacao": "Geral / Renegociação por ... / etc",
  "inicio": "YYYY-MM-DD",
  "fim": "YYYY-MM-DD ou 9999-12-31",
  "fim_efetivo": "YYYY-MM-DD ou null se 9999-12-31",
  "em_litigio": false,
  "n_devedores": 1,
  "total_em_divida": 0.0,
  "em_incumprimento": 0.0,
  "vencido": 0.0,
  "abatido_ao_ativo": 0.0,
  "potencial": 0.0,
  "prestacao": 0.0,
  "periodicidade": "Mensal / Outros / vazio",
  "tem_garantias": false
}

Regras:
- Todos os valores monetários em float (ex: 1465.67, não "1 465,67 €")
- Se um campo não existir ou for "-", usa 0.0 para números e "" para strings
- fim_efetivo é null quando fim é "9999-12-31"
- Inclui contratos onde és Avalista/Fiador
- NÃO incluas dados do quadro síntese, apenas os contratos individuais
- Responde APENAS com o array JSON, sem texto adicional, sem markdown

Texto do CRC:
"""


def _parse_claude(texto_completo: str) -> list[dict]:
    """Usa a Claude API para extrair contratos quando o parser regex falha."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY não configurada")

    client = anthropic.Anthropic(api_key=api_key)

    # Remove páginas de quadro síntese para poupar tokens
    texto_limpo = re.sub(
        r"Resumo das Responsabilidades.*?Fim de relatório",
        "",
        texto_completo,
        flags=re.DOTALL,
    )

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": PROMPT_EXTRACAO + texto_limpo
        }]
    )

    resposta = message.content[0].text.strip()

    # limpa possíveis marcadores de código
    resposta = re.sub(r"^```(?:json)?\s*", "", resposta)
    resposta = re.sub(r"\s*```$", "", resposta)

    contratos = json.loads(resposta)
    return contratos


# ─────────────────────────────────────────────────────────────────────────────
# Ponto de entrada principal
# ─────────────────────────────────────────────────────────────────────────────

def parse_crc(pdf_path: str) -> dict:
    """
    Extrai dados do CRC com validação automática e fallback para IA.

    Fluxo:
      1. Extrai texto do PDF
      2. Lê o total esperado do quadro síntese
      3. Corre o parser regex
      4. Se o nº de contratos não bater → usa Claude API como fallback
      5. Devolve dicionário com titular + contratos
    """
    texto_completo, paginas = _extrair_texto_pdf(pdf_path)

    # cabeçalho
    titular: dict = {"nome": "", "nif": "", "referente_a": "", "data_emissao": ""}
    if paginas:
        text0 = paginas[0]
        m = re.search(r"Nome:\s+(.+?)\s+País da Entidade", text0)
        if m: titular["nome"] = m.group(1).strip()
        m = re.search(r"Nº de Identificação:\s+(\d+)", text0)
        if m: titular["nif"] = m.group(1)
        m = re.search(r"referentes a\s+(.+?)(?:\n|$)", text0)
        if m: titular["referente_a"] = m.group(1).strip()
        m = re.search(r"Data de Emissão:\s+([\d\-: ]+)", text0)
        if m: titular["data_emissao"] = m.group(1).strip()

    total_esperado = _total_esperado(texto_completo)

    # nível 1 — regex
    contratos = _parse_regex(paginas)

    # nível 2 — fallback IA se necessário
    metodo = "regex"
    if total_esperado > 0 and len(contratos) != total_esperado:
        print(f"[parser] regex extraiu {len(contratos)}, esperado {total_esperado} → a usar Claude API")
        try:
            contratos = _parse_claude(texto_completo)
            metodo = "claude"
        except Exception as e:
            print(f"[parser] fallback Claude falhou: {e} — a usar resultado regex")

    titular["contratos"] = contratos
    titular["metodo_extracao"] = metodo
    titular["total_esperado"] = total_esperado
    titular["total_extraido"] = len(contratos)
    return titular


# ─────────────────────────────────────────────────────────────────────────────
# Análise financeira
# ─────────────────────────────────────────────────────────────────────────────

def analisar_crc(crc: dict, rendimento_mensal: float) -> dict:
    contratos = crc.get("contratos", [])

    divida_efetiva   = sum(c["total_em_divida"] for c in contratos)
    divida_potencial = sum(c["potencial"] for c in contratos)
    incumprimento    = sum(c["em_incumprimento"] for c in contratos)
    vencido          = sum(c["vencido"] for c in contratos)
    abatido          = sum(c["abatido_ao_ativo"] for c in contratos)
    prestacao_mensal = sum(
        c["prestacao"] for c in contratos if c["periodicidade"] == "Mensal"
    )

    taxa_esforco = round(prestacao_mensal / rendimento_mensal * 100, 2) if rendimento_mensal > 0 else 0.0
    racio_divida = round(divida_efetiva / (rendimento_mensal * 12), 2) if rendimento_mensal > 0 else 0.0

    score = 100
    if taxa_esforco > 35: score -= min(25, round((taxa_esforco - 35) * 1.5))
    if taxa_esforco > 50: score -= 15
    if racio_divida > 3:  score -= min(20, round((racio_divida - 3) * 5))
    if incumprimento > 0: score -= 40
    if abatido > 0:       score -= 20
    n_cartoes = sum(1 for c in contratos if "Cartão" in c["produto"])
    if n_cartoes > 2:     score -= 10
    if any(c["em_litigio"] for c in contratos): score -= 20
    score = max(0, min(100, score))

    alertas = []
    if incumprimento > 0 or vencido > 0 or abatido > 0:
        alertas.append({"nivel": "crit", "codigo": "INCUMPRIMENTO",
            "msg": f"Incumprimento activo: {incumprimento:,.2f} €"})
    if taxa_esforco > 50:
        alertas.append({"nivel": "crit", "codigo": "TAXA_ESFORCO_CRITICA",
            "msg": f"Taxa de esforço crítica: {taxa_esforco:.1f}% (limite: 35%)"})
    elif taxa_esforco > 35:
        alertas.append({"nivel": "warn", "codigo": "TAXA_ESFORCO_ELEVADA",
            "msg": f"Taxa de esforço elevada: {taxa_esforco:.1f}% (limite: 35%)"})
    if racio_divida > 3:
        alertas.append({"nivel": "warn", "codigo": "RACIO_DIVIDA",
            "msg": f"Rácio dívida/rendimento anual: {racio_divida:.1f}× (alerta: 3×)"})
    cartoes_sem_saldo = [
        c for c in contratos
        if "Cartão" in c["produto"] and c["total_em_divida"] == 0 and c["potencial"] > 0
    ]
    if cartoes_sem_saldo:
        total_pot = sum(c["potencial"] for c in cartoes_sem_saldo)
        alertas.append({"nivel": "info", "codigo": "CARTOES_SEM_SALDO",
            "msg": f"{len(cartoes_sem_saldo)} cartão(ões) sem saldo mas com limite ({total_pot:,.2f} €)"})
    conjuntos = [c for c in contratos if c["n_devedores"] > 1]
    if conjuntos:
        alertas.append({"nivel": "info", "codigo": "CONTRATO_CONJUNTO",
            "msg": f"{len(conjuntos)} contrato(s) com múltiplos devedores — responsabilidade solidária"})
    avalistas = [
        c for c in contratos
        if "avalista" in c.get("tipo_responsabilidade", "").lower()
        or "fiador" in c.get("tipo_responsabilidade", "").lower()
    ]
    if avalistas:
        alertas.append({"nivel": "info", "codigo": "AVALISTA",
            "msg": f"És avalista/fiador em {len(avalistas)} contrato(s) — monitoriza o devedor principal"})
    renegociacoes = [c for c in contratos if "Renegociação" in c.get("tipo_negociacao", "")]
    if renegociacoes:
        alertas.append({"nivel": "warn", "codigo": "RENEGOCIACAO",
            "msg": f"{len(renegociacoes)} contrato(s) com renegociação por prevenção/incumprimento"})

    # aviso se fallback foi usado
    if crc.get("metodo_extracao") == "claude":
        alertas.append({"nivel": "info", "codigo": "FALLBACK_IA",
            "msg": "Extracção assistida por IA (formato do CRC não standard)"})

    recomendacoes = _gerar_recomendacoes(contratos, taxa_esforco, racio_divida, incumprimento)

    return {
        "titular": crc.get("nome", ""),
        "referente_a": crc.get("referente_a", ""),
        "data_emissao": crc.get("data_emissao", ""),
        "rendimento_mensal": rendimento_mensal,
        "metodo_extracao": crc.get("metodo_extracao", "regex"),
        "metricas": {
            "divida_efetiva": round(divida_efetiva, 2),
            "divida_potencial": round(divida_potencial, 2),
            "endividamento_total": round(divida_efetiva + divida_potencial, 2),
            "prestacao_mensal": round(prestacao_mensal, 2),
            "incumprimento_total": round(incumprimento, 2),
            "vencido_total": round(vencido, 2),
            "abatido_total": round(abatido, 2),
            "n_contratos": len(contratos),
            "n_instituicoes": len(set(c["instituicao"] for c in contratos)),
            "taxa_esforco_pct": taxa_esforco,
            "racio_divida_rendimento": racio_divida,
            "score_saude": score,
        },
        "alertas": alertas,
        "recomendacoes": recomendacoes,
        "contratos": contratos,
    }


def _gerar_recomendacoes(contratos, taxa_esforco, racio_divida, incumprimento):
    recs = []

    if incumprimento > 0:
        recs.append({"prioridade": 0, "impacto": "critico",
            "titulo": "Regularizar incumprimentos urgentemente",
            "descricao": "Os incumprimentos activos impedem a aprovação de qualquer novo crédito. Contacta as instituições para negociar um plano de pagamento.",
            "codigo": "REGULARIZAR_INCUMPRIMENTO"})

    cartoes_sem_saldo = [c for c in contratos if "Cartão" in c["produto"] and c["total_em_divida"] == 0]
    if cartoes_sem_saldo:
        recs.append({"prioridade": 1, "impacto": "alto",
            "titulo": f"Cancelar {len(cartoes_sem_saldo)} cartão(ões) sem utilização",
            "descricao": "Cartões com saldo zero mas limite activo contam como responsabilidades potenciais. Cancelá-los melhora imediatamente o perfil de crédito.",
            "codigo": "CANCELAR_CARTOES",
            "instituicoes": [c["instituicao"] for c in cartoes_sem_saldo]})

    if taxa_esforco > 35:
        recs.append({"prioridade": 2, "impacto": "alto",
            "titulo": "Taxa de esforço elevada — renegociar prazos",
            "descricao": f"Com {taxa_esforco:.1f}% de taxa de esforço, a aprovação de novos créditos fica comprometida. Alargar o prazo dos créditos existentes pode reduzir as prestações mensais.",
            "codigo": "RENEGOCIAR_PRAZO"})

    cartoes_com_saldo = [c for c in contratos if "Cartão" in c["produto"] and c["total_em_divida"] > 0]
    if cartoes_com_saldo:
        total = sum(c["total_em_divida"] for c in cartoes_com_saldo)
        recs.append({"prioridade": 3, "impacto": "medio",
            "titulo": "Transferir saldo de cartão para crédito pessoal",
            "descricao": f"Os cartões têm taxas de 15–24%/ano. Transferir {total:,.2f} € para um crédito pessoal a taxa mais baixa pode poupar centenas de euros em juros.",
            "codigo": "REFINANCIAR_CARTAO", "valor_estimado": total})

    renegociacoes = [c for c in contratos if "Renegociação" in c.get("tipo_negociacao", "")]
    if renegociacoes:
        recs.append({"prioridade": 4, "impacto": "medio",
            "titulo": f"{len(renegociacoes)} crédito(s) com renegociação por incumprimento",
            "descricao": "Créditos renegociados indicam historial de dificuldades. Manter os pagamentos em dia é crítico para recuperar o perfil de crédito junto dos bancos.",
            "codigo": "CREDITOS_RENEGOCIADOS"})

    if taxa_esforco <= 35 and incumprimento == 0 and racio_divida <= 3:
        recs.append({"prioridade": 5, "impacto": "baixo",
            "titulo": "Perfil favorável para novo crédito",
            "descricao": "A situação financeira está equilibrada. Podes explorar a antecipação de capital em créditos com taxa mais alta, ou simular um novo crédito com boa probabilidade de aprovação.",
            "codigo": "OTIMIZAR_PERFIL"})

    return sorted(recs, key=lambda r: r["prioridade"])
