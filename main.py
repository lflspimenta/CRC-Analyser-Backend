"""
main.py — API REST para o analisador de CRC

Endpoints:
  POST /analisar   — recebe PDF + rendimento, devolve análise completa
  GET  /health     — verificação de estado

Instalar dependências:
  pip install fastapi uvicorn python-multipart pdfplumber

Correr localmente:
  uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import tempfile, os

from parser import parse_crc, analisar_crc

app = FastAPI(
    title="CRC Analyser API",
    description="Análise automática do Mapa de Responsabilidades de Crédito do Banco de Portugal",
    version="1.0.0",
)

# CORS — ajusta as origens em produção
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # em produção: ["https://teudominio.com"]
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analisar")
async def analisar(
    pdf: UploadFile = File(..., description="PDF do CRC (Banco de Portugal)"),
    rendimento_mensal: float = Form(..., description="Rendimento líquido mensal em €"),
):
    """
    Recebe o PDF do CRC e o rendimento mensal.
    Devolve a análise financeira completa em JSON.
    """
    # validações básicas
    if not pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="O ficheiro deve ser um PDF.")
    if rendimento_mensal <= 0:
        raise HTTPException(status_code=400, detail="O rendimento deve ser positivo.")
    if rendimento_mensal > 100_000:
        raise HTTPException(status_code=400, detail="Rendimento fora do intervalo esperado.")

    # guarda o upload num ficheiro temporário
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await pdf.read())
        tmp_path = tmp.name

    try:
        crc = parse_crc(tmp_path)

        # validação mínima: tem de ter pelo menos 1 contrato
        if not crc.get("contratos"):
            raise HTTPException(
                status_code=422,
                detail="Não foi possível extrair contratos do PDF. "
                       "Confirma que é um CRC do Banco de Portugal.",
            )

        return analisar_crc(crc, rendimento_mensal)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar o PDF: {str(e)}")
    finally:
        os.unlink(tmp_path)
