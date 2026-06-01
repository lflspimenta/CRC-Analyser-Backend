# CRC Analyser — Backend

Parser e motor de análise financeira para o Mapa de Responsabilidades de Crédito
do Banco de Portugal.

## Ficheiros

| Ficheiro | O que faz |
|---|---|
| `parser.py` | Extracção do PDF + cálculo de métricas e recomendações |
| `main.py` | API REST com FastAPI (endpoint `/analisar`) |
| `requirements.txt` | Dependências Python |

## Instalação local

```bash
# 1. cria ambiente virtual
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. instala dependências
pip install -r requirements.txt

# 3. corre o servidor
uvicorn main:app --reload --port 8000
```

O servidor fica disponível em `http://localhost:8000`.
Documentação interactiva (Swagger) em `http://localhost:8000/docs`.

## Testar com curl

```bash
curl -X POST http://localhost:8000/analisar \
  -F "pdf=@Mapa_CRC.pdf" \
  -F "rendimento_mensal=1500"
```

## Deploy no Railway

1. Cria conta em railway.app
2. New Project → Deploy from GitHub repo
3. Railway detecta o Python automaticamente
4. Adiciona variável de ambiente PORT=8000 (opcional, o Railway define sozinho)
5. Adiciona um Procfile se necessário:
   ```
   web: uvicorn main:app --host 0.0.0.0 --port $PORT
   ```

## Deploy no Render

1. New Web Service → conecta o repo GitHub
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Plano Free funciona para começar

## Resposta da API (exemplo)

```json
{
  "titular": "NOME DO TITULAR",
  "referente_a": "janeiro de 2020",
  "rendimento_mensal": 1500,
  "metricas": {
    "divida_efetiva": 37987.30,
    "divida_potencial": 1057.13,
    "endividamento_total": 39044.43,
    "prestacao_mensal": 590.74,
    "incumprimento_total": 0.0,
    "taxa_esforco_pct": 39.38,
    "racio_divida_rendimento": 2.11,
    "score_saude": 76
  },
  "alertas": [
    {
      "nivel": "warn",
      "codigo": "TAXA_ESFORCO_ELEVADA",
      "msg": "Taxa de esforço elevada: 39.4% (limite: 35%)"
    }
  ],
  "recomendacoes": [...],
  "contratos": [...]
}
```

## Notas sobre o formato do CRC

- O PDF é gerado digitalmente pelo Banco de Portugal — não é scaneado,
  por isso não precisa de OCR.
- Cada página de detalhe contém um contrato.
- A última página é o quadro síntese (ignorada pelo parser, os dados já
  foram extraídos página a página).
- `fim = "9999-12-31"` indica produto revolving sem prazo (cartões).
- `periodicidade = "Outros"` é normal em cartões sem free-float.
