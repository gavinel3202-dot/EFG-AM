from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict
from datetime import datetime

app = FastAPI(
    title="API Senior Fitness Test - Observatorio INDER",
    version="1.0.0"
)

class RegistroSFT(BaseModel):
    usuario_id: int
    peso: float
    talla: float
    circunferencia_pantorrilla: float
    resultados_pruebas: Dict[str, float]
    medicamentos: List[str]

def contar_pruebas_bajo_percentil_25(resultados: Dict[str, float]) -> int:
    return sum(1 for valor in resultados.values() if valor < 25)

def evaluar_obesidad_sarcopenica(datos: RegistroSFT) -> str:
    imc = datos.peso / (datos.talla ** 2)
    es_obesidad = imc > 28
    es_sarcopenia = datos.circunferencia_pantorrilla < 31
    pruebas_bajas = contar_pruebas_bajo_percentil_25(datos.resultados_pruebas)
    
    if es_obesidad and es_sarcopenia and pruebas_bajas >= 2:
        return "Crítico" if imc > 30 else "Alto"
    return "Bajo"

@app.post("/procesar-test")
async def procesar_test(datos: RegistroSFT):
    try:
        riesgo = evaluar_obesidad_sarcopenica(datos)
        imc_calculado = round(datos.peso / (datos.talla ** 2), 2)
        return {
            "status": "Evaluación completada",
            "usuario_id": datos.usuario_id,
            "imc": imc_calculado,
            "alerta_riesgo": riesgo,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {"message": "API Senior Fitness Test operando correctamente en Vercel"}
