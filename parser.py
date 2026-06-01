"""
parser.py — Motor de extracção e análise do CRC (Banco de Portugal)

Utiliza pdfplumber para extracção de texto (PDF gerado digitalmente,
sem necessidade de OCR). Cobre todas as variantes conhecidas do formato.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional
import pdfplumber


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_money(s: str) -> float:
    """Converte '1 465,67 €' → 1465.67, tratando '-' e 'Não Aplicável' como 0."""
    if not s or s.strip() in ("-", "Não Aplicável", "N/A", ""):
        return 0.0
    cleaned = re.sub(r"[^\d,]", "", s.strip()).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _find(pattern: str, text: str, group: int = 1) -> Optional[str]:
    """Extrai o primeiro grupo de uma regex, ou None."""
    m = re.search(pattern, text)
    return m.group(group).strip() if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Modelo de dados
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Contrato:
    instituicao: str = ""
    codigo_instituicao: str = ""
    produto: str = ""
    tipo_responsabilidade: str = ""   # Devedor, Avalista, Garante…
    tipo_negociacao: str = ""
    inicio: str = ""
    fim: str = ""                     # "9999-12-31" = sem prazo (revolving)
    em_litigio: bool = False
    n_devedores: int = 1
    total_em_divida: float = 0.0
    em_incumprimento: float = 0.0
    vencido: float = 0.0
    abatido_ao_ativo: float = 0.0
    potencial: float = 0.0            # limite disponível em cartões
    prestacao: float = 0.0
    periodicidade: str = ""           # Mensal, Trimestral, Outros…
    tem_garantias: bool = False

    # campos derivados (preenchidos pelo analisador)
    fim_efetivo: Optional[str] = None  # None se revolving
    meses_restantes: Optional[int] = None


@dataclass
class TitularCRC:
    nome: str = ""
    nif: str = ""
    referente_a: str = ""             # "janeiro de 2020"
    data_emissao: str = ""
    contratos: list[Contrato] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Parsing de cada página de contrato
# ─────────────────────────────────────────────────────────────────────────────

def _parse_contrato(text: str) -> Optional[Contrato]:
    """Extrai um Contrato a partir do texto de uma página do CRC."""
    if "Informação comunicada pela instituição:" not in text:
        return None

    c = Contrato()

    # instituição e código
    m = re.search(
        r"Informação comunicada pela instituição:\s+(.+?)\s*\((\d+)\)", text
    )
    if m:
        c.instituicao = m.group(1).strip()
        c.codigo_instituicao = m.group(2)

    # produto — termina antes de "Garantias" ou "Em litígio"
    c.produto = _find(
        r"Produto financeiro\s+(.+?)(?=\s*Garantias|\s*Em litígio)", text
    ) or ""

    # tipo de responsabilidade
    c.tipo_responsabilidade = _find(r"Tipo de responsabilidade\s+(\S+)", text) or ""

    # tipo de negociação
    c.tipo_negociacao = _find(
        r"Tipo de negociação\s+(.+?)(?=\s*Em litígio)", text
    ) or ""

    # datas
    c.inicio = _find(r"Início\s+([\d-]+)", text) or ""
    c.fim    = _find(r"Fim\s+([\d-]+)", text) or ""

    # sem prazo real se fim for 9999
    c.fim_efetivo = None if c.fim == "9999-12-31" else c.fim

    # litígio
    litigio = _find(r"Em litígio judicial\s+(Sim|Não)", text)
    c.em_litigio = litigio == "Sim"

    # nº devedores
    nd = _find(r"Nº devedores no contrato\s+(\d+)", text)
    c.n_devedores = int(nd) if nd else 1

    # montantes
    c.total_em_divida  = _parse_money(_find(r"Total em dívida\s+([\d\s.,]+€)", text) or "")
    c.em_incumprimento = _parse_money(_find(r"do qual, em incumprimento\s+([\d\s.,]+€)", text) or "")
    c.vencido          = _parse_money(_find(r"Vencido\s+([\d\s.,]+€)", text) or "")
    c.abatido_ao_ativo = _parse_money(_find(r"Abatido ao ativo\s+([\d\s.,]+€)", text) or "")
    c.potencial        = _parse_money(_find(r"Potencial\s+([\d\s.,]+€)", text) or "")
    c.prestacao        = _parse_money(_find(r"Prestação\s+([\d\s.,]+€)", text) or "")

    c.periodicidade = _find(r"Periodicidade\s+(\S+)", text) or ""

    # garantias: presentes quando a tabela não mostra só "- - -"
    c.tem_garantias = "- - -" not in text

    return c


# ─────────────────────────────────────────────────────────────────────────────
# Parsing do cabeçalho (primeira página)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_cabecalho(text: str, titular: TitularCRC) -> None:
    m = re.search(r"Nome:\s+(.+)", text)
    if m:
        titular.nome = m.group(1).strip()

    m = re.search(r"Nº de Identificação:\s+(\d+)", text)
    if m:
        titular.nif = m.group(1)

    m = re.search(r"referentes a\s+(.+?)(?:\n|$)", text)
    if m:
        titular.referente_a = m.group(1).strip()

    m = re.search(r"Data de Emissão:\s+([\d\-: ]+)", text)
    if m:
        titular.data_emissao = m.group(1).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Ponto de entrada principal
# ─────────────────────────────────────────────────────────────────────────────

def parse_crc(pdf_path: str) -> dict:
    """
    Lê um PDF do CRC e devolve um dicionário com todos os dados extraídos.

    Args:
        pdf_path: caminho para o ficheiro PDF

    Returns:
        Dicionário serializável com titular + lista de contratos
    """
    titular = TitularCRC()

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""

            # cabeçalho lido a partir da primeira página
            if i == 0:
                _parse_cabecalho(text, titular)

            # tenta extrair contrato de qualquer página
            contrato = _parse_contrato(text)
            if contrato:
                titular.contratos.append(contrato)

    return asdict(titular)


# ─────────────────────────────────────────────────────────────────────────────
# Análise financeira
# ─────────────────────────────────────────────────────────────────────────────

def analisar_crc(crc: dict, rendimento_mensal: float) -> dict:
    """
    Calcula métricas financeiras e gera alertas a partir dos dados do CRC.

    Args:
        crc: resultado de parse_crc()
        rendimento_mensal: rendimento líquido mensal do titular (em €)

    Returns:
        Dicionário com métricas, score, alertas e recomendações
    """
    contratos = crc.get("contratos", [])

    # ── totais ────────────────────────────────────────────────────────────────
    divida_efetiva   = sum(c["total_em_divida"] for c in contratos)
    divida_potencial = sum(c["potencial"] for c in contratos)
    incumprimento    = sum(c["em_incumprimento"] for c in contratos)
    vencido          = sum(c["vencido"] for c in contratos)
    abatido          = sum(c["abatido_ao_ativo"] for c in contratos)

    # apenas contratos mensais contam na taxa de esforço
    prestacao_mensal = sum(
        c["prestacao"] for c in contratos
        if c["periodicidade"] == "Mensal"
    )

    # ── métricas ──────────────────────────────────────────────────────────────
    taxa_esforco = (
        round(prestacao_mensal / rendimento_mensal * 100, 2)
        if rendimento_mensal > 0 else 0.0
    )
    racio_divida_rendimento = (
        round(divida_efetiva / (rendimento_mensal * 12), 2)
        if rendimento_mensal > 0 else 0.0
    )

    # ── score 0-100 ───────────────────────────────────────────────────────────
    score = 100

    # taxa de esforço
    if taxa_esforco > 35:
        score -= min(25, round((taxa_esforco - 35) * 1.5))
    if taxa_esforco > 50:
        score -= 15

    # rácio dívida/rendimento
    if racio_divida_rendimento > 3:
        score -= min(20, round((racio_divida_rendimento - 3) * 5))

    # incumprimento e abatido são críticos
    if incumprimento > 0:
        score -= 40
    if abatido > 0:
        score -= 20

    # muitos cartões reduzem aprovação de habitação
    n_cartoes = sum(1 for c in contratos if "Cartão" in c["produto"])
    if n_cartoes > 2:
        score -= 10

    # créditos em litígio
    if any(c["em_litigio"] for c in contratos):
        score -= 20

    score = max(0, min(100, score))

    # ── alertas ───────────────────────────────────────────────────────────────
    alertas = []

    if incumprimento > 0 or vencido > 0 or abatido > 0:
        alertas.append({
            "nivel": "crit",
            "codigo": "INCUMPRIMENTO",
            "msg": (
                f"Incumprimento activo: {incumprimento:,.2f} €"
                + (f" | Vencido: {vencido:,.2f} €" if vencido > 0 else "")
                + (f" | Abatido ao activo: {abatido:,.2f} €" if abatido > 0 else "")
            ),
        })

    if taxa_esforco > 50:
        alertas.append({
            "nivel": "crit",
            "codigo": "TAXA_ESFORCO_CRITICA",
            "msg": f"Taxa de esforço crítica: {taxa_esforco:.1f}% (limite: 35%)",
        })
    elif taxa_esforco > 35:
        alertas.append({
            "nivel": "warn",
            "codigo": "TAXA_ESFORCO_ELEVADA",
            "msg": f"Taxa de esforço elevada: {taxa_esforco:.1f}% (limite: 35%)",
        })

    if racio_divida_rendimento > 3:
        alertas.append({
            "nivel": "warn",
            "codigo": "RACIO_DIVIDA",
            "msg": f"Rácio dívida/rendimento anual: {racio_divida_rendimento:.1f}× (alerta: 3×)",
        })

    cartoes_sem_saldo = [
        c for c in contratos
        if "Cartão" in c["produto"] and c["total_em_divida"] == 0 and c["potencial"] > 0
    ]
    if cartoes_sem_saldo:
        total_pot_cartoes = sum(c["potencial"] for c in cartoes_sem_saldo)
        alertas.append({
            "nivel": "info",
            "codigo": "CARTOES_SEM_SALDO",
            "msg": (
                f"{len(cartoes_sem_saldo)} cartão(ões) sem saldo mas com limite disponível "
                f"({total_pot_cartoes:,.2f} €) — contam como responsabilidade potencial"
            ),
        })

    contratos_conjunto = [c for c in contratos if c["n_devedores"] > 1]
    if contratos_conjunto:
        alertas.append({
            "nivel": "info",
            "codigo": "CONTRATO_CONJUNTO",
            "msg": (
                f"{len(contratos_conjunto)} contrato(s) com múltiplos devedores "
                "— responsabilidade solidária pela totalidade da dívida"
            ),
        })

    litigios = [c for c in contratos if c["em_litigio"]]
    if litigios:
        alertas.append({
            "nivel": "crit",
            "codigo": "EM_LITIGIO",
            "msg": f"{len(litigios)} contrato(s) em litígio judicial",
        })

    # ── recomendações ─────────────────────────────────────────────────────────
    recomendacoes = _gerar_recomendacoes(
        contratos, taxa_esforco, racio_divida_rendimento, incumprimento
    )

    return {
        "titular": crc.get("nome", ""),
        "referente_a": crc.get("referente_a", ""),
        "data_emissao": crc.get("data_emissao", ""),
        "rendimento_mensal": rendimento_mensal,
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
            "racio_divida_rendimento": racio_divida_rendimento,
            "score_saude": score,
        },
        "alertas": alertas,
        "recomendacoes": recomendacoes,
        "contratos": contratos,
    }


def _gerar_recomendacoes(
    contratos: list,
    taxa_esforco: float,
    racio_divida: float,
    incumprimento: float,
) -> list[dict]:
    """Gera recomendações priorizadas com base no perfil de crédito."""
    recs = []

    # cartões sem saldo → cancelar
    cartoes_sem_saldo = [
        c for c in contratos
        if "Cartão" in c["produto"] and c["total_em_divida"] == 0
    ]
    if cartoes_sem_saldo:
        recs.append({
            "prioridade": 1,
            "impacto": "alto",
            "titulo": f"Cancelar {len(cartoes_sem_saldo)} cartão(ões) sem utilização",
            "descricao": (
                "Cartões com saldo zero mas limite activo contam como responsabilidades "
                "potenciais no CRC. Cancelá-los reduz imediatamente este valor e melhora "
                "o perfil para novos créditos, nomeadamente habitação."
            ),
            "codigo": "CANCELAR_CARTOES",
            "instituicoes": [c["instituicao"] for c in cartoes_sem_saldo],
        })

    # taxa esforço elevada → consolidar ou renegociar
    if taxa_esforco > 35:
        creditos_pessoais = [
            c for c in contratos
            if c["produto"] in ("Crédito pessoal", "Crédito automóvel")
        ]
        if len(creditos_pessoais) >= 2:
            recs.append({
                "prioridade": 2,
                "impacto": "alto",
                "titulo": "Consolidar créditos pessoais/automóvel",
                "descricao": (
                    "Juntar múltiplos créditos num único com taxa mais baixa pode reduzir "
                    "a prestação mensal total e simplificar a gestão financeira."
                ),
                "codigo": "CONSOLIDAR_CREDITOS",
                "valor_estimado": sum(c["total_em_divida"] for c in creditos_pessoais),
            })
        else:
            recs.append({
                "prioridade": 2,
                "impacto": "medio",
                "titulo": "Renegociar prazo para reduzir prestação",
                "descricao": (
                    "Alargar o prazo do(s) crédito(s) existente(s) pode baixar a prestação "
                    "mensal e a taxa de esforço, embora aumente o custo total em juros."
                ),
                "codigo": "RENEGOCIAR_PRAZO",
            })

    # cartão com saldo → refinanciar
    cartoes_com_saldo = [
        c for c in contratos
        if "Cartão" in c["produto"] and c["total_em_divida"] > 0
    ]
    if cartoes_com_saldo:
        total_cartoes = sum(c["total_em_divida"] for c in cartoes_com_saldo)
        recs.append({
            "prioridade": 3,
            "impacto": "medio",
            "titulo": "Transferir saldo de cartão para crédito pessoal",
            "descricao": (
                f"Os cartões de crédito têm taxas de 15–24% ao ano. "
                f"Transferir {total_cartoes:,.2f} € para um crédito pessoal "
                "a 6–12% pode poupar centenas de euros em juros."
            ),
            "codigo": "REFINANCIAR_CARTAO",
            "valor_estimado": total_cartoes,
        })

    # incumprimento → regularizar
    if incumprimento > 0:
        recs.insert(0, {
            "prioridade": 0,
            "impacto": "critico",
            "titulo": "Regularizar incumprimentos urgentemente",
            "descricao": (
                "Os incumprimentos activos impedem a aprovação de qualquer novo crédito "
                "e agravam o historial no CRC. Contactar as instituições para negociar "
                "um plano de pagamento é a prioridade absoluta."
            ),
            "codigo": "REGULARIZAR_INCUMPRIMENTO",
        })

    # perfil saudável → optimizar
    if taxa_esforco <= 35 and incumprimento == 0 and racio_divida <= 3:
        recs.append({
            "prioridade": 4,
            "impacto": "baixo",
            "titulo": "Perfil favorável para novo crédito",
            "descricao": (
                "A situação financeira está equilibrada. Podes explorar a antecipação "
                "de capital em créditos com taxa mais alta, ou simular um novo crédito "
                "habitação/automóvel com boa probabilidade de aprovação."
            ),
            "codigo": "OTIMIZAR_PERFIL",
        })

    return sorted(recs, key=lambda r: r["prioridade"])
