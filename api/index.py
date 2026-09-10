from pathlib import Path
from fastapi.responses import HTMLResponse, Response
from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import Client, create_client
from fpdf import FPDF

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
# MOTOR DE INTERPRETACIÓN SFT
# ==========================================================

def buscar_valor(datos: Dict[str, Any], claves: List[str]):
    """
    Busca un resultado usando varios nombres posibles.
    Devuelve (clave_encontrada, valor).
    """
    for clave in claves:
        if clave in datos and datos[clave] not in (None, ""):
            try:
                return clave, float(datos[clave])
            except (TypeError, ValueError):
                return clave, None

    return None, None


def obtener_baremo(
    supabase: Client,
    clave_prueba: str,
    sexo: str,
    edad: int,
):
    respuesta = (
        supabase
        .table("baremos_sft")
        .select("*")
        .eq("clave_prueba", clave_prueba)
        .eq("sexo", sexo)
        .execute()
    )

    for fila in respuesta.data or []:
        if fila["edad_min"] <= edad <= fila["edad_max"]:
            return fila

    return None


def obtener_criterio_mantenimiento(
    supabase: Client,
    clave_prueba: str,
    sexo: str,
    edad: int,
):
    respuesta = (
        supabase
        .table("criterios_mantenimiento")
        .select("*")
        .eq("clave_prueba", clave_prueba)
        .eq("sexo", sexo)
        .execute()
    )

    for fila in respuesta.data or []:
        if fila["edad_min"] <= edad <= fila["edad_max"]:
            return fila

    return None


def clasificar_por_baremo(
    valor: float,
    baremo: Dict[str, Any],
) -> str:

    p25 = float(baremo["p25"])
    p75 = float(baremo["p75"])
    mayor_es_mejor = bool(baremo["mayor_es_mejor"])

    if mayor_es_mejor:

        if valor < p25:
            return "Por debajo del rango normal"

        if valor > p75:
            return "Por encima del rango normal"

        return "Dentro del rango normal"

    # Pruebas donde MENOR resultado es mejor,
    # como 8-Foot Up-and-Go.
    if valor > p25:
        return "Por debajo del rango normal"

    if valor < p75:
        return "Por encima del rango normal"

    return "Dentro del rango normal"


def evaluar_criterio_mantenimiento(
    valor: float,
    criterio: Optional[Dict[str, Any]],
):

    if not criterio:
        return None

    objetivo = float(criterio["valor_objetivo"])
    operador = criterio["cumple_si"]

    if operador == ">=":
        cumple = valor >= objetivo
    else:
        cumple = valor <= objetivo

    return {
        "cumple": cumple,
        "valor_objetivo": objetivo,
        "operador": operador,
        "unidad": criterio["unidad"],
    }


def interpretar_evaluacion(
    supabase: Client,
    edad: int,
    sexo: str,
    datos: Dict[str, Any],
) -> Dict[str, Any]:

    resultado = {
        "edad": edad,
        "sexo": sexo,
        "grupo_normativo": None,
        "imc": None,
        "pruebas": {},
       "resumen": {
    "por_debajo": 0,
    "dentro_rango": 0,
    "por_encima": 0,
    "datos_a_verificar": 0,
    "total_interpretadas": 0,
},
        "advertencias": [],
    }

    # ======================================================
    # EDAD
    # ======================================================

    if edad < 60 or edad > 94:
        resultado["advertencias"].append(
            "No existen baremos SFT configurados para esta edad."
        )
        return resultado

    inicio = 60 + ((edad - 60) // 5) * 5
    fin = min(inicio + 4, 94)

    resultado["grupo_normativo"] = f"{inicio}-{fin}"

    # ======================================================
    # IMC
    # ======================================================

    _, peso = buscar_valor(
        datos,
        ["peso_kg", "peso"]
    )

    _, talla = buscar_valor(
        datos,
        ["talla_m", "talla"]
    )

    if peso is not None and talla is not None and talla > 0:
        resultado["imc"] = round(
            peso / (talla ** 2),
            2
        )

    # ======================================================
    # CONFIGURACIÓN DE PRUEBAS
    # ======================================================

    pruebas = {

        "chair_stand": {
            "nombre": "Sentarse y levantarse de una silla",
            "claves": [
                "chair_stand",
                "sentarse_levantarse"
            ],
            "conversion": "ninguna",
        },

        "arm_curl": {
            "nombre": "Flexiones del brazo",
            "claves": [
                "arm_curl",
                "flexiones_brazo"
            ],
            "conversion": "ninguna",
        },

        "six_min_walk": {
            "nombre": "Caminata de 6 minutos",
            "claves": [
                "six_min_walk",
                "six_min_walk_m",
                "caminata_6_min"
            ],
            # La aplicación recibe metros;
            # los baremos están en yardas.
            "conversion": "metros_a_yardas",
        },

        "two_min_step": {
            "nombre": "Marcha de 2 minutos",
            "claves": [
                "two_min_step",
                "marcha_2_min"
            ],
            "conversion": "ninguna",
        },

        "chair_sit_reach": {
            "nombre": "Sentado y alcanzar el pie",
            "claves": [
                "chair_sit_reach",
                "sit_reach"
            ],
            # El protocolo registra cm;
            # los baremos originales están en pulgadas.
            "conversion": "cm_a_pulgadas",
        },

        "back_scratch": {
            "nombre": "Alcanzar manos tras la espalda",
            "claves": [
                "back_scratch",
                "manos_espalda"
            ],
            "conversion": "cm_a_pulgadas",
        },

        "eight_foot_up_go": {
            "nombre": "8-Foot Up-and-Go",
            "claves": [
                "eight_foot_up_go",
                "up_and_go"
            ],
            "conversion": "ninguna",
        },
    }

    # ======================================================
    # INTERPRETAR CADA PRUEBA
    # ======================================================

    for clave_prueba, config in pruebas.items():

        clave_encontrada, valor_original = buscar_valor(
            datos,
            config["claves"]
        )

        if valor_original is None:
            continue

        valor_baremo = valor_original
        unidad_ingresada = None

        if config["conversion"] == "cm_a_pulgadas":

            valor_baremo = valor_original / 2.54
            unidad_ingresada = "cm"

        elif config["conversion"] == "metros_a_yardas":

            valor_baremo = valor_original * 1.0936133
            unidad_ingresada = "metros"
        # ======================================================
        # CONTROL DE PLAUSIBILIDAD
        # No es un punto de corte clínico.
        # Evita interpretar posibles errores de digitación.
        # ======================================================

        dato_a_verificar = False
        motivo_verificacion = None

        if (
            clave_prueba == "two_min_step"
            and valor_original > 200
        ):
            dato_a_verificar = True

            motivo_verificacion = (
                "El resultado de Marcha de 2 minutos "
                "es inusualmente alto. Verifique que "
                "corresponda realmente al número de pasos."
            )

            resultado["advertencias"].append(
                motivo_verificacion
            )

        baremo = obtener_baremo(
            supabase,
            clave_prueba,
            sexo,
            edad,
        )

        if not baremo:

            resultado["advertencias"].append(
                f"No se encontró baremo para {config['nombre']}."
            )
            continue

        if dato_a_verificar:

            clasificacion = "Dato a verificar"

        else:

            clasificacion = clasificar_por_baremo(
                valor_baremo,
                baremo,
            )

        if dato_a_verificar:

            criterio = None
            mantenimiento = None

        else:

            criterio = obtener_criterio_mantenimiento(
                supabase,
                clave_prueba,
                sexo,
                edad,
            )

            mantenimiento = evaluar_criterio_mantenimiento(
                valor_baremo,
                criterio,
            )

        registro = {
            "nombre": config["nombre"],
            "clave_datos": clave_encontrada,
            "resultado_original": valor_original,
            "unidad_ingresada": unidad_ingresada or baremo["unidad"],
            "resultado_para_baremo": round(valor_baremo, 2),
            "unidad_baremo": baremo["unidad"],
            "p25": float(baremo["p25"]),
            "p75": float(baremo["p75"]),
            "mayor_es_mejor": bool(
                baremo["mayor_es_mejor"]
            ),
            "clasificacion": clasificacion,
            "criterio_mantenimiento": mantenimiento,
            "valido_para_interpretacion": not dato_a_verificar,
            "motivo_verificacion": motivo_verificacion,
        }

        resultado["pruebas"][clave_prueba] = registro

        if dato_a_verificar:

            resultado["resumen"]["datos_a_verificar"] += 1

        else:

            resultado["resumen"]["total_interpretadas"] += 1

            if clasificacion == "Por debajo del rango normal":

                resultado["resumen"]["por_debajo"] += 1

            elif clasificacion == "Dentro del rango normal":

                resultado["resumen"]["dentro_rango"] += 1

            else:

                resultado["resumen"]["por_encima"] += 1
    # ======================================================
    # PRIORIDAD FUNCIONAL
    # ======================================================

    bajas = resultado["resumen"]["por_debajo"]

    if bajas >= 3:
        prioridad = "Alta"
    elif bajas >= 1:
        prioridad = "Seguimiento"
    else:
        prioridad = "Sin alerta funcional por baremos"

    resultado["prioridad_funcional"] = prioridad

    return resultado
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

@app.get("/", response_class=HTMLResponse)
async def root():
    archivo = Path(__file__).resolve().parent / "web.html"

    if not archivo.exists():
        raise HTTPException(
            status_code=500,
            detail="No se encontró la interfaz web.html"
        )

    return archivo.read_text(encoding="utf-8")

      
# ==========================================================
# INFORME INDIVIDUAL PDF
# ==========================================================

def texto_pdf(valor):
    if valor is None:
        return ""

    return (
        str(valor)
        .replace("≥", ">=")
        .replace("≤", "<=")
        .replace("✓", "Cumple")
        .encode("latin-1", errors="replace")
        .decode("latin-1")
    )


def valor_visible(valor):
    if valor is None:
        return ""

    if isinstance(valor, bool):
        return "Sí" if valor else "No"

    if isinstance(valor, list):
        return ", ".join(str(x) for x in valor)

    if isinstance(valor, dict):
        return ", ".join(
            f"{k}: {v}"
            for k, v in valor.items()
        )

    return str(valor)


def rango_misma_unidad(prueba, valor):
    if valor is None:
        return None

    valor = float(valor)

    unidad_baremo = str(
        prueba.get("unidad_baremo", "")
    ).lower()

    unidad_ingreso = str(
        prueba.get("unidad_ingresada", "")
    ).lower()

    if (
        unidad_baremo == "yardas"
        and unidad_ingreso == "metros"
    ):
        return valor / 1.0936133

    if (
        unidad_baremo == "pulgadas"
        and unidad_ingreso == "cm"
    ):
        return valor * 2.54

    return valor


def numero_pdf(valor):
    if valor is None:
        return ""

    try:
        numero = float(valor)

        if numero.is_integer():
            return str(int(numero))

        return f"{numero:.2f}".replace(".", ",")

    except Exception:
        return str(valor)


class InformeSFT(FPDF):

    def header(self):
        self.set_font(
            "Helvetica",
            "B",
            15
        )

        self.cell(
            0,
            8,
            texto_pdf(
                "Senior Fitness Test INDER"
            ),
            ln=1
        )

        self.set_font(
            "Helvetica",
            "",
            9
        )

        self.cell(
            0,
            6,
            texto_pdf(
                "Informe individual de valoración funcional"
            ),
            ln=1
        )

        self.ln(3)

        self.line(
            self.l_margin,
            self.get_y(),
            self.w - self.r_margin,
            self.get_y()
        )

        self.ln(5)


    def footer(self):
        self.set_y(-15)

        self.set_font(
            "Helvetica",
            "",
            8
        )

        self.cell(
            0,
            8,
            texto_pdf(
                f"Página {self.page_no()}"
            ),
            align="C"
        )


def titulo_seccion(pdf, titulo):
    pdf.ln(3)

    pdf.set_font(
        "Helvetica",
        "B",
        11
    )

    pdf.cell(
        0,
        7,
        texto_pdf(titulo),
        ln=1
    )

    pdf.line(
        pdf.l_margin,
        pdf.get_y(),
        pdf.w - pdf.r_margin,
        pdf.get_y()
    )

    pdf.ln(2)


def linea_dato(pdf, etiqueta, valor):
    if valor in (
        None,
        "",
        [],
        {}
    ):
        return

    pdf.set_font(
        "Helvetica",
        "B",
        9
    )

    pdf.cell(
        52,
        6,
        texto_pdf(etiqueta + ":")
    )

    pdf.set_font(
        "Helvetica",
        "",
        9
    )

    pdf.multi_cell(
        0,
        6,
        texto_pdf(
            valor_visible(valor)
        )
    )


def construir_pdf_evaluacion(
    evaluacion,
    campos
):
    pdf = InformeSFT()

    pdf.set_auto_page_break(
        auto=True,
        margin=18
    )

    pdf.add_page()

    datos = (
        evaluacion.get("datos")
        or {}
    )

    interpretacion = (
        evaluacion.get("interpretacion")
        or {}
    )

    # IDENTIFICACIÓN

    titulo_seccion(
        pdf,
        "1. Identificación"
    )

    linea_dato(
        pdf,
        "Documento",
        evaluacion.get("documento")
    )

    linea_dato(
        pdf,
        "Nombres y apellidos",
        evaluacion.get("nombres")
    )

    linea_dato(
        pdf,
        "Fecha de nacimiento",
        evaluacion.get(
            "fecha_nacimiento"
        )
    )

    linea_dato(
        pdf,
        "Edad",
        evaluacion.get("edad")
    )

    linea_dato(
        pdf,
        "Sexo",
        evaluacion.get("sexo")
    )

    linea_dato(
        pdf,
        "Fecha de valoración",
        evaluacion.get("created_at")
    )

    # CAMPOS REGISTRADOS

    grupos = {}

    for campo in campos:

        clave = campo.get("clave")

        if not clave:
            continue

        valor = datos.get(clave)

        if valor in (
            None,
            "",
            [],
            {}
        ):
            continue

        grupo = (
            campo.get("grupo")
            or "Otros"
        )

        grupos.setdefault(
            grupo,
            []
        ).append(
            (
                campo,
                valor
            )
        )

    numero_seccion = 2

    for grupo, elementos in grupos.items():

        titulo_seccion(
            pdf,
            f"{numero_seccion}. {grupo}"
        )

        numero_seccion += 1

        for campo, valor in elementos:

            etiqueta = (
                campo.get("etiqueta")
                or campo.get("clave")
            )

            unidad = campo.get("unidad")

            if unidad:
                etiqueta += f" ({unidad})"

            linea_dato(
                pdf,
                etiqueta,
                valor
            )

    # INTERPRETACIÓN SFT

    pruebas = (
        interpretacion.get("pruebas")
        or {}
    )

    if pruebas:

        titulo_seccion(
            pdf,
            f"{numero_seccion}. Interpretación SFT"
        )

        numero_seccion += 1

        linea_dato(
            pdf,
            "Grupo normativo",
            interpretacion.get(
                "grupo_normativo"
            )
        )

        linea_dato(
            pdf,
            "IMC",
            interpretacion.get("imc")
        )

        pdf.ln(2)

        for prueba in pruebas.values():

            pdf.set_font(
                "Helvetica",
                "B",
                10
            )

            pdf.multi_cell(
                0,
                6,
                texto_pdf(
                    prueba.get(
                        "nombre",
                        "Prueba"
                    )
                )
            )

            resultado_original = (
                prueba.get(
                    "resultado_original"
                )
            )

            unidad_ingresada = (
                prueba.get(
                    "unidad_ingresada",
                    ""
                )
            )

            linea_dato(
                pdf,
                "Resultado",
                (
                    f"{numero_pdf(resultado_original)} "
                    f"{unidad_ingresada}"
                )
            )

            p25 = rango_misma_unidad(
                prueba,
                prueba.get("p25")
            )

            p75 = rango_misma_unidad(
                prueba,
                prueba.get("p75")
            )

            if (
                p25 is not None
                and p75 is not None
            ):

                minimo = min(
                    p25,
                    p75
                )

                maximo = max(
                    p25,
                    p75
                )

                linea_dato(
                    pdf,
                    "Rango normal",
                    (
                        f"{numero_pdf(minimo)} - "
                        f"{numero_pdf(maximo)} "
                        f"{unidad_ingresada}"
                    )
                )

            linea_dato(
                pdf,
                "Clasificación",
                prueba.get(
                    "clasificacion"
                )
            )

            if (
                prueba.get(
                    "valido_para_interpretacion"
                )
                is False
            ):
                linea_dato(
                    pdf,
                    "Advertencia",
                    prueba.get(
                        "motivo_verificacion"
                    )
                )

            criterio = (
                prueba.get(
                    "criterio_mantenimiento"
                )
            )

            if criterio:

                objetivo = rango_misma_unidad(
                    prueba,
                    criterio.get(
                        "valor_objetivo"
                    )
                )

                cumple = (
                    "Sí"
                    if criterio.get("cumple")
                    else "No"
                )

                linea_dato(
                    pdf,
                    "Criterio de mantenimiento",
                    (
                        f"{cumple} "
                        f"({criterio.get('operador')} "
                        f"{numero_pdf(objetivo)} "
                        f"{unidad_ingresada})"
                    )
                )

            pdf.ln(3)

    # RESUMEN FUNCIONAL

    resumen = (
        interpretacion.get("resumen")
        or {}
    )

    if resumen:

        titulo_seccion(
            pdf,
            f"{numero_seccion}. Resumen funcional"
        )

        linea_dato(
            pdf,
            "Por debajo del rango",
            resumen.get(
                "por_debajo",
                0
            )
        )

        linea_dato(
            pdf,
            "Dentro del rango",
            resumen.get(
                "dentro_rango",
                0
            )
        )

        linea_dato(
            pdf,
            "Por encima del rango",
            resumen.get(
                "por_encima",
                0
            )
        )

        linea_dato(
            pdf,
            "Datos a verificar",
            resumen.get(
                "datos_a_verificar",
                0
            )
        )

        linea_dato(
            pdf,
            "Pruebas interpretadas",
            resumen.get(
                "total_interpretadas",
                0
            )
        )

        linea_dato(
            pdf,
            "Prioridad funcional",
            interpretacion.get(
                "prioridad_funcional"
            )
        )

    pdf.ln(5)

    pdf.set_font(
        "Helvetica",
        "I",
        8
    )

    pdf.multi_cell(
        0,
        5,
        texto_pdf(
            "Este informe presenta los resultados registrados "
            "durante la valoración y su comparación con los "
            "baremos configurados en la aplicación. "
            "La interpretación no reemplaza una valoración médica."
        )
    )

    return bytes(
        pdf.output()
    )
    
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

@app.post("/interpretar")
async def interpretar_sin_guardar(
    evaluacion: EvaluacionCrear,
):

    supabase = get_supabase()

    edad = (
        evaluacion.edad
        if evaluacion.edad is not None
        else calcular_edad(
            evaluacion.fecha_nacimiento
        )
    )

    interpretacion = interpretar_evaluacion(
        supabase=supabase,
        edad=edad,
        sexo=evaluacion.sexo,
        datos=evaluacion.datos,
    )

    return {
        "status": "ok",
        "interpretacion": interpretacion,
    }
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
            
        edad_final = datos.get("edad")

        if edad_final is None:
            edad_final = calcular_edad(
                evaluacion.fecha_nacimiento
            )

        interpretacion_automatica = interpretar_evaluacion(
            supabase=supabase,
            edad=edad_final,
            sexo=evaluacion.sexo,
            datos=evaluacion.datos,
        )

        datos["interpretacion"] = interpretacion_automatica
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


@app.get("/evaluaciones/{evaluacion_id}/reporte")
async def descargar_reporte_evaluacion(
    evaluacion_id: str
):

    supabase = get_supabase()

    respuesta = (
        supabase
        .table("evaluaciones")
        .select("*")
        .eq("id", evaluacion_id)
        .limit(1)
        .execute()
    )

    if not respuesta.data:
        raise HTTPException(
            status_code=404,
            detail="No se encontró la valoración."
        )

    evaluacion = respuesta.data[0]

    respuesta_campos = (
        supabase
        .table("campos")
        .select("*")
        .eq("activo", True)
        .order("grupo")
        .order("orden")
        .execute()
    )

    campos = respuesta_campos.data or []

    contenido = construir_pdf_evaluacion(
        evaluacion,
        campos
    )

    documento = (
        evaluacion.get("documento")
        or "sin_documento"
    )

    nombre_archivo = (
        f"SFT_{documento}.pdf"
    )

    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                (
                    "attachment; "
                    f'filename="{nombre_archivo}"'
                )
        }
    )


# ==========================================================
# ACTUALIZAR EVALUACIÓN
# ==========================================================

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

