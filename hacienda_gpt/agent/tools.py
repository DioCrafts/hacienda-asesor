"""Tools the agent can call, and their execution against local state.

Tools are deliberately few and typed. Everything that must be *exact* — what
the law says, what the user declared, which rules fire — lives behind a tool,
so the model orchestrates and reasons but never has to recall a figure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
import logging
from typing import Any, get_args

from pydantic import ValidationError

from hacienda_gpt.agent.evidence import SOURCE_TYPES, EvidenceLedger, Fragment
from hacienda_gpt.agent.providers.base import ToolCall, ToolSpec
from hacienda_gpt.agent.search import CorpusSearcher, SearchFilters, normalize_date
from hacienda_gpt.decision.fact_extractor import EXPECTED_FACT_TYPE, KNOWN_FACT_NAMES, _coerce_value
from hacienda_gpt.decision.interpreter import _merge_facts
from hacienda_gpt.decision.rules_engine import evaluate_rules
from hacienda_gpt.decision.schemas import CaseState, Fact, parse_fiscal_year
from hacienda_gpt.doctrine.citas import extract_citations, identify_document
from hacienda_gpt.doctrine.grafo import DoctrineGraph
from hacienda_gpt.proactive.jobs import hoy_en_espana
from hacienda_gpt.tax.actividad import ActividadInput, calcular_rendimiento_actividad
from hacienda_gpt.tax.autonomos_params import ejercicios_autonomos_disponibles
from hacienda_gpt.tax.beneficios import evaluar_beneficios
from hacienda_gpt.tax.calendario import aviso_festivos, proximos_vencimientos
from hacienda_gpt.tax.conjunta import comparar_tributacion
from hacienda_gpt.tax.irpf import IRPFInput, IRPFResult
from hacienda_gpt.tax.iva import Modelo303Input, calcular_modelo_303
from hacienda_gpt.tax.optimizador import PlanOptimo, optimizar_desde_hechos
from hacienda_gpt.tax.pagos_fraccionados import Modelo130Input, calcular_modelo_130
from hacienda_gpt.tax.params import EjercicioNoDisponibleError, EtapaEducativa, ejercicios_disponibles, load_params
from hacienda_gpt.tax.patrimonio import PatrimonioInput, calcular_patrimonio
from hacienda_gpt.tax.patrimonio_params import ejercicios_patrimonio_disponibles
from hacienda_gpt.tax.profile import (
    DESCENDIENTES,
    RECUENTOS_DEL_DETALLE,
    DatosInvalidosError,
    build_input_con_actividad,
    descendientes_a_texto,
)
from hacienda_gpt.tax.simulador import simular
from hacienda_gpt.tax.sociedades import (
    ComparacionInput,
    PagoFraccionadoSociedadesInput,
    SociedadesInput,
    calcular_pago_fraccionado_sociedades,
    calcular_sociedades,
    comparar_autonomo_vs_sociedad,
)
from hacienda_gpt.tax.sociedades_params import ejercicios_sociedades_disponibles
from hacienda_gpt.tax.sucesiones import BeneficiarioISD, calcular_isd
from hacienda_gpt.tax.sucesiones_params import ejercicios_isd_disponibles

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 10
_ETAPAS_EDUCATIVAS = list(get_args(EtapaEducativa))

SEARCH_TOOL = ToolSpec(
    name="buscar_fuentes",
    description=(
        "Busca en el corpus fiscal español (legislación consolidada del BOE, consultas vinculantes de la DGT, "
        "resoluciones del TEAC, jurisprudencia y contenido de la AEAT) y devuelve fragmentos literales, cada uno con "
        "un identificador citable [F#], su autoridad, fecha y si es vinculante. Úsala antes de afirmar cualquier "
        "norma, cifra, plazo o criterio. La búsqueda es semántica: escribe la consulta como una frase descriptiva "
        "con los términos técnicos de la norma (p. ej. 'exención por reinversión en vivienda habitual artículo 38 "
        "LIRPF requisitos y plazo'), no como palabras sueltas. Usa `tipo_fuente` para pedir expresamente la ley, la "
        "jurisprudencia o la doctrina administrativa sobre un punto ya localizado."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "consulta": {"type": "string", "description": "Qué buscar, en español, con terminología técnica."},
            "tipo_fuente": {
                "type": "string",
                "enum": list(SOURCE_TYPES),
                "description": (
                    "Restringe a un tipo: 'normativa' (leyes y reglamentos, BOE y boletines autonómicos), "
                    "'jurisprudencia' (TS, TC), 'doctrina_administrativa' (DGT, TEAC) o 'aeat' (manuales y páginas "
                    "informativas). Omítelo para buscar en todo."
                ),
            },
            "tributo": {
                "type": "string",
                "description": "Tributo al que acotar si procede: IRPF, IVA, ISD, ITP_AJD, IS, IP, IIVTNU…",
            },
            "ambito": {
                "type": "string",
                "description": (
                    "'estatal', o 'autonomico:<comunidad>' para normativa propia de una comunidad autónoma "
                    "(la estatal se incluye siempre)."
                ),
            },
            "solo_vinculante": {
                "type": "boolean",
                "description": "true para devolver solo doctrina marcada como vinculante.",
            },
            "desde_fecha": {
                "type": "string",
                "description": (
                    "AAAA o AAAA-MM-DD. Descarta consultas, resoluciones y sentencias anteriores a esa fecha; úsalo "
                    "cuando una reforma o una sentencia posterior haya podido dejar superada la doctrina antigua. "
                    "La legislación consolidada (sin fecha de resolución) se mantiene siempre."
                ),
            },
            "max_resultados": {"type": "integer", "description": f"Entre 1 y {MAX_SEARCH_RESULTS}. Por defecto 6."},
        },
        "required": ["consulta"],
    },
)

READ_CASE_TOOL = ToolSpec(
    name="consultar_expediente",
    description=(
        "Devuelve el expediente fiscal del usuario: ejercicio, hechos ya conocidos (residencia, tipo de renta, "
        "importes, actividad…), datos que faltan y obligaciones candidatas detectadas. Consúltalo antes de "
        "preguntar al usuario algo que quizá ya haya dicho."
    ),
    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
)

RECORD_FACTS_TOOL = ToolSpec(
    name="registrar_hechos",
    description=(
        "Guarda en el expediente hechos que el usuario ha declarado expresamente en esta conversación (o que ha "
        "corregido). No registres inferencias ni supuestos tuyos. Valores esperados: residencia_fiscal 'ES' o "
        "código ISO del país; periodo_fiscal año de 4 dígitos; importes en euros como número; booleanos true/false. "
        "`descendientes` (detalle de cada hijo) se guarda como texto JSON: una lista con un objeto por hijo, con "
        "`edad` (a 31 de diciembre) o `fecha_nacimiento` (AAAA-MM-DD) y, si constan, grado_discapacidad, "
        f"etapa_educativa ({' | '.join(_ETAPAS_EDUCATIVAS)}), ejercicio_adopcion, adopcion_internacional, "
        "parto_multiple, meses_guarderia y gastos {campo: euros}; por ejemplo "
        '[{"fecha_nacimiento": "2024-03-10", "etapa_educativa": "infantil_primer_ciclo", "gastos": '
        '{"gastos_guarderia": 1800}}]. Envía siempre la lista completa (sustituye a la anterior); con ella no '
        "registres num_descendientes ni los demás recuentos de hijos."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "hechos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "nombre": {"type": "string", "enum": list(KNOWN_FACT_NAMES)},
                        "valor": {"type": ["string", "number", "boolean"]},
                    },
                    "required": ["nombre", "valor"],
                },
            }
        },
        "required": ["hechos"],
    },
)

EVALUATE_RULES_TOOL = ToolSpec(
    name="evaluar_obligaciones",
    description=(
        "Ejecuta el catálogo determinista de reglas sobre el expediente y devuelve las obligaciones tributarias "
        "candidatas, los hechos que faltan para confirmarlas y las fuentes normativas de cada regla. El catálogo es "
        "parcial: que una obligación no aparezca no significa que no exista."
    ),
    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
)


_INPUT_FIELDS_DOC = (
    "comunidad_autonoma, tributacion ('individual' | 'conjunta_biparental' | 'conjunta_monoparental'), edad, "
    "grado_discapacidad (65 si la incapacidad está declarada judicialmente), necesita_asistencia (bool: con "
    "discapacidad, necesita ayuda de terceras personas o tiene movilidad reducida), num_descendientes, "
    "num_descendientes_menores_3, descendientes_compartidos (bool), num_ascendientes_mayores_65, "
    "num_ascendientes_mayores_75. Mínimo por discapacidad de descendientes y ascendientes: "
    "num_descendientes_discapacidad (hijos con discapacidad del 33 % o más, a cualquier edad), "
    "num_descendientes_discapacidad_65 (de ellos, del 65 % o más), num_descendientes_asistencia (del 33 % al 64 % que "
    "necesitan ayuda de terceras personas o tienen movilidad reducida); lo mismo para los ascendientes que conviven "
    "con el usuario: num_ascendientes_discapacidad, num_ascendientes_discapacidad_65, num_ascendientes_asistencia y "
    "num_ascendientes_discapacidad_hasta_65 (los de 65 años o menos, que no van en num_ascendientes_mayores_65 y "
    "también dan el mínimo por ascendientes); num_declaraciones_mismos_ascendientes (declaraciones, la del usuario "
    "incluida, que aplican el mínimo por los mismos ascendientes: se reparte entre ellas). "
    "rendimiento_trabajo_bruto, "
    "cotizaciones_seguridad_social, otros_gastos_deducibles_trabajo, rendimiento_actividad_neto, "
    "rendimiento_alquiler_neto (ya reducido), rendimiento_alquiler_vivienda_sin_reducir (la calculadora aplica la "
    "reducción del art. 23.2 con arrendamiento_fecha_contrato AAAA-MM-DD y, si constan, arrendamiento_gran_tenedor, "
    "arrendamiento_primer_alquiler, arrendamiento_variacion_renta en %, arrendamiento_mismo_inquilino, "
    "arrendamiento_renta_bajo_indice, arrendamiento_zona_tensionada, arrendamiento_inquilino_joven, "
    "arrendamiento_alquiler_social, arrendamiento_vivienda_rehabilitada, arrendamiento_prorroga_tacita), "
    "otras_rentas_base_general, rendimiento_capital_mobiliario, ganancias_patrimoniales, "
    "rendimientos_negativos_pendientes y perdidas_patrimoniales_pendientes (saldos negativos del ahorro de los "
    "cuatro años anteriores, en positivo), "
    "aportacion_plan_pensiones, contribucion_empresa_plan_pensiones, donativos, donativos_recurrentes (bool), "
    "pagos_vivienda_habitual_pre2013, hijos_deduccion_maternidad, gastos_guarderia, retenciones. Para residentes en "
    "Madrid, además: alquiler_vivienda_habitual, gastos_escolaridad, gastos_ensenanza_idiomas, "
    "gastos_uniformes_escolares, num_hijos_gastos_educativos, num_hijos_primer_ciclo_infantil, "
    "cotizaciones_empleada_hogar, hijos_nacidos_ultimos_3_ejercicios, donativos_fundaciones_madrid, familia_numerosa. Para las deducciones del resto de comunidades: "
    "alquiler_vivienda_habitual, gastos_guarderia, gastos_libros_texto, hijos_nacidos_en_el_ejercicio, "
    "familia_numerosa_especial (bool), familia_monoparental (bool), edad, grado_discapacidad, menores_acogidos, "
    "hijos_adoptados_en_el_ejercicio, gastos_cuidado_familiares, "
    "donativos_autonomicos, inversion_vivienda_habitual, inversion_empresas_nueva_creacion, "
    "gastos_eficiencia_energetica, gastos_salud, gastos_deporte_cultura, viudedad (bool), "
    "vivienda_en_municipio_rural "
    "(bool), municipio_rural_o_despoblacion (bool), base_imponible_resto_unidad_familiar, miembros_unidad_familiar. "
    "Datos más finos de las deducciones autonómicas — cónyuge: edad_conyuge, grado_discapacidad_conyuge, "
    "necesita_asistencia_conyuge (bool); a cargo: num_familiares_discapacidad_a_cargo y "
    "num_familiares_discapacidad_65_a_cargo (TOTAL de hijos y ascendientes con discapacidad; si los das por separado "
    "arriba no hace falta: se suman), num_personas_acogidas_mayores_o_discapacidad; municipio: "
    "habitantes_municipio; partes del año (días): dias_alquiler_vivienda_habitual, dias_acogimiento, dias_desempleo; "
    "declaraciones entre las que se reparte la deducción (la del usuario incluida): "
    "num_declaraciones_mismo_alquiler, num_declaraciones_misma_vivienda; condiciones (bool): progenitores_trabajan, "
    "victima_violencia_genero, victima_terrorismo, vivienda_protegida, vivienda_nueva_construccion, "
    "primera_vivienda; años (AAAA): ejercicio_viudedad, ejercicio_adquisicion_vivienda, "
    "ejercicio_traslado_residencia; otros gastos: gastos_veterinarios, adquisicion_vehiculo_electrico, "
    "inversion_economia_social. Algunos gastos son un TOTAL que admite desglose en partes excluyentes entre sí: "
    "gastos_eficiencia_energetica (parte: gastos_instalaciones_renovables); gastos_deporte_cultura (gastos_deporte, "
    "gastos_cultura); gastos_salud (gastos_honorarios_sanitarios —sin dentista ni salud mental—, "
    "gastos_salud_bucodental, gastos_salud_mental, gastos_optica, gastos_enfermedades_raras, gastos_ela); "
    "inversion_vivienda_habitual (pagos_adquisicion_vivienda y pagos_rehabilitacion_vivienda, ambos sin intereses, e "
    "intereses_prestamo_vivienda); donativos_autonomicos (donativos_medio_ambiente, donativos_cultura_patrimonio, "
    "donativos_investigacion, donativos_cooperacion_fines_sociales, donativos_lengua_propia, "
    "donativos_fundaciones_autonomicas). Si solo das las partes, el total es su suma; si das también el total, no "
    "puede ser menor que la suma de sus partes (el cálculo lo rechaza). Detalle de cada hijo: `descendientes`, una "
    "lista con un objeto por hijo (ver el esquema). Con ese detalle NO indiques num_descendientes, "
    "num_descendientes_menores_3, hijos_nacidos_en_el_ejercicio, hijos_nacidos_ultimos_3_ejercicios, "
    "hijos_adoptados_en_el_ejercicio, num_hijos_primer_ciclo_infantil, num_descendientes_discapacidad, "
    "num_descendientes_discapacidad_65 ni num_descendientes_asistencia, ni el total de los gastos que repartas por "
    "hijo: se obtienen del detalle, y una cifra distinta se rechaza"
)

# One child of ``descendientes`` (hacienda_gpt.tax.irpf.Descendiente).
_DESCENDIENTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "edad": {
            "type": "integer",
            "minimum": 0,
            "description": "Edad cumplida a 31 de diciembre del ejercicio (0 = nacido ese año).",
        },
        "fecha_nacimiento": {
            "type": "string",
            "description": "AAAA-MM-DD. Alternativa a `edad`; permite prorratear por días.",
        },
        "grado_discapacidad": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Grado reconocido; 65 si su incapacidad está declarada judicialmente.",
        },
        "necesita_asistencia": {
            "type": "boolean",
            "description": (
                "Con discapacidad del 33 % al 64 %: necesita ayuda de terceras personas o tiene movilidad reducida "
                "(añade los gastos de asistencia al mínimo por discapacidad; desde el 65 % se añaden siempre)."
            ),
        },
        "etapa_educativa": {"type": "string", "enum": _ETAPAS_EDUCATIVAS},
        "ejercicio_adopcion": {"type": "integer", "description": "Año de la adopción; omítelo si no es adoptado."},
        "adopcion_internacional": {"type": "boolean"},
        "parto_multiple": {
            "type": "boolean",
            "description": "Nacido en parto múltiple, o adoptado junto con otros en la misma fecha.",
        },
        "meses_guarderia": {
            "type": "integer",
            "minimum": 0,
            "maximum": 12,
            "description": "Meses del ejercicio en guardería o escuela infantil.",
        },
        "gastos": {
            "type": "object",
            "description": (
                "Gastos del año pagados por ESTE hijo, {campo: euros}, con los mismos nombres que los gastos de "
                "`datos`: gastos_guarderia, gastos_escolaridad, gastos_libros_texto, gastos_ensenanza_idiomas, "
                "gastos_uniformes_escolares, gastos_optica, gastos_deporte… El total de cada gasto será la suma de "
                "los hijos."
            ),
            "additionalProperties": {"type": "number", "minimum": 0},
        },
    },
}
_DESCENDIENTES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": (
        "Detalle de cada hijo con derecho al mínimo por descendientes (cada uno con `edad` o `fecha_nacimiento`). "
        "Permite aplicar las deducciones autonómicas que dependen de la edad, la etapa educativa, la discapacidad "
        "o los gastos de cada hijo. Los recuentos de hijos se calculan a partir de esta lista. Una lista vacía "
        "quita el detalle guardado."
    ),
    "items": _DESCENDIENTE_SCHEMA,
}


def _datos_schema(description: str) -> dict[str, Any]:
    """Schema of a ``{campo: valor}`` object of calculator inputs.

    The scalar fields are listed in the tool description (``_INPUT_FIELDS_DOC``)
    and checked by the calculator, whose error names the offending field; only
    the structured input is spelled out here.
    """
    return {"type": "object", "description": description, "properties": {"descendientes": _DESCENDIENTES_SCHEMA}}


CALCULATE_TOOL = ToolSpec(
    name="calcular_irpf",
    description=(
        "Calcula la declaración del IRPF de forma determinista con los parámetros legales del ejercicio (escalas, "
        "mínimos, reducciones y deducciones estatales) y devuelve el desglose línea a línea con su referencia legal, "
        "como un fragmento citable [F#]. Úsala SIEMPRE que haga falta una cuota, un ahorro o una comparación: no "
        "calcules impuestos por tu cuenta. Parte de los datos del expediente del usuario si lo hay; `datos` añade o "
        "sustituye cifras. Para comparar alternativas (individual frente a conjunta, aportar o no a un plan de "
        "pensiones, vender este año o el siguiente), pasa `escenarios`: cada uno aplica sus `cambios` sobre los "
        "datos base y el resultado indica la diferencia con el primero. Importes anuales en euros. "
        f"Campos admitidos en `datos` y `cambios`: {_INPUT_FIELDS_DOC}. Lee los avisos del resultado: dicen qué no "
        "se ha modelado (en particular, la parte autonómica) y debes trasladar al usuario los que le afecten."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ejercicio": {"type": "integer", "description": "Año fiscal. Por defecto, el del expediente."},
            "datos": _datos_schema("Cifras del contribuyente que añadir o corregir sobre el expediente."),
            "escenarios": {
                "type": "array",
                "description": "Alternativas a comparar. Omítelo para un único cálculo.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "nombre": {"type": "string", "description": "Etiqueta corta, p. ej. 'Con plan de pensiones'."},
                        "cambios": _datos_schema("Campos que cambian respecto a los datos base."),
                    },
                    "required": ["nombre", "cambios"],
                },
            },
        },
    },
)

JOINT_TOOL = ToolSpec(
    name="comparar_declaracion_conjunta",
    description=(
        "Compara, para un matrimonio, presentar dos declaraciones individuales o una conjunta, calculando las tres con "
        "la calculadora determinista, y dice cuál sale más barata y por cuánto. Úsala cuando el usuario esté casado y "
        "pregunte cómo le conviene declarar, o cuando uno de los cónyuges tenga pocos ingresos. `conyuge_a` parte de "
        "los datos del expediente (el usuario); `conyuge_b` son los datos del otro cónyuge. Los hijos se toman como "
        f"comunes. Importes anuales en euros. Campos admitidos en ambos: {_INPUT_FIELDS_DOC}. Devuelve fragmentos "
        "citables [F#] y los supuestos que ha hecho: trasládalos al usuario."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ejercicio": {"type": "integer", "description": "Año fiscal. Por defecto, el del expediente."},
            "conyuge_a": _datos_schema(
                "Cifras del usuario que añadir o corregir sobre su expediente. El detalle de los hijos comunes "
                "(`descendientes`) va aquí."
            ),
            "conyuge_b": {
                "type": "object",
                "description": (
                    "Cifras del otro cónyuge (salario, cotizaciones, retenciones…). No repitas aquí los gastos de "
                    "los hijos que ya estén repartidos en `descendientes` de conyuge_a."
                ),
            },
        },
        "required": ["conyuge_b"],
    },
)

_NUM: dict[str, Any] = {"type": "number", "minimum": 0}
_BOOL: dict[str, Any] = {"type": "boolean"}

ACTIVITY_TOOL = ToolSpec(
    name="calcular_rendimiento_actividad",
    description=(
        "Calcula el rendimiento neto anual de una actividad económica de un autónomo en estimación directa "
        "simplificada: ingresos íntegros menos los gastos que indique el usuario por categoría, menos el porcentaje de "
        "gastos de difícil justificación (art. 30 RIRPF) y, si proceden, las reducciones del art. 32 LIRPF. No valora "
        "si un gasto es deducible. No sirve para módulos ni para estimación directa normal. El campo "
        "`rendimiento_actividad_neto` del resultado es el que debe pasarse a calcular_irpf. Importes anuales sin IVA. "
        "Traslada los avisos al usuario."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ejercicio": {"type": "integer"},
            **{k: _NUM for k in ("ingresos_integros", "seguridad_social_autonomo", "suministros", "alquileres",
                                 "servicios_profesionales", "otros_gastos", "amortizaciones", "cifra_negocios_ano_anterior",
                                 "reduccion_rendimientos_trabajo")},
            "otras_rentas_no_exentas": {"type": "number", "description": "Rentas distintas de la actividad; omítelo si no se conocen."},
            "cumple_requisitos_reduccion_dependiente": {
                "type": "boolean",
                "description": "Solo si el usuario confirma TODOS los requisitos del art. 32.2.2.º LIRPF (cliente único, etc.).",
            },
            "discapacidad": _BOOL,
            "discapacidad_ampliada": _BOOL,
            "rendimientos_irregulares": {
                "type": "number", "minimum": 0,
                "description": "Parte del rendimiento neto generada en más de dos años o notoriamente irregular (art. 32.1 LIRPF, art. 25 RIRPF).",
            },
            "reduccion_inicio_actividad": {
                "type": "boolean",
                "description": "Solo si el usuario confirma los requisitos del art. 32.3 LIRPF (actividad nueva, primer año con rendimiento positivo o el siguiente).",
            },
            "ceuta_melilla": {
                "type": "string", "enum": ["ceuta", "melilla"],
                "description": "Actividad desarrollada en Ceuta o Melilla con derecho a la deducción del art. 68.4 LIRPF.",
            },
        },
        "required": ["ingresos_integros"],
    },
)  # fmt: skip

MODELO_130_TOOL = ToolSpec(
    name="calcular_modelo_130",
    description=(
        "Calcula el pago fraccionado trimestral del IRPF (modelo 130) de una actividad en estimación directa: el 20 % "
        "del rendimiento neto acumulado desde el 1 de enero, menos los pagos de trimestres anteriores, las retenciones "
        "soportadas (profesionales) y la minoración del art. 110.3.c) RIRPF. El resultado nunca es negativo. Indica "
        "además si un profesional está exonerado por tener retención en al menos el 70 % de sus ingresos del año "
        "anterior (art. 109.2 RIRPF). Todos los importes son ACUMULADOS desde el 1 de enero. Para el trimestre "
        "siguiente, pasa en `pagos_trimestres_anteriores` la suma de los `pago_previo_a_minoracion` anteriores y en "
        "`minoracion_pendiente_trimestres_anteriores` la `minoracion_pendiente`."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ejercicio": {"type": "integer"},
            "trimestre": {"type": "integer", "minimum": 1, "maximum": 4},
            **{k: _NUM for k in ("ingresos_acumulados", "gastos_acumulados", "retenciones_acumuladas",
                                 "pagos_trimestres_anteriores", "minoracion_pendiente_trimestres_anteriores",
                                 "ingresos_ano_anterior", "ingresos_con_retencion_ano_anterior",
                                 "ingresos_con_retencion_acumulados")},
            "rendimiento_neto_ejercicio_anterior": {"type": "number"},
            "aplicar_gastos_dificil_justificacion": _BOOL,
            "actividad_profesional": _BOOL,
            "inicio_actividad": _BOOL,
            "ceuta_melilla": {
                "type": "string", "enum": ["ceuta", "melilla"],
                "description": "Actividad desarrollada en Ceuta o Melilla con derecho a la deducción del art. 68.4 LIRPF: el porcentaje se reduce (art. 110.2 RIRPF).",
            },
        },
        "required": ["trimestre", "ingresos_acumulados"],
    },
)  # fmt: skip

MODELO_303_TOOL = ToolSpec(
    name="calcular_modelo_303",
    description=(
        "Calcula la autoliquidación trimestral del IVA (modelo 303) en régimen general con operaciones interiores: IVA "
        "devengado por tipo (general, reducido, superreducido) a partir de las bases imponibles, menos el IVA soportado "
        "deducible que indique el usuario y las cuotas a compensar de períodos anteriores. Devuelve el resultado a "
        "ingresar o a compensar. No decide qué tipo corresponde a cada producto ni si una cuota es deducible. No cubre "
        "prorrata, recargo de equivalencia, régimen simplificado, criterio de caja ni operaciones intracomunitarias."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ejercicio": {"type": "integer"},
            "trimestre": {"type": "integer", "minimum": 1, "maximum": 4},
            **{k: _NUM for k in ("base_tipo_general", "base_tipo_reducido", "base_tipo_superreducido",
                                 "cuotas_soportadas_deducibles", "cuotas_soportadas_bienes_inversion",
                                 "cuotas_a_compensar_periodos_anteriores")},
        },
        "required": ["trimestre"],
    },
)  # fmt: skip

SOCIEDADES_TOOL = ToolSpec(
    name="calcular_impuesto_sociedades",
    description="Calcula el Impuesto sobre Sociedades de una sociedad pequeña (territorio común, art. 29.1 LIS) para un período iniciado en 2025-2028: del resultado contable a la cuota diferencial, con reserva de capitalización, compensación de bases negativas y el tipo que corresponda (general, escala de microempresas, reducida dimensión o nueva creación). Los ajustes y las deducciones son totales que aporta el usuario. Devuelve el desglose y avisos de lo no modelado, que hay que trasladar siempre.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["ejercicio"],
        "properties": {
            "ejercicio": {"type": "integer", "description": "Año en que se inicia el período impositivo (2025-2028)"},
            "resultado_contable": {
                "type": "number",
                "description": "Resultado contable antes del gasto por Impuesto sobre Sociedades (puede ser negativo)",
            },
            "ajustes_positivos": {"type": "number", "minimum": 0},
            "ajustes_negativos": {"type": "number", "minimum": 0},
            "cifra_negocios_anterior": {
                "type": ["number", "null"],
                "minimum": 0,
                "description": "Importe neto de la cifra de negocios del período anterior. Sin él se aplica el 25 %",
            },
            "cifra_negocios_12_meses_anteriores": {"type": ["number", "null"], "minimum": 0},
            "entidad_patrimonial": {"type": "boolean"},
            "nueva_creacion_tipo_reducido": {
                "type": "boolean",
                "description": "El usuario afirma cumplir los requisitos del 15 % de entidades de nueva creación",
            },
            "incremento_fondos_propios": {"type": "number", "minimum": 0},
            "incremento_plantilla_porcentaje": {"type": ["number", "null"], "minimum": 0},
            "reserva_capitalizacion_pendiente": {"type": "number", "minimum": 0},
            "bases_negativas_pendientes": {"type": "number", "minimum": 0},
            "nueva_creacion_sin_limite_bases_negativas": {"type": "boolean"},
            "dias_periodo": {"type": "integer", "minimum": 1, "maximum": 366},
            "reserva_nivelacion": {
                "type": ["number", "null"],
                "minimum": 0,
                "description": "Importe de reserva de nivelación (art. 105 LIS) que el usuario quiere dotar; solo entidades con la escala de microempresas.",
            },
            "reserva_nivelacion_maxima": {"type": "boolean", "description": "Dotar el máximo que permita la ley."},
            "reserva_nivelacion_pendiente": {"type": "number", "minimum": 0},
            "reserva_nivelacion_vence": {"type": "number", "minimum": 0},
            "bonificaciones_deducciones": {"type": "number", "minimum": 0},
            "retenciones_ingresos_cuenta": {"type": "number", "minimum": 0},
            "pagos_fraccionados": {"type": "number", "minimum": 0},
        },
    },
)

SOCIEDADES_PAGO_TOOL = ToolSpec(
    name="calcular_pago_fraccionado_sociedades",
    description="Calcula un pago fraccionado del Impuesto sobre Sociedades (modelo 202, art. 40 LIS): modalidad 'cuota' (18 % de la cuota del último período menos deducciones y retenciones) o modalidad 'base'. Con la escala de microempresas en modalidad base aplica cinco séptimos de cada tipo a su tramo, como el modelo 202. Si el texto legal no permite una cifra exacta (cifra de negocios de 10 millones o más, último período inferior al año) devuelve importe nulo y lo explica en avisos.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["ejercicio"],
        "properties": {
            "ejercicio": {"type": "integer"},
            "modalidad": {"type": "string", "enum": ["cuota", "base"]},
            "cifra_negocios_anterior": {"type": ["number", "null"], "minimum": 0},
            "cifra_negocios_12_meses_anteriores": {"type": ["number", "null"], "minimum": 0},
            "entidad_patrimonial": {"type": "boolean"},
            "nueva_creacion_tipo_reducido": {"type": "boolean"},
            "cuota_integra_ultimo_periodo": {"type": "number", "minimum": 0},
            "deducciones_bonificaciones_ultimo_periodo": {"type": "number", "minimum": 0},
            "retenciones_ultimo_periodo": {"type": "number", "minimum": 0},
            "ultimo_periodo_inferior_al_ano": {"type": "boolean"},
            "base_imponible_acumulada": {"type": "number"},
            "dias_periodo": {"type": "integer", "minimum": 1, "maximum": 366},
            "bonificaciones": {"type": "number", "minimum": 0},
            "retenciones_acumuladas": {"type": "number", "minimum": 0},
            "pagos_fraccionados_anteriores": {"type": "number", "minimum": 0},
        },
    },
)

AUTONOMO_SOCIEDAD_TOOL = ToolSpec(
    name="comparar_autonomo_vs_sociedad",
    description="Compara, solo en impuestos sobre la renta, ganar el mismo beneficio como autónomo (IRPF) o a través de una sociedad que paga un salario al socio y reparte el resto como dividendo (Impuesto sobre Sociedades + IRPF del socio). Las cotizaciones a la Seguridad Social solo entran en la cuenta si el usuario las aporta (cuota_autonomos, cotizaciones_socio, cotizaciones_empresa); nunca se estiman. NO incluye las reglas del art. 27 LIRPF sobre socios profesionales, IVA ni costes de gestión, y el beneficio no repartido no tributa en el socio hasta que se distribuya. No declara qué opción es mejor: presenta siempre las cifras junto con los avisos y no concluyas más allá de la aritmética. Ejercicios 2025 y 2026.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["ejercicio", "beneficio_antes_retribucion"],
        "properties": {
            "ejercicio": {"type": "integer"},
            "beneficio_antes_retribucion": {
                "type": "number",
                "minimum": 0,
                "description": "Beneficio anual del negocio antes de retribuir al titular",
            },
            "salario": {
                "type": "number",
                "minimum": 0,
                "description": "Salario bruto anual que la sociedad pagaría al socio",
            },
            "dividendo": {
                "type": ["number", "null"],
                "minimum": 0,
                "description": "Dividendo repartido; null = todo el beneficio después de impuestos",
            },
            "cuota_autonomos": {
                "type": ["number", "null"],
                "minimum": 0,
                "description": "Cuota anual de autónomos que paga el usuario (gasto de la actividad).",
            },
            "cotizaciones_socio": {
                "type": ["number", "null"],
                "minimum": 0,
                "description": "Cotizaciones anuales a cargo del socio en la opción sociedad.",
            },
            "cotizaciones_empresa": {
                "type": ["number", "null"],
                "minimum": 0,
                "description": "Cotizaciones anuales a cargo de la sociedad (gasto de la sociedad).",
            },
            "comunidad_autonoma": {"type": ["string", "null"]},
            "cifra_negocios_anterior": {"type": ["number", "null"], "minimum": 0},
            "nueva_creacion_tipo_reducido": {"type": "boolean"},
        },
    },
)

PATRIMONIO_TOOL = ToolSpec(
    name="calcular_patrimonio",
    description="Calcula el Impuesto sobre el Patrimonio y el Impuesto Temporal de Solidaridad de las Grandes Fortunas (ITSGF) de UN contribuyente residente en una comunidad de régimen común, devengo 31 de diciembre de 2026. Aplica la exención de vivienda habitual, el mínimo exento y la escala estatales o autonómicos, el límite conjunto con el IRPF (solo si se dan las dos cifras del IRPF), la bonificación general autonómica y el ITSGF, e indica si hay obligación de declarar. Los importes son los valores de la parte del contribuyente, ya valorados por el usuario. No cubre Navarra ni País Vasco, no residentes ni bonificaciones autonómicas de bienes concretos. Traslada los avisos al usuario.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["comunidad"],
        "properties": {
            "ejercicio": {"type": "integer", "description": "Año del devengo (31-12). Solo 2026."},
            "comunidad": {"type": "string", "description": "Comunidad autónoma de residencia."},
            "vivienda_habitual": {"type": "number", "minimum": 0},
            "otros_inmuebles": {"type": "number", "minimum": 0},
            "depositos": {"type": "number", "minimum": 0},
            "valores_cotizados": {"type": "number", "minimum": 0},
            "participaciones_no_cotizadas": {"type": "number", "minimum": 0},
            "seguros_y_rentas": {"type": "number", "minimum": 0},
            "vehiculos_y_otros": {"type": "number", "minimum": 0},
            "deuda_vivienda_habitual": {"type": "number", "minimum": 0},
            "otras_deudas": {"type": "number", "minimum": 0},
            "bienes_exentos_empresa_familiar": {
                "type": "number",
                "minimum": 0,
                "description": "Bienes afectos y participaciones que el usuario declara exentos (art. 4.Ocho).",
            },
            "grado_discapacidad": {"type": "number", "minimum": 0, "maximum": 100},
            "irpf_base_imponible": {
                "type": "number",
                "minimum": 0,
                "description": "Suma de bases imponibles del IRPF, sin las ganancias de elementos con más de un año. Omítelo si no se conoce.",
            },
            "irpf_cuota_integra": {
                "type": "number",
                "minimum": 0,
                "description": "Suma de cuotas íntegras del IRPF, sin la parte de esas ganancias. Omítelo si no se conoce.",
            },
            "bienes_no_productivos": {"type": "number", "minimum": 0},
        },
    },
)

ISD_TOOL = ToolSpec(
    name="calcular_sucesiones_donaciones",
    description="Calcula el Impuesto sobre Sucesiones y Donaciones de UN heredero, legatario, beneficiario de seguro o donatario (llámala una vez por persona). Aplica la ley estatal y, si está cargada, la normativa de la comunidad autónoma (reducciones, tarifa, coeficientes y bonificaciones); están cargadas las 15 comunidades de régimen común; sin comunidad, o en Navarra y País Vasco, aplica solo la ley estatal y lo avisa. No decide qué comunidad corresponde: en herencias es la de residencia habitual del fallecido; en donaciones de inmuebles, donde esté el inmueble; en el resto de donaciones, la de residencia del donatario. Devuelve el desglose con referencias legales y avisos que SIEMPRE hay que trasladar.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["modo", "valor_recibido"],
        "properties": {
            "modo": {"type": "string", "enum": ["sucesion", "donacion"]},
            "comunidad": {
                "type": "string",
                "description": "Comunidad cuya normativa se aplica; omítela para solo ley estatal.",
            },
            "fecha_devengo": {"type": "string", "description": "AAAA-MM-DD del fallecimiento o de la donación (2026)."},
            "parentesco": {
                "type": "string",
                "description": "Parentesco del beneficiario con el fallecido o donante: conyuge, pareja_de_hecho, hijo, otro_descendiente (nieto), padre, otro_ascendiente (abuelo), hermano, sobrino, tio, colateral_afinidad (cuñado), hijastro, yerno_nuera, padrastro, suegro, colateral_cuarto_grado (primo), colateral_lejano, extrano.",
            },
            "grupo": {
                "type": "string",
                "enum": ["I", "II", "III", "IV"],
                "description": "Solo si no se conoce el parentesco exacto.",
            },
            "edad": {
                "type": "integer",
                "description": "Edad del beneficiario; imprescindible en descendientes (menores de 21 = grupo I).",
            },
            "grado_discapacidad": {"type": "integer"},
            "valor_recibido": {
                "type": "number",
                "minimum": 0,
                "description": "Valor neto de lo que recibe (ya restadas cargas, deudas y gastos deducibles), sin el seguro de vida.",
            },
            "incluir_ajuar": {
                "type": "boolean",
                "description": "Añade la presunción legal del 3 % de ajuar doméstico (solo herencias).",
            },
            "seguro_vida": {
                "type": "number",
                "minimum": 0,
                "description": "Importe percibido como beneficiario de seguros de vida del fallecido.",
            },
            "valor_vivienda_habitual": {
                "type": "number",
                "minimum": 0,
                "description": "Parte de valor_recibido que es la vivienda habitual del fallecido.",
            },
            "valor_total_vivienda": {
                "type": "number",
                "minimum": 0,
                "description": "Valor real de la vivienda entera, para las comunidades que gradúan el porcentaje por él (Asturias, Galicia).",
            },
            "participacion_vivienda": {"type": "number", "description": "Fracción (0-1) de esa vivienda que adquiere."},
            "valor_empresa_familiar": {
                "type": "number",
                "minimum": 0,
                "description": "Parte de valor_recibido con derecho a la reducción de empresa familiar, según declara el usuario.",
            },
            "valor_patrimonio_historico": {"type": "number", "minimum": 0},
            "otras_reducciones": {
                "type": "number",
                "minimum": 0,
                "description": "Importe de otras reducciones a las que el usuario afirma tener derecho.",
            },
            "patrimonio_preexistente": {
                "type": "number",
                "minimum": 0,
                "description": "Patrimonio del beneficiario antes de la adquisición.",
            },
            "escritura_publica": {"type": "boolean", "description": "La donación consta en escritura pública."},
        },
    },
)

BENEFITS_TOOL = ToolSpec(
    name="listar_beneficios_fiscales",
    description=(
        "Revisa el catálogo de reducciones, mínimos y deducciones ESTATALES del IRPF contra la situación del "
        "usuario y devuelve, para cada uno, si ya está aplicado, si está disponible (con el ahorro estimado por la "
        "calculadora), o qué dato falta para saberlo. Es el punto de partida para responder a «¿cómo pago menos?». "
        "No incluye deducciones autonómicas: el resultado indica cómo buscarlas. Admite los mismos `datos` que "
        "calcular_irpf."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ejercicio": {"type": "integer", "description": "Año fiscal. Por defecto, el del expediente."},
            "datos": _datos_schema("Cifras del contribuyente que añadir o corregir sobre el expediente."),
        },
    },
)


_POSICION_LATENTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "descripcion": {"type": "string", "description": "Qué es (p. ej. 'Fondo indexado', 'Acciones X')."},
        "importe": {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": "Pérdida (o plusvalía) que realizaría la venta, en euros y en positivo.",
        },
        "tipo": {
            "type": "string",
            "enum": ["valores_cotizados", "valores_no_cotizados", "otro_elemento"],
            "description": "Cambia la regla de recompra (art. 33.5 e, f y g LIRPF). Por defecto, valores cotizados.",
        },
        "titular": {"type": "string", "enum": ["contribuyente", "conyuge"], "description": "Por defecto, el usuario."},
    },
    "required": ["importe"],
}
# Constraints of the optimiser (hacienda_gpt.tax.optimizador.Restricciones), as tool arguments.
_RESTRICCIONES = (
    "aportacion_maxima_pensiones",
    "aportacion_maxima_pensiones_conyuge",
    "acepta_iliquidez",
    "donativo_previsto",
    "presupuesto_donativos",
    "donativo_recurrente_importe_anterior",
    "perdidas_latentes",
    "ganancias_latentes",
)

OPTIMIZE_TOOL = ToolSpec(
    name="optimizar_impuestos",
    description=(
        "Busca de forma determinista la MEJOR COMBINACIÓN de las opciones que la ley deja al contribuyente para "
        "pagar menos IRPF en el ejercicio: aportar a planes de pensiones (cada cónyuge), declarar conjunta o "
        "individualmente, a qué tipo de entidad donar lo que ya piensa donar (o hasta el presupuesto que diga) y "
        "vender inversiones con pérdidas latentes. Las opciones interactúan (una aportación puede llevar la base por "
        "debajo del límite de una deducción autonómica; la conjunta cambia todos los umbrales), así que calcula las "
        "combinaciones con la calculadora en vez de sumar ahorros por separado. Devuelve el plan: qué hacer y antes "
        "de cuándo, el ahorro de cada medida dentro del plan y por separado, su naturaleza (ahorro definitivo, "
        "diferimiento o gasto), sus requisitos no comprobados y sus citas, y los cálculos actual y óptimo, todo "
        "como fragmentos citables [F#]. Úsala para «¿cómo pago menos?», «¿qué me conviene hacer antes de fin de "
        "año?» o «¿conjunta o individual?». Después del 31 de diciembre solo quedan las opciones de la declaración. "
        "Parte del expediente; `datos` añade o corrige cifras del usuario y `conyuge` son las del otro cónyuge (sin "
        "ellas no se compara la conjunta). Las restricciones son las que diga el usuario: no las inventes; si no las "
        "ha dicho, omítelas y traslada los supuestos del resultado. "
        f"Campos admitidos en `datos` y `conyuge`: {_INPUT_FIELDS_DOC}."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ejercicio": {"type": "integer", "description": "Año fiscal. Por defecto, el del expediente."},
            "datos": _datos_schema("Cifras del usuario que añadir o corregir sobre el expediente."),
            "conyuge": {
                "type": "object",
                "description": (
                    "Cifras del otro cónyuge (salario, cotizaciones, retenciones, aportaciones…). No repitas aquí los "
                    "gastos de los hijos que ya estén en `descendientes` del usuario."
                ),
            },
            "aportacion_maxima_pensiones": {
                "type": "number",
                "minimum": 0,
                "description": "Lo que el usuario puede aportar todavía este año a planes de pensiones, en euros.",
            },
            "aportacion_maxima_pensiones_conyuge": {
                "type": "number",
                "minimum": 0,
                "description": "Lo mismo para el cónyuge.",
            },
            "acepta_iliquidez": {
                "type": "boolean",
                "description": "false si el usuario no quiere inmovilizar dinero hasta la jubilación.",
            },
            "donativo_previsto": {
                "type": "number",
                "minimum": 0,
                "description": "Lo que el usuario va a donar este año de todos modos: el optimizador solo elige a qué "
                "tipo de entidad y, si declaran por separado, qué cónyuge lo paga.",
            },
            "presupuesto_donativos": {
                "type": "number",
                "minimum": 0,
                "description": "Lo máximo que estaría dispuesto a donar en total. Sin él, nunca se propone donar más.",
            },
            "donativo_recurrente_importe_anterior": {
                "type": "number",
                "exclusiveMinimum": 0,
                "description": "Lo que donó el año pasado a una entidad a la que también donó el anterior (sin bajar "
                "el importe): permite el porcentaje de donativos recurrentes.",
            },
            "perdidas_latentes": {
                "type": "array",
                "items": _POSICION_LATENTE_SCHEMA,
                "description": "Inversiones que hoy darían pérdida si se vendieran.",
            },
            "ganancias_latentes": {
                "type": "array",
                "items": _POSICION_LATENTE_SCHEMA,
                "description": "Inversiones con plusvalía latente (solo para avisar si realizarlas sale gratis).",
            },
        },
    },
)


CALENDAR_TOOL = ToolSpec(
    name="calendario_fiscal",
    description=(
        "Devuelve los próximos plazos fiscales que aplican al usuario según su expediente (fin del ejercicio para "
        "aportaciones y donativos, declaración de la Renta, IVA y pagos fraccionados si tiene actividad económica, "
        "modelo 720 si tiene bienes en el extranjero), con su fecha, días restantes y referencia legal, como un "
        "fragmento citable. Úsala cuando pregunte por plazos o cuando una recomendación dependa de una fecha."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {"dias": {"type": "integer", "description": "Horizonte en días (por defecto 365)."}},
    },
)


VALIDITY_TOOL = ToolSpec(
    name="consultar_vigencia",
    description=(
        "Consulta el grafo de doctrina para saber si una consulta de la DGT, una resolución del TEAC o una "
        "sentencia sigue vigente: qué resoluciones posteriores la confirman, matizan, superan o contradicen. "
        "Úsala antes de apoyarte en doctrina de hace años o cuando dos fuentes parezcan discrepar. El resultado es "
        "un indicio construido a partir de las citas entre resoluciones, no una fuente: para afirmar que algo está "
        "superado, busca y cita la resolución posterior con buscar_fuentes."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "referencia": {
                "type": "string",
                "description": (
                    "La resolución tal como se cita: 'V1234-19', 'resolución TEAC 00/1234/2017', "
                    "'STS de 3 de junio de 2020 (rec. 1234/2019)', 'STC 182/2021'."
                ),
            }
        },
        "required": ["referencia"],
    },
)

_RELACION_VERBO = {
    "cita": "la cita",
    "confirma": "la confirma",
    "matiza": "la matiza",
    "supera": "la supera",
    "contradice": "la contradice",
}


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    summary: str
    is_error: bool = False


def _error(message: str) -> ToolOutcome:
    return ToolOutcome(content=message, summary=message[:120], is_error=True)


_CASE_ONLY_TOOLS = frozenset({READ_CASE_TOOL.name, RECORD_FACTS_TOOL.name, EVALUATE_RULES_TOOL.name})


def _money(value: Decimal) -> str:
    """1234.5 → '1.234,50 €' (Spanish formatting, sign preserved)."""
    text = f"{abs(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if value < 0 else ''}{text} €"


def _render_result(nombre: str, result: IRPFResult) -> str:
    lines = [f"Cálculo del IRPF {result.ejercicio} — {nombre} (tributación {result.tributacion.replace('_', ' ')})"]
    for line in result.lineas:
        ref = f" [{line.referencia}]" if line.referencia else ""
        lines.append(f"- {line.concepto}: {_money(line.importe)}{ref}")
    lines.append(f"- Tipo medio efectivo: {result.tipo_medio_efectivo} %")
    if result.avisos:
        lines.append("Avisos (lo que este cálculo no cubre o da por supuesto):")
        lines.extend(f"  * {aviso}" for aviso in result.avisos)
    lines.append(f"Parámetros: {result.fuente}.")
    return "\n".join(lines)


_NATURALEZA = {
    "ahorro_definitivo": "ahorro definitivo",
    "diferimiento": "diferimiento (se recupera más adelante)",
    "requiere_gastar": "requiere gastar",
}


def _render_plan(plan: PlanOptimo) -> str:
    """The optimiser's plan as a citable text: every figure the answer may quote is in it."""
    modo = {
        "individual": "individual",
        "conjunta_biparental": "conjunta",
        "conjunta_monoparental": "conjunta monoparental",
    }
    lines = [
        f"Plan óptimo del IRPF {plan.ejercicio} a {plan.fecha.strftime('%d/%m/%Y')} "
        + (
            "(antes del cierre del ejercicio)"
            if plan.fase == "antes_del_cierre"
            else "(solo opciones de la declaración)"
        ),
        f"- Situación actual: resultado {_money(plan.base.resultado_total)} (tributación "
        f"{modo[plan.base.tributacion]}; positivo = a pagar, negativo = a devolver).",
        f"- Con el plan: resultado {_money(plan.optimo.resultado_total)} (tributación {modo[plan.optimo.tributacion]}).",
        f"- Ahorro total del plan combinado: {_money(plan.ahorro_total)}.",
    ]
    if plan.palancas:
        lines.append(
            "Medidas (el ahorro de cada una es lo que aporta DENTRO del plan; no se suman los ahorros por separado):"
        )
        for n, p in enumerate(plan.palancas, start=1):
            detalle = [
                f"ahorro dentro del plan {_money(p.ahorro_marginal)}",
                f"por sí sola {_money(p.ahorro_aislado)}"
                + ("" if p.accion_aislada == p.accion else f" («{p.accion_aislada}»)"),
                _NATURALEZA[p.naturaleza],
            ]
            if p.compromiso:
                detalle.append(f"compromiso {_money(p.compromiso)}")
            if p.ahorro_por_euro is not None:
                detalle.append(f"{str(p.ahorro_por_euro).replace('.', ',')} € de ahorro por euro comprometido")
            if p.coste_neto is not None:
                detalle.append(f"coste neto {_money(p.coste_neto)}")
            lines.append(f"{n}. {p.accion}: " + "; ".join(detalle) + f" [{p.referencia}]")
            lines.extend(f"   - Requisito: {r}" for r in p.requisitos)
        lines.append(
            f"Suma de los ahorros por separado: {_money(plan.suma_ahorros_aislados)}; diferencia con el plan "
            f"combinado: {_money(plan.interaccion)}."
        )
        lines.extend(f"  * {m}" for m in plan.explicacion_interaccion)
    else:
        lines.append("No hay ninguna combinación de las opciones disponibles que reduzca el impuesto.")
    if plan.cambios_traza:
        lines.append(
            "Líneas del cálculo que cambian (suma del hogar): "
            + "; ".join(f"{c.concepto}: {_money(c.antes)} → {_money(c.despues)}" for c in plan.cambios_traza)
        )
    for titulo, items in (
        ("Opciones no disponibles", [f"{nd.nombre}: {nd.detalle}" for nd in plan.no_disponibles]),
        ("Opciones descartadas", [f"{d.nombre}: {d.motivo} [{d.referencia}]" for d in plan.descartadas]),
        ("Otras oportunidades", plan.oportunidades),
        ("Supuestos (trasládalos al usuario)", plan.supuestos),
        ("Datos que faltan", plan.datos_que_faltan),
    ):
        if items:
            lines.append(f"{titulo}:")
            lines.extend(f"  * {item}" for item in items)
    lines.append(f"Búsqueda {plan.busqueda.metodo}: {plan.busqueda.nota}")
    lines.append("Citas: " + "; ".join(plan.citas))
    return "\n".join(lines)


class Toolbox:
    """Holds the per-run state the tools read and mutate."""

    def __init__(
        self,
        *,
        searcher: CorpusSearcher,
        ledger: EvidenceLedger,
        case: CaseState | None = None,
        default_k: int = 6,
        graph: DoctrineGraph | None = None,
        allowed: frozenset[str] | None = None,
    ) -> None:
        # Subagents get a subset of the tools (a researcher must not edit the case).
        self._allowed = allowed
        self._graph = graph
        self._searcher = searcher
        self.ledger = ledger
        self.case = case
        self.case_changed = False
        self._default_k = default_k

    def specs(self) -> list[ToolSpec]:
        specs = [
            SEARCH_TOOL,
            CALCULATE_TOOL,
            JOINT_TOOL,
            ACTIVITY_TOOL,
            MODELO_130_TOOL,
            MODELO_303_TOOL,
            SOCIEDADES_TOOL,
            SOCIEDADES_PAGO_TOOL,
            AUTONOMO_SOCIEDAD_TOOL,
            PATRIMONIO_TOOL,
            ISD_TOOL,
            BENEFITS_TOOL,
            OPTIMIZE_TOOL,
            CALENDAR_TOOL,
        ]
        if self._graph is not None:
            specs.append(VALIDITY_TOOL)
        if self.case is not None:
            specs.extend([READ_CASE_TOOL, RECORD_FACTS_TOOL, EVALUATE_RULES_TOOL])
        if self._allowed is not None:
            specs = [spec for spec in specs if spec.name in self._allowed]
        return specs

    def execute(self, call: ToolCall) -> ToolOutcome:
        if call.arguments_error:
            return _error(f"No se pudo interpretar la llamada: {call.arguments_error}. Reenvíala con JSON válido.")
        handlers = {
            SEARCH_TOOL.name: self._search,
            CALCULATE_TOOL.name: self._calculate,
            JOINT_TOOL.name: self._joint,
            ACTIVITY_TOOL.name: lambda a: self._autonomo(
                a, ActividadInput, calcular_rendimiento_actividad, "Rendimiento de la actividad"
            ),
            MODELO_130_TOOL.name: lambda a: self._autonomo(a, Modelo130Input, calcular_modelo_130, "Modelo 130"),
            MODELO_303_TOOL.name: lambda a: self._autonomo(a, Modelo303Input, calcular_modelo_303, "Modelo 303"),
            SOCIEDADES_TOOL.name: lambda a: self._calculadora(
                a,
                SociedadesInput,
                calcular_sociedades,
                "Impuesto sobre Sociedades",
                ejercicios_sociedades_disponibles(),
                "sociedades",
            ),
            SOCIEDADES_PAGO_TOOL.name: lambda a: self._calculadora(
                a,
                PagoFraccionadoSociedadesInput,
                calcular_pago_fraccionado_sociedades,
                "Pago fraccionado de Sociedades",
                ejercicios_sociedades_disponibles(),
                "sociedades",
            ),
            AUTONOMO_SOCIEDAD_TOOL.name: lambda a: self._calculadora(
                a,
                ComparacionInput,
                comparar_autonomo_vs_sociedad,
                "Autónomo frente a sociedad",
                [2025, 2026],
                "sociedades",
            ),
            PATRIMONIO_TOOL.name: lambda a: self._calculadora(
                a,
                PatrimonioInput,
                calcular_patrimonio,
                "Impuesto sobre el Patrimonio",
                ejercicios_patrimonio_disponibles(),
                "patrimonio",
            ),
            ISD_TOOL.name: lambda a: self._calculadora(
                a,
                BeneficiarioISD,
                calcular_isd,
                "Impuesto sobre Sucesiones y Donaciones",
                ejercicios_isd_disponibles(),
                "sucesiones",
            ),
            BENEFITS_TOOL.name: self._benefits,
            OPTIMIZE_TOOL.name: self._optimize,
            CALENDAR_TOOL.name: self._calendar,
            VALIDITY_TOOL.name: self._validity,
            READ_CASE_TOOL.name: self._read_case,
            RECORD_FACTS_TOOL.name: self._record_facts,
            EVALUATE_RULES_TOOL.name: self._evaluate_rules,
        }
        handler = handlers.get(call.name)
        unavailable = (
            (call.name in _CASE_ONLY_TOOLS and self.case is None)
            or (self._allowed is not None and call.name not in self._allowed)
        ) or (call.name == VALIDITY_TOOL.name and self._graph is None)
        if handler is None or unavailable:
            return _error(f"Herramienta no disponible: {call.name}.")
        try:
            return handler(call.arguments)
        except Exception as exc:  # a tool failure is reported to the model, never crashes the turn
            logger.exception("tool %s failed", call.name)
            return _error(f"La herramienta {call.name} falló: {exc}")

    # ------------------------------------------------------------------ #

    def _search(self, arguments: dict[str, Any]) -> ToolOutcome:
        query = arguments.get("consulta")
        if not isinstance(query, str) or not query.strip():
            return _error("Falta 'consulta' (texto no vacío).")
        source_type = arguments.get("tipo_fuente")
        if source_type is not None and source_type not in SOURCE_TYPES:
            return _error(f"'tipo_fuente' debe ser uno de: {', '.join(SOURCE_TYPES)}.")
        raw_k = arguments.get("max_resultados", self._default_k)
        k = raw_k if isinstance(raw_k, int) and not isinstance(raw_k, bool) else self._default_k
        k = max(1, min(MAX_SEARCH_RESULTS, k))

        def _text(key: str) -> str | None:
            value = arguments.get(key)
            return value.strip() if isinstance(value, str) and value.strip() else None

        date_from = _text("desde_fecha")
        if date_from is not None and normalize_date(date_from) is None:
            return _error("'desde_fecha' debe tener formato AAAA o AAAA-MM-DD.")
        filters = SearchFilters(
            date_from=date_from,
            source_type=source_type,
            tributo=_text("tributo"),
            jurisdiction_scope=_text("ambito"),
            binding_only=arguments.get("solo_vinculante") is True,
        )
        docs = self._searcher.search(query.strip(), k=k, filters=filters)
        if not docs:
            hint = " Prueba sin filtros o con otra formulación." if not filters.is_empty() else ""
            return ToolOutcome(
                content=f"Sin resultados para esta búsqueda.{hint}",
                summary="0 fragmentos",
            )
        fragments = [
            self.ledger.register(text=doc.page_content or "", metadata=self._with_validity(dict(doc.metadata or {})))
            for doc in docs
        ]
        # A repeated search can return chunks already registered; show each once.
        unique = list({fragment.fragment_id: fragment for fragment in fragments}.values())
        body = "\n\n".join(fragment.render() for fragment in unique)
        alerts = self._validity_alerts(unique)
        if alerts:
            body += "\n\n" + alerts
        return ToolOutcome(
            content=body,
            summary=f"{len(unique)} fragmentos: {', '.join(f.fragment_id for f in unique)}",
        )

    # ------------------------------------------------------------------ #
    # Tax computation
    # ------------------------------------------------------------------ #

    def _scenario_base(self, arguments: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any]]:
        """Resolve (fiscal year, case facts, explicit data) for a computation."""
        datos = arguments.get("datos") or {}
        if not isinstance(datos, dict):
            raise DatosInvalidosError("'datos' debe ser un objeto {campo: valor}.")
        facts = {fact.name: fact.value for fact in self.case.facts} if self.case is not None else {}
        raw_year = arguments.get("ejercicio")
        if isinstance(raw_year, int) and not isinstance(raw_year, bool):
            year = raw_year
        else:
            case_year = parse_fiscal_year(self.case.tax_period) if self.case is not None else None
            year = case_year if case_year is not None else max(ejercicios_disponibles())
        return year, facts, datos

    def _calculate(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            year, facts, datos = self._scenario_base(arguments)
            simulacion = simular(
                ejercicio=year, facts=facts, datos=datos, escenarios=arguments.get("escenarios"), con_beneficios=False
            )
        except (DatosInvalidosError, EjercicioNoDisponibleError) as exc:
            return _error(str(exc))

        blocks: list[str] = []
        fragment_ids: list[str] = []
        base_name = simulacion.escenarios[0].nombre
        for escenario in simulacion.escenarios:
            nombre, result = escenario.nombre, escenario.resultado
            # Each scenario is registered as evidence: the answer cites it like
            # any other source and the verifier checks figures against it.
            fragment = self.ledger.register(
                text=_render_result(nombre, result),
                metadata={
                    "title": f"Cálculo IRPF {result.ejercicio} — {nombre}",
                    "source": f"calculadora://irpf/{result.ejercicio}/{len(self.ledger) + 1}",
                    "source_authority": "CALCULADORA",
                    "section": "Simulación determinista",
                },
            )
            fragment_ids.append(fragment.fragment_id)
            block = f"[{fragment.fragment_id}] " + _render_result(nombre, result)
            if nombre != base_name:
                diff = escenario.ahorro_frente_a_base
                verb = "ahorra" if diff > 0 else "cuesta"
                block += (
                    f"\nFrente a «{base_name}»: {verb} {_money(abs(diff))}."
                    if diff
                    else f"\nFrente a «{base_name}»: sin diferencia."
                )
            blocks.append(block)
        if simulacion.datos_no_aportados:
            blocks.append(
                "DATOS NO APORTADOS (el cálculo los ha tomado como cero o sin informar): "
                + ", ".join(simulacion.datos_no_aportados)
                + ". Pídeselos al usuario si pueden cambiar el resultado, y dile que el cálculo es provisional sin ellos."
            )
        return ToolOutcome(
            content="\n\n".join(blocks),
            summary=f"{len(simulacion.escenarios)} cálculo(s): {', '.join(fragment_ids)}",
        )

    def _autonomo(self, arguments: dict[str, Any], modelo: Any, calcular: Any, titulo: str) -> ToolOutcome:
        return self._calculadora(arguments, modelo, calcular, titulo, ejercicios_autonomos_disponibles(), "autonomos")

    def _calculadora(
        self, arguments: dict[str, Any], modelo: Any, calcular: Any, titulo: str, ejercicios: list[int], ambito: str
    ) -> ToolOutcome:
        """Shared handler of the deterministic calculators: validate, compute, register as evidence."""
        datos = dict(arguments)
        if not isinstance(datos.get("ejercicio"), int) or isinstance(datos.get("ejercicio"), bool):
            case_year = parse_fiscal_year(self.case.tax_period) if self.case is not None else None
            datos["ejercicio"] = case_year if case_year in ejercicios else max(ejercicios)
        try:
            result = calcular(modelo(**datos))
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            return _error(f"Datos no válidos: {problems}")
        except ValueError as exc:  # unavailable year or community, inconsistent figures
            return _error(str(exc))
        lines = [f"{titulo} — ejercicio {datos['ejercicio']}"]
        for linea in getattr(result, "lineas", None) or []:
            ref = f" ({linea.referencia})" if linea.referencia else ""
            lines.append(f"  {linea.concepto}: {_money(linea.importe)}{ref}")
        resumen = {
            k: v
            for k, v in result.model_dump(mode="json").items()
            if k not in ("lineas", "avisos", "parametros_no_verificados", "fuente", "no_modelado")
            and v is not None
            and not isinstance(v, dict | list)
        }
        lines.append("Resultado: " + "; ".join(f"{k} = {v}" for k, v in resumen.items()))
        if getattr(result, "avisos", None):
            lines.append("Avisos (lo que este cálculo no cubre o da por supuesto):")
            lines.extend(f"  * {aviso}" for aviso in result.avisos)
        if getattr(result, "no_modelado", None):
            lines.append("No modelado: " + "; ".join(str(x) for x in result.no_modelado))
        if getattr(result, "fuente", None):
            lines.append(f"Parámetros: {result.fuente}.")
        fragment = self.ledger.register(
            text="\n".join(lines),
            metadata={
                "title": f"{titulo} {datos['ejercicio']}",
                "source": f"calculadora://{ambito}/{datos['ejercicio']}/{len(self.ledger) + 1}",
                "source_authority": "CALCULADORA",
                "section": "Cálculo determinista",
            },
        )
        return ToolOutcome(
            content=f"[{fragment.fragment_id}] " + "\n".join(lines), summary=f"{titulo}: {fragment.fragment_id}"
        )

    def _joint(self, arguments: dict[str, Any]) -> ToolOutcome:
        otro = arguments.get("conyuge_b")
        if not isinstance(otro, dict):
            return _error(
                "Falta 'conyuge_b' (objeto con los datos del otro cónyuge; puede ir vacío si no tiene ingresos)."
            )
        try:
            year, facts, datos = self._scenario_base(
                {"ejercicio": arguments.get("ejercicio"), "datos": arguments.get("conyuge_a")}
            )
            a, actividad_a = build_input_con_actividad(ejercicio=year, facts=facts, datos=datos)
            b, actividad_b = build_input_con_actividad(
                ejercicio=year, datos={"comunidad_autonoma": a.comunidad_autonoma, **otro}
            )
            try:
                comparacion = comparar_tributacion(a, b)
            except ValidationError as exc:
                # The joint return adds both spouses' figures: the sum can contradict the detail of
                # the children (a per-child expense given again by the other spouse) or a breakdown.
                problemas = "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
                raise DatosInvalidosError(
                    f"No se puede formar la declaración conjunta sumando los datos de los dos cónyuges: {problemas}. "
                    "Un gasto repartido por hijo en `descendientes` debe darse por hijo en los dos cónyuges (los mismos hijos) o solo en uno."
                ) from exc
        except (DatosInvalidosError, EjercicioNoDisponibleError) as exc:
            return _error(str(exc))
        # Where each spouse's activity income came from (computed from the breakdown or given).
        supuestos = [
            *comparacion.avisos,
            *(
                f"Cónyuge {letra}: {actividad.aviso}"
                for letra, actividad in (("A", actividad_a), ("B", actividad_b))
                if actividad is not None
            ),
        ]

        blocks: list[str] = []
        ids: list[str] = []
        for nombre, result in (
            ("Individual del cónyuge A", comparacion.individual_a),
            ("Individual del cónyuge B", comparacion.individual_b),
            ("Conjunta", comparacion.conjunta),
        ):
            fragment = self.ledger.register(
                text=_render_result(nombre, result),
                metadata={
                    "title": f"Cálculo IRPF {result.ejercicio} — {nombre}",
                    "source": f"calculadora://irpf/{result.ejercicio}/{len(self.ledger) + 1}",
                    "source_authority": "CALCULADORA",
                    "section": "Comparación de tributación individual y conjunta",
                },
            )
            ids.append(fragment.fragment_id)
            blocks.append(f"[{fragment.fragment_id}] " + _render_result(nombre, result))
        veredicto = (
            "Las dos opciones dan el mismo resultado."
            if comparacion.mejor == "indiferente"
            else f"Sale mejor la declaración {comparacion.mejor}: ahorra {_money(comparacion.ahorro)} "
            f"(individuales: {_money(comparacion.total_individual)}; conjunta: {_money(comparacion.total_conjunta)}; "
            "positivo = a pagar, negativo = a devolver)."
        )
        resumen = self.ledger.register(
            text=veredicto + "\nSupuestos: " + " ".join(supuestos),
            metadata={
                "title": f"Comparación individual / conjunta {year}",
                "source": f"calculadora://irpf/{year}/{len(self.ledger) + 1}",
                "source_authority": "CALCULADORA",
                "section": "Comparación de tributación individual y conjunta",
            },
        )
        blocks.insert(
            0, f"[{resumen.fragment_id}] {veredicto}\nSupuestos:\n" + "\n".join(f"  * {a}" for a in supuestos)
        )
        return ToolOutcome(
            content="\n\n".join(blocks), summary=f"mejor {comparacion.mejor}: {resumen.fragment_id}, {', '.join(ids)}"
        )

    def _optimize(self, arguments: dict[str, Any]) -> ToolOutcome:
        conyuge = arguments.get("conyuge")
        if conyuge is not None and not isinstance(conyuge, dict):
            return _error("'conyuge' debe ser un objeto {campo: valor} con las cifras del otro cónyuge.")
        restricciones = {k: arguments[k] for k in _RESTRICCIONES if arguments.get(k) is not None}
        try:
            year, facts, datos = self._scenario_base(arguments)
            plan = optimizar_desde_hechos(
                ejercicio=year,
                fecha=hoy_en_espana(),
                facts=facts,
                datos=datos,
                conyuge=conyuge,
                restricciones=restricciones,
            )
        except (DatosInvalidosError, EjercicioNoDisponibleError) as exc:
            return _error(str(exc))

        texto = _render_plan(plan)
        resumen = self.ledger.register(
            text=texto,
            metadata={
                "title": f"Plan óptimo IRPF {plan.ejercicio}",
                "source": f"calculadora://optimizador/{plan.ejercicio}/{len(self.ledger) + 1}",
                "source_authority": "CALCULADORA",
                "section": "Optimización determinista",
            },
        )
        blocks = [f"[{resumen.fragment_id}] {texto}"]
        ids: list[str] = []
        # The returns of both scenarios, citable like any other calculation.
        for escenario, hogar in (("Situación actual", plan.base), ("Plan óptimo", plan.optimo)):
            for declaracion in hogar.declaraciones:
                quien = {"contribuyente": "usuario", "conyuge": "cónyuge", "conjunta": "conjunta"}[declaracion.titular]
                nombre = f"{escenario} ({quien})"
                result = declaracion.resultado
                fragment = self.ledger.register(
                    text=_render_result(nombre, result),
                    metadata={
                        "title": f"Cálculo IRPF {result.ejercicio} — {nombre}",
                        "source": f"calculadora://irpf/{result.ejercicio}/{len(self.ledger) + 1}",
                        "source_authority": "CALCULADORA",
                        "section": "Optimización determinista",
                    },
                )
                ids.append(fragment.fragment_id)
                blocks.append(f"[{fragment.fragment_id}] " + _render_result(nombre, result))
        return ToolOutcome(
            content="\n\n".join(blocks),
            summary=f"ahorro {_money(plan.ahorro_total)} con {len(plan.palancas)} medida(s): "
            f"{resumen.fragment_id}, {', '.join(ids)}",
        )

    def _calendar(self, arguments: dict[str, Any]) -> ToolOutcome:
        raw_days = arguments.get("dias", 365)
        days = raw_days if isinstance(raw_days, int) and not isinstance(raw_days, bool) else 365
        days = max(1, min(730, days))
        facts = {fact.name: fact.value for fact in self.case.facts} if self.case is not None else {}
        today = datetime.now(UTC).date()
        vencimientos = proximos_vencimientos(facts, hoy=today, dias=days)
        if not vencimientos:
            return ToolOutcome(content="No hay vencimientos en ese plazo.", summary="0 vencimientos")
        lines = [
            f"- {v.fecha.strftime('%d/%m/%Y')} (en {v.dias_restantes} días): {v.titulo} — {v.periodo}"
            + (f", modelo {v.modelo}" if v.modelo else "")
            + f". {v.detalle} [{v.referencia}{'' if v.verificado else '; fecha no contrastada'}]"
            + (f" Nota: {v.nota}" if v.nota else "")
            for v in vencimientos
        ]
        text = (
            f"Calendario fiscal del usuario a {today.strftime('%d/%m/%Y')} (próximos {days} días)\n"
            + "\n".join(lines)
            + f"\n{aviso_festivos()}\nSolo se listan las obligaciones que constan en el expediente: si el usuario "
            "tiene actividad económica o bienes en el extranjero y no lo ha dicho, no aparecen."
        )
        # Registered as evidence so dates in the answer can be cited and verified.
        fragment = self.ledger.register(
            text=text,
            metadata={
                "title": f"Calendario fiscal a {today.strftime('%d/%m/%Y')}",
                "source": f"calendario://{today.isoformat()}/{days}",
                "source_authority": "CALCULADORA",
                "section": "Plazos de presentación",
            },
        )
        return ToolOutcome(
            content=f"[{fragment.fragment_id}] {text}",
            summary=f"{len(vencimientos)} vencimientos: {fragment.fragment_id}",
        )

    def _benefits(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            year, facts, datos = self._scenario_base(arguments)
            params = load_params(year)
            inp, actividad = build_input_con_actividad(ejercicio=year, facts=facts, datos=datos)
        except (DatosInvalidosError, EjercicioNoDisponibleError) as exc:
            return _error(str(exc))
        conocidos = {name for name in (*facts, *datos) if name in IRPFInput.model_fields or name == "estado_civil"}
        if inp.descendientes:
            # The counts of children are known through the detail of each child.
            conocidos.update(RECUENTOS_DEL_DETALLE)
        beneficios = evaluar_beneficios(inp, conocidos=conocidos, params=params)
        payload = {
            "ejercicio": year,
            "beneficios": [
                {
                    "nombre": b.nombre,
                    "estado": b.estado,
                    "detalle": b.detalle,
                    "ahorro_estimado_eur": float(b.ahorro_estimado) if b.ahorro_estimado is not None else None,
                    "requisitos": b.requisitos,
                    "referencia": b.referencia,
                    "parametros_verificados": b.verificado,
                    "datos_que_faltan": b.datos_que_faltan,
                }
                for b in beneficios
            ],
            "nota": (
                "Los ahorros estimados salen de la calculadora con los datos disponibles; para citarlos ante el "
                "usuario, calcula el escenario con calcular_irpf y cita ese fragmento. Las condiciones de cada "
                "beneficio deben contrastarse con buscar_fuentes antes de afirmarlas."
            ),
        }
        if actividad is not None:
            # The activity's net income was derived (or deliberately not): the agent must say so.
            payload["actividad_economica"] = actividad.aviso
        disponibles = sum(1 for b in beneficios if b.estado == "disponible")
        return ToolOutcome(
            content=json.dumps(payload, ensure_ascii=False),
            summary=f"{len(beneficios)} beneficios revisados, {disponibles} disponibles",
        )

    # ------------------------------------------------------------------ #
    # Doctrine validity
    # ------------------------------------------------------------------ #

    def _with_validity(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Stamp a retrieved chunk with its document's validity status."""
        if self._graph is None:
            return metadata
        doc = identify_document(metadata)
        if doc is None:
            return metadata
        status = self._graph.vigencia(doc.clave)
        if status.estado != "desconocido":
            metadata["_doc_key"] = status.clave
            metadata["_vigencia_estado"] = status.estado
            metadata["_vigencia_resumen"] = status.resumen
        return metadata

    def _validity_alerts(self, fragments: list[Fragment]) -> str:
        """Warn the model about superseded/disputed doctrine among the results,
        and about results that disagree with each other."""
        if self._graph is None:
            return ""
        lines: list[str] = []
        for fragment in fragments:
            if fragment.vigencia in ("superada", "discutida", "matizada"):
                lines.append(f"- [{fragment.fragment_id}] {fragment.title}: {fragment.vigencia_resumen}")
        by_key = {fragment.doc_key: fragment for fragment in fragments if fragment.doc_key}
        for key, fragment in by_key.items():
            for arista in self._graph.salientes(key):
                other = by_key.get(arista.destino)
                if other is not None and arista.relacion in ("supera", "contradice", "matiza"):
                    lines.append(
                        f"- [{fragment.fragment_id}] {_RELACION_VERBO[arista.relacion].replace('la ', '')} a "
                        f"[{other.fragment_id}] (según el grafo de citas)."
                    )
        if not lines:
            return ""
        return (
            "ALERTAS DE VIGENCIA (indicios del grafo de citas entre resoluciones; compruébalos leyendo la resolución "
            "posterior antes de afirmarlos):\n" + "\n".join(dict.fromkeys(lines))
        )

    def _validity(self, arguments: dict[str, Any]) -> ToolOutcome:
        assert self._graph is not None
        reference = arguments.get("referencia")
        if not isinstance(reference, str) or not reference.strip():
            return _error("Falta 'referencia' (la resolución tal como se cita).")
        citas = extract_citations(reference)
        if not citas:
            return _error(
                "No reconozco esa referencia. Usa el número de consulta (V1234-19), el RG del TEAC (00/1234/2017), "
                "o 'STS … (rec. 1234/2019)' / 'STC 182/2021'."
            )
        blocks: list[str] = []
        for cita in citas[:3]:
            nodo = self._graph.get(cita.clave)
            if nodo is None:
                blocks.append(
                    f"{cita.texto}: no consta en el grafo (ni está en el corpus ni la cita ningún documento)."
                )
                continue
            status = self._graph.vigencia(nodo.clave)
            lines = [
                f"{self._graph.etiqueta(nodo)} — estado: {status.estado.upper()}. {status.resumen}"
                + (
                    ""
                    if nodo.en_corpus
                    else " (No está en el corpus: solo se conoce por las citas de otros documentos.)"
                )
            ]
            for arista in self._graph.entrantes(nodo.clave):
                otro = self._graph.get(arista.origen)
                if otro is not None:
                    evidencia = f" — «{arista.evidencia}»" if arista.evidencia and arista.relacion != "cita" else ""
                    lines.append(f"  · {self._graph.etiqueta(otro)} {_RELACION_VERBO[arista.relacion]}{evidencia}")
            for arista in self._graph.salientes(nodo.clave):
                otro = self._graph.get(arista.destino)
                if otro is not None and arista.relacion != "cita":
                    lines.append(f"  · {arista.relacion} a {self._graph.etiqueta(otro)}")
            blocks.append("\n".join(lines))
        blocks.append(
            "Esto es un indicio, no una fuente citable: para afirmar ante el usuario que un criterio está superado o "
            "discutido, localiza la resolución posterior con buscar_fuentes y cítala."
        )
        return ToolOutcome(content="\n\n".join(blocks), summary=f"vigencia de {len(citas[:3])} referencia(s)")

    def _case_snapshot(self) -> dict[str, Any]:
        case = self.case
        assert case is not None
        return {
            "ejercicio": case.tax_period,
            "jurisdiccion": case.jurisdiction,
            "hechos": {fact.name: fact.value for fact in case.facts},
            "datos_pendientes": [missing.fact_name for missing in case.missing_facts],
            "obligaciones_candidatas": [
                {
                    "id": obligation.obligation_id,
                    "titulo": obligation.title,
                    "estado": obligation.status.value,
                    "faltan_para_confirmar": obligation.blocking_missing_facts,
                }
                for obligation in case.obligation_candidates
            ],
        }

    def _read_case(self, arguments: dict[str, Any]) -> ToolOutcome:
        del arguments
        snapshot = self._case_snapshot()
        return ToolOutcome(
            content=json.dumps(snapshot, ensure_ascii=False, default=str),
            summary=f"{len(snapshot['hechos'])} hechos conocidos",
        )

    def _record_facts(self, arguments: dict[str, Any]) -> ToolOutcome:
        assert self.case is not None
        raw_facts = arguments.get("hechos")
        if not isinstance(raw_facts, list) or not raw_facts:
            return _error("Falta 'hechos' (lista no vacía de {nombre, valor}).")

        accepted: list[Fact] = []
        rejected: list[str] = []
        for item in raw_facts:
            name = item.get("nombre") if isinstance(item, dict) else None
            value = item.get("valor") if isinstance(item, dict) else None
            expected = EXPECTED_FACT_TYPE.get(name) if isinstance(name, str) else None
            if not isinstance(name, str) or expected is None:
                rejected.append(f"{name!r}: nombre de hecho desconocido")
                continue
            if name == DESCENDIENTES:
                # Structured fact: checked child by child and stored as canonical JSON text.
                try:
                    value = descendientes_a_texto(value)
                except DatosInvalidosError as exc:
                    rejected.append(f"{name}: {exc}")
                    continue
            coerced = _coerce_value(
                expected=expected,
                value_string=value if isinstance(value, str) else None,
                value_number=value if isinstance(value, int | float) and not isinstance(value, bool) else None,
                value_boolean=value if isinstance(value, bool) else None,
            )
            if coerced is None:
                rejected.append(f"{name}: valor {value!r} no es de tipo {expected.value}")
                continue
            accepted.append(
                Fact(
                    fact_id=f"fact_{name}",
                    name=name,
                    value=coerced,
                    value_type=expected,
                    source="user_statement_via_agent",
                    confidence=0.9,
                )
            )

        if accepted:
            merged = _merge_facts(accepted, self.case)
            known = {fact.name for fact in merged}
            update: dict[str, Any] = {
                "facts": merged,
                # CaseState forbids a fact being both known and missing.
                "missing_facts": [m for m in self.case.missing_facts if m.fact_name not in known],
                "updated_at": datetime.now(UTC),
            }
            period = next((fact for fact in accepted if fact.name == "periodo_fiscal"), None)
            if period is not None:
                update["tax_period"] = str(period.value)
            self.case = self.case.model_copy(update=update)
            self.case_changed = True

        lines = [f"Registrados: {', '.join(f'{fact.name}={fact.value}' for fact in accepted) or 'ninguno'}."]
        if rejected:
            lines.append("Rechazados: " + "; ".join(rejected) + ".")
        return ToolOutcome(
            content=" ".join(lines),
            summary=f"{len(accepted)} registrados, {len(rejected)} rechazados",
            is_error=not accepted,
        )

    def _evaluate_rules(self, arguments: dict[str, Any]) -> ToolOutcome:
        del arguments
        assert self.case is not None
        result = evaluate_rules(case_state=self.case, recent_facts=[])
        if list(result.candidate_obligations) != list(self.case.obligation_candidates):
            self.case = self.case.model_copy(
                update={"obligation_candidates": result.candidate_obligations, "updated_at": datetime.now(UTC)}
            )
            self.case_changed = True
        payload = {
            "ejercicio_evaluado": result.fiscal_year,
            "obligaciones_candidatas": [
                {
                    "id": obligation.obligation_id,
                    "titulo": obligation.title,
                    "descripcion": obligation.description,
                    "riesgo": obligation.risk_level.value,
                    "fuentes_de_la_regla": [f"{ref.title} ({ref.locator})" for ref in obligation.evidence_refs],
                }
                for obligation in result.candidate_obligations
            ],
            "reglas_no_activadas": [
                {"regla": trace.rule_id, "hechos_que_faltan": trace.missing_facts}
                for trace in result.rule_traces
                if not trace.matched
            ],
            "aviso": "Catálogo de reglas parcial: la ausencia de una obligación no implica que no exista.",
        }
        return ToolOutcome(
            content=json.dumps(payload, ensure_ascii=False, default=str),
            summary=f"{len(result.candidate_obligations)} obligaciones candidatas",
        )
