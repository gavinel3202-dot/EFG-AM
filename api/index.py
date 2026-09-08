import os
from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import Client, create_client


# ==========================================================
# CONFIGURACIÓN GENERAL
# ==========================================================

app = FastAPI(
    title="API Senior Fitness Test - Observatorio INDER",
    version="2.0.0",
    description=(
        "API Python para procesamiento, almacenamiento, consulta "
        "y actualización de valoraciones Senior Fitness Test."
    ),
)


# ==========================================================
# CORS
# ==========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Ajustaremos esto cuando tengamos la interfaz definitiva
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================================
# SUPABASE
# ==========================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")


def get_supabase() -> Client:

    if not SUPABASE_URL:
        raise HTTPException(
            status_code=500,
            detail="SUPABASE_URL no está configurada en Vercel",
        )

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=500,
            detail="SUPABASE_SERVICE_ROLE_KEY no está configurada en Vercel",
        )

    return create_client(
        SUPABASE_URL,
        SUPABASE_SERVICE_ROLE_KEY,
    )


# ==========================================================
# FUNCIONES AUXILIARES
# ==========================================================

def calcular_edad(fecha_nacimiento: date) -> int:

    hoy = date.today()

    return (
        hoy.year
        - fecha_nacimiento.year
        - (
            (hoy.month, hoy.day)
            <
            (fecha_nacimiento.month, fecha_nacimiento.day)
        )
    )


def modelo_a_dict(modelo, exclude_none=True):

    if hasattr(modelo, "model_dump"):
        return modelo.model_dump(exclude_none=exclude_none)

    return modelo.dict(exclude_none=exclude_none)


def registrar_auditoria(
    accion: str,
    evaluation_id: Optional[str] = None,
    detalles: Optional[Dict[str, Any]] = None,
):

    try:

        supabase = get_supabase()

        supabase.table("auditoria").insert(
            {
                "accion": accion,
                "evaluation_id": evaluation_id,
                "detalles": detalles or {},
            }
        ).execute()

    except Exception:

        # Un error de auditoría no debe impedir una valoración.
        pass


# ==========================================================
# MODELOS EXISTENTES
# ==========================================================

class RegistroSFT(BaseModel):

    usuario_id: int

    peso: float

    talla: float

    circunferencia_pantorrilla: float

    resultados_pruebas: Dict[str, float]

    medicamentos: List[str]


# ==========================================================
# MODELOS DE EVALUACIONES
# ==========================================================

class EvaluacionCrear(BaseModel):

    documento: str

    nombres: str

    fecha_nacimiento: date

    sexo: Literal["F", "M"]

    edad: Optional[int] = None

    datos: Dict[str, Any] = Field(default_factory=dict)

    interpretacion: Dict[str, Any] = Field(default_factory=dict)

    consentimiento: bool = False


class EvaluacionActualizar(BaseModel):

    documento: Optional[str] = None

    nombres: Optional[str] = None

    fecha_nacimiento: Optional[date] = None

    sexo: Optional[Literal["F", "M"]] = None

    edad: Optional[int] = None

    datos: Optional[Dict[str, Any]] = None

    interpretacion: Optional[Dict[str, Any]] = None

    consentimiento: Optional[bool] = None


# ==========================================================
# MODELOS DE CAMPOS CONFIGURABLES
# ==========================================================

class CampoCrear(BaseModel):

    clave: str

    etiqueta: str

    grupo: str = "Otros"

    tipo: Literal[
        "texto",
        "texto_largo",
        "numero",
        "entero",
        "seleccion",
        "checkbox",
    ] = "texto"

    unidad: Optional[str] = None

    opciones: List[str] = Field(default_factory=list)

    requerido: bool = False

    activo: bool = True

    orden: int = 100


class CampoActualizar(BaseModel):

    etiqueta: Optional[str] = None

    grupo: Optional[str] = None

    tipo: Optional[
        Literal[
            "texto",
            "texto_largo",
            "numero",
            "entero",
            "seleccion",
            "checkbox",
        ]
    ] = None

    unidad: Optional[str] = None

    opciones: Optional[List[str]] = None

    requerido: Optional[bool] = None

    activo: Optional[bool] = None

    orden: Optional[int] = None


# ==========================================================
# PROCESAMIENTO SFT EXISTENTE
# ==========================================================

def contar_pruebas_bajo_percentil_25(
    resultados: Dict[str, float],
) -> int:

    # Esta función presupone que los valores recibidos
    # corresponden a percentiles.
    return sum(
        1
        for valor in resultados.values()
        if valor < 25
    )


def evaluar_obesidad_sarcopenica(
    datos: RegistroSFT,
) -> str:

    imc = datos.peso / (datos.talla ** 2)

    es_obesidad = imc > 28

    es_sarcopenia = (
        datos.circunferencia_pantorrilla < 31
    )

    pruebas_bajas = (
        contar_pruebas_bajo_percentil_25(
            datos.resultados_pruebas
        )
    )

    if (
        es_obesidad
        and es_sarcopenia
        and pruebas_bajas >= 2
    ):

        return (
            "Crítico"
            if imc > 30
            else "Alto"
        )

    return "Bajo"


# ==========================================================
# RUTA PRINCIPAL
# ==========================================================

@app.get("/")
async def root():

    return {
        "message": (
            "API Senior Fitness Test "
            "operando correctamente en Vercel"
        ),
        "version": "2.0.0",
        "supabase_configurado": bool(
            SUPABASE_URL
            and SUPABASE_SERVICE_ROLE_KEY
        ),
    }


# ==========================================================
# COMPROBAR CONEXIÓN SUPABASE
# ==========================================================

@app.get("/supabase-health")
async def supabase_health():

    try:

        supabase = get_supabase()

        respuesta = (
            supabase
            .table("campos")
            .select("id")
            .limit(1)
            .execute()
        )

        return {
            "status": "ok",
            "message": (
                "Vercel conectado correctamente "
                "con Supabase"
            ),
            "tabla_campos_accesible": True,
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=(
                "No fue posible conectar "
                f"con Supabase: {str(e)}"
            ),
        )


# ==========================================================
# PROCESAR TEST
# ==========================================================

@app.post("/procesar-test")
async def procesar_test(
    datos: RegistroSFT,
):

    try:

        riesgo = evaluar_obesidad_sarcopenica(
            datos
        )

        imc_calculado = round(
            datos.peso
            / (datos.talla ** 2),
            2,
        )

        return {
            "status": "Evaluación completada",
            "usuario_id": datos.usuario_id,
            "imc": imc_calculado,
            "alerta_riesgo": riesgo,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ==========================================================
# CREAR EVALUACIÓN
# ==========================================================

@app.post("/evaluaciones")
async def crear_evaluacion(
    evaluacion: EvaluacionCrear,
):

    try:

        supabase = get_supabase()

        datos = modelo_a_dict(evaluacion)

        datos["fecha_nacimiento"] = (
            evaluacion.fecha_nacimiento.isoformat()
        )

        if evaluacion.edad is None:

            datos["edad"] = calcular_edad(
                evaluacion.fecha_nacimiento
            )

        respuesta = (
            supabase
            .table("evaluaciones")
            .insert(datos)
            .execute()
        )

        if not respuesta.data:

            raise HTTPException(
                status_code=500,
                detail=(
                    "Supabase no devolvió "
                    "la evaluación creada"
                ),
            )

        registro = respuesta.data[0]

        registrar_auditoria(
            accion="crear_evaluacion",
            evaluation_id=registro.get("id"),
            detalles={
                "documento": evaluacion.documento,
            },
        )

        return {
            "status": "ok",
            "message": (
                "Evaluación guardada "
                "correctamente en la nube"
            ),
            "evaluacion": registro,
        }

    except HTTPException:
        raise

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ==========================================================
# LISTAR EVALUACIONES
# ==========================================================

@app.get("/evaluaciones")
async def listar_evaluaciones(
    documento: Optional[str] = None,
    limite: int = Query(
        default=100,
        ge=1,
        le=500,
    ),
):

    try:

        supabase = get_supabase()

        consulta = (
            supabase
            .table("evaluaciones")
            .select("*")
        )

        if documento:

            consulta = consulta.eq(
                "documento",
                documento,
            )

        respuesta = (
            consulta
            .order(
                "created_at",
                desc=True,
            )
            .limit(limite)
            .execute()
        )

        return {
            "total": len(
                respuesta.data or []
            ),
            "evaluaciones": (
                respuesta.data or []
            ),
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ==========================================================
# CONSULTAR UNA EVALUACIÓN
# ==========================================================

@app.get("/evaluaciones/{evaluacion_id}")
async def obtener_evaluacion(
    evaluacion_id: str,
):

    try:

        supabase = get_supabase()

        respuesta = (
            supabase
            .table("evaluaciones")
            .select("*")
            .eq(
                "id",
                evaluacion_id,
            )
            .limit(1)
            .execute()
        )

        if not respuesta.data:

            raise HTTPException(
                status_code=404,
                detail=(
                    "Evaluación no encontrada"
                ),
            )

        return respuesta.data[0]

    except HTTPException:
        raise

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ==========================================================
# ACTUALIZAR EVALUACIÓN
# ==========================================================

@app.put("/evaluaciones/{evaluacion_id}")
async def actualizar_evaluacion(
    evaluacion_id: str,
    cambios: EvaluacionActualizar,
):

    try:

        supabase = get_supabase()

        datos = modelo_a_dict(
            cambios,
            exclude_none=True,
        )

        if (
            cambios.fecha_nacimiento
            is not None
        ):

            datos[
                "fecha_nacimiento"
            ] = (
                cambios
                .fecha_nacimiento
                .isoformat()
            )

            if cambios.edad is None:

                datos["edad"] = (
                    calcular_edad(
                        cambios
                        .fecha_nacimiento
                    )
                )

        if not datos:

            raise HTTPException(
                status_code=400,
                detail=(
                    "No se enviaron campos "
                    "para actualizar"
                ),
            )

        respuesta = (
            supabase
            .table("evaluaciones")
            .update(datos)
            .eq(
                "id",
                evaluacion_id,
            )
            .execute()
        )

        if not respuesta.data:

            raise HTTPException(
                status_code=404,
                detail=(
                    "Evaluación no encontrada"
                ),
            )

        registrar_auditoria(
            accion="actualizar_evaluacion",
            evaluation_id=evaluacion_id,
            detalles={
                "campos_modificados": list(
                    datos.keys()
                )
            },
        )

        return {
            "status": "ok",
            "message": (
                "Evaluación actualizada "
                "correctamente"
            ),
            "evaluacion": (
                respuesta.data[0]
            ),
        }

    except HTTPException:
        raise

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ==========================================================
# CONSULTAR CAMPOS
# ==========================================================

@app.get("/campos")
async def listar_campos(
    solo_activos: bool = True,
):

    try:

        supabase = get_supabase()

        consulta = (
            supabase
            .table("campos")
            .select("*")
        )

        if solo_activos:

            consulta = consulta.eq(
                "activo",
                True,
            )

        respuesta = (
            consulta
            .order("grupo")
            .order("orden")
            .execute()
        )

        return {
            "total": len(
                respuesta.data or []
            ),
            "campos": (
                respuesta.data or []
            ),
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ==========================================================
# CREAR CAMPO
# ==========================================================

@app.post("/campos")
async def crear_campo(
    campo: CampoCrear,
):

    try:

        supabase = get_supabase()

        datos = modelo_a_dict(campo)

        respuesta = (
            supabase
            .table("campos")
            .insert(datos)
            .execute()
        )

        if not respuesta.data:

            raise HTTPException(
                status_code=500,
                detail=(
                    "No fue posible "
                    "crear el campo"
                ),
            )

        return {
            "status": "ok",
            "campo": respuesta.data[0],
        }

    except HTTPException:
        raise

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ==========================================================
# ACTUALIZAR CAMPO
# ==========================================================

@app.put("/campos/{campo_id}")
async def actualizar_campo(
    campo_id: int,
    cambios: CampoActualizar,
):

    try:

        supabase = get_supabase()

        datos = modelo_a_dict(
            cambios,
            exclude_none=True,
        )

        if not datos:

            raise HTTPException(
                status_code=400,
                detail=(
                    "No se enviaron cambios"
                ),
            )

        respuesta = (
            supabase
            .table("campos")
            .update(datos)
            .eq(
                "id",
                campo_id,
            )
            .execute()
        )

        if not respuesta.data:

            raise HTTPException(
                status_code=404,
                detail=(
                    "Campo no encontrado"
                ),
            )

        return {
            "status": "ok",
            "campo": respuesta.data[0],
        }

    except HTTPException:
        raise

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )
