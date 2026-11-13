// Pure helpers for mapping backend fields to presentation values.
// Kept separate from components so they can be unit-tested in isolation.

import type { AnswerMode, Citation, ClaimCheck, ClaimVerdict, Fact, ProgressEvent, RiskLevel } from "../api";
import { DATOS_CONYUGE, PREFERENCIAS_OPTIMIZACION, preferencesSummary, spouseSummary } from "./expediente";

export type SourceKind = "boe" | "manual" | "dgt" | "court" | "calc" | "other";

/** Pick an icon family for a citation: the agent's explicit authority first,
 *  URL/title sniffing as a fallback for legacy /qa citations. */
export function classifySource(c: Citation): SourceKind {
  const authority = (c.authority || "").toUpperCase();
  if (authority === "CALCULADORA") return "calc";
  if (authority === "DGT") return "dgt";
  if (authority === "TEAC" || authority === "TEAR" || authority.startsWith("TS") || authority === "TC") return "court";
  if (authority === "AEAT") return "manual";
  if (authority === "BOE" || authority.startsWith("BOLETIN")) return "boe";
  const loc = (c.locator || "").toLowerCase();
  const title = (c.title || "").toLowerCase();
  if (loc.startsWith("boe://") || loc.includes("boe.es")) return "boe";
  if (loc.startsWith("aeat://") || loc.includes("agenciatributaria")) return "manual";
  if (loc.startsWith("dgt://") || loc.includes("petete.tributos") || /\bv\d{4}-\d{2}\b/i.test(title)) return "dgt";
  if (loc.startsWith("teac://") || title.includes("teac")) return "court";
  return "other";
}

const AUTHORITY_LABEL: Record<string, string> = {
  BOE: "Boletín Oficial del Estado",
  DGT: "Dirección General de Tributos",
  TEAC: "Tribunal Económico-Administrativo Central",
  TEAR: "Tribunal Económico-Administrativo Regional",
  TC: "Tribunal Constitucional",
  TS: "Tribunal Supremo",
  TS_SALA_TERCERA: "Tribunal Supremo · Sala Tercera",
  AEAT: "Agencia Tributaria",
  CALCULADORA: "Calculadora de HaciendaGPT · parámetros de la Ley del IRPF",
};

export function authorityLabel(authority: string | null | undefined): string | null {
  if (!authority) return null;
  if (authority.toUpperCase().startsWith("BOLETIN")) return "Boletín autonómico";
  return AUTHORITY_LABEL[authority.toUpperCase()] ?? authority;
}

export function isHttpUrl(value: string | null | undefined): value is string {
  return !!value && (value.startsWith("http://") || value.startsWith("https://"));
}

/** "boe.es" from a full URL, for a compact "Abrir en boe.es" link. */
export function hostLabel(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "la fuente";
  }
}

const DATE_FMT = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "short", year: "numeric" });

/** "2025-01-20" → "20 ene 2025". Returns the input untouched if it is not a date. */
export function formatDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!match) return iso;
  // Build in local time from the parts: `new Date("2026-06-30")` is UTC and can
  // render as the previous day in negative-offset time zones.
  return DATE_FMT.format(new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
}

/** Whole days from today until an ISO date (negative when past). */
export function daysUntil(iso: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!match) return null;
  const target = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((target.getTime() - today.getTime()) / 86_400_000);
}

export function relativeTime(timestamp: number): string {
  const minutes = Math.round((Date.now() - timestamp) / 60_000);
  if (minutes < 1) return "ahora";
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `hace ${hours} h`;
  const days = Math.round(hours / 24);
  if (days < 7) return days === 1 ? "ayer" : `hace ${days} días`;
  return DATE_FMT.format(new Date(timestamp));
}

export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} s`;
  return `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, "0")} s`;
}

/** Map RiskLevel -> visual bucket (CRITICAL collapses onto HIGH). */
export function riskBucket(r: RiskLevel): "high" | "medium" | "low" {
  if (r === "critical" || r === "high") return "high";
  if (r === "medium") return "medium";
  return "low";
}

export const RISK_LABEL: Record<"high" | "medium" | "low", string> = {
  high: "Riesgo alto",
  medium: "Riesgo medio",
  low: "Riesgo bajo",
};

export interface TrustCopy {
  cls: "cited" | "uncited" | "abstain";
  label: string;
  short: string;
}
export const TRUST: Record<AnswerMode, TrustCopy> = {
  verified: {
    cls: "cited",
    label: "Verificada contra sus fuentes",
    short: "Cada afirmación normativa se ha comprobado con el texto de la fuente que cita.",
  },
  partially_verified: {
    cls: "uncited",
    label: "Verificada solo en parte",
    short: "Alguna afirmación no queda respaldada por la fuente citada. Mira el detalle antes de fiarte.",
  },
  unverified: {
    cls: "uncited",
    label: "Orientativa · sin verificar",
    short: "No se ha podido comprobar contra las fuentes; tómala como una orientación general.",
  },
  clarification: {
    cls: "abstain",
    label: "Necesito un dato más",
    short: "La respuesta depende de información que todavía no tengo.",
  },
  cited: {
    cls: "cited",
    label: "Respuesta con fuentes",
    short: "Se apoya en las fuentes listadas.",
  },
  uncited: {
    cls: "uncited",
    label: "Orientativa · sin fuentes",
    short: "Trátala como una orientación y verifícala.",
  },
  abstained: {
    cls: "abstain",
    label: "Sin base normativa suficiente",
    short: "Prefiero no responder antes que darte un dato sin fuente.",
  },
};

export const VERDICT_LABEL: Record<ClaimVerdict, string> = {
  respaldada: "Respaldada por la fuente",
  parcial: "Respaldada solo en parte",
  no_respaldada: "Sin respaldo en la fuente",
};

// ---- Case facts ------------------------------------------------------------

const FACT_LABEL: Record<string, string> = {
  residencia_fiscal: "Residencia fiscal",
  periodo_fiscal: "Ejercicio",
  tipo_renta: "Tipo de renta",
  menciona_ingresos: "Tiene ingresos",
  importe_renta_aproximado: "Ingresos anuales aproximados",
  categoria_contribuyente: "Situación laboral",
  regimen_tributario: "Régimen de tributación",
  volumen_facturacion: "Facturación anual",
  actividad_economica: "Actividad económica",
  plantilla: "Empleados",
  alta_actividad_economica: "De alta en actividad económica",
  fecha_inicio_actividad: "Inicio de la actividad",
  regimen_cotizacion: "Régimen de cotización",
  periodicidad_iva: "Periodicidad del IVA",
  tema_tributario: "Tema de la consulta",
  comunidad_autonoma: "Comunidad autónoma",
  estado_civil: "Estado civil",
  edad: "Edad",
  grado_discapacidad: "Grado de discapacidad (%)",
  necesita_asistencia: "Necesitas ayuda de terceras personas o tienes movilidad reducida",
  num_descendientes: "Hijos a cargo",
  num_descendientes_menores_3: "Hijos menores de 3 años",
  num_ascendientes_mayores_65: "Ascendientes a cargo mayores de 65",
  num_ascendientes_mayores_75: "Ascendientes a cargo mayores de 75",
  num_descendientes_discapacidad: "Hijos a cargo con discapacidad (33 % o más)",
  num_descendientes_discapacidad_65: "Hijos a cargo con discapacidad del 65 % o más",
  num_descendientes_asistencia: "Hijos con discapacidad que necesitan ayuda de terceros o tienen movilidad reducida",
  num_ascendientes_discapacidad: "Ascendientes a cargo con discapacidad (33 % o más)",
  num_ascendientes_discapacidad_65: "Ascendientes a cargo con discapacidad del 65 % o más",
  num_ascendientes_asistencia: "Ascendientes con discapacidad que necesitan ayuda de terceros o tienen movilidad reducida",
  num_ascendientes_discapacidad_hasta_65: "Ascendientes con discapacidad de 65 años o menos",
  num_declaraciones_mismos_ascendientes: "Declaraciones que aplican el mínimo por los mismos ascendientes",
  rendimiento_trabajo_bruto: "Salario bruto anual",
  cotizaciones_seguridad_social: "Cotizaciones a la Seguridad Social",
  retenciones: "IRPF retenido en el año",
  rendimiento_actividad_neto: "Beneficio neto de la actividad",
  rendimiento_alquiler_neto: "Rendimiento neto de alquileres (ya reducido)",
  rendimiento_alquiler_vivienda_sin_reducir: "Alquiler de vivienda: neto antes de la reducción",
  arrendamiento_fecha_contrato: "Fecha del contrato de alquiler",
  arrendamiento_gran_tenedor: "Arrendador gran tenedor",
  arrendamiento_primer_alquiler: "Primera vez que se alquila la vivienda",
  arrendamiento_variacion_renta: "Variación de la renta sobre el contrato anterior (%)",
  arrendamiento_mismo_inquilino: "Nuevo contrato con el mismo inquilino",
  arrendamiento_renta_bajo_indice: "Renta por debajo del índice de referencia",
  arrendamiento_zona_tensionada: "Vivienda en zona tensionada",
  arrendamiento_inquilino_joven: "Inquilino de 18 a 35 años",
  arrendamiento_alquiler_social: "Alquiler social o de renta limitada",
  arrendamiento_vivienda_rehabilitada: "Vivienda rehabilitada (dos años anteriores)",
  arrendamiento_prorroga_tacita: "Contrato en prórroga tácita",
  rendimiento_capital_mobiliario: "Intereses y dividendos",
  ganancias_patrimoniales: "Ganancias patrimoniales",
  rendimientos_negativos_pendientes: "Rendimientos negativos de años anteriores",
  perdidas_patrimoniales_pendientes: "Pérdidas patrimoniales de años anteriores",
  aportacion_plan_pensiones: "Aportación a plan de pensiones",
  contribucion_empresa_plan_pensiones: "Aportación de la empresa al plan",
  donativos: "Donativos",
  pagos_vivienda_habitual_pre2013: "Pagos de hipoteca (vivienda anterior a 2013)",
  gastos_guarderia: "Gastos de guardería",
  bienes_en_el_extranjero: "Bienes o cuentas en el extranjero",
  alquiler_vivienda_habitual: "Alquiler pagado por tu vivienda",
  gastos_escolaridad: "Gastos de escolaridad",
  gastos_ensenanza_idiomas: "Clases de idiomas de los hijos",
  gastos_uniformes_escolares: "Uniformes escolares",
  cotizaciones_empleada_hogar: "Cotizaciones de empleada de hogar",
  hijos_nacidos_ultimos_3_ejercicios: "Hijos nacidos en los últimos 3 años",
  donativos_fundaciones_madrid: "Donativos a fundaciones de Madrid",
  base_imponible_resto_unidad_familiar: "Rentas del resto de la unidad familiar",
  miembros_unidad_familiar: "Miembros de la unidad familiar",
  familia_numerosa: "Familia numerosa",
  familia_numerosa_especial: "Familia numerosa de categoría especial",
  familia_monoparental: "Familia monoparental",
  hijos_nacidos_en_el_ejercicio: "Hijos nacidos o adoptados este año",
  gastos_libros_texto: "Libros de texto y material escolar",
  gastos_cuidado_familiares: "Gastos de cuidado de familiares",
  num_familiares_discapacidad_a_cargo: "Familiares con discapacidad a cargo (total)",
  menores_acogidos: "Menores en acogimiento familiar",
  hijos_adoptados_en_el_ejercicio: "Hijos adoptados este año",
  donativos_autonomicos: "Donativos a entidades de tu comunidad",
  inversion_vivienda_habitual: "Compra o rehabilitación de vivienda habitual",
  inversion_empresas_nueva_creacion: "Inversión en empresas nuevas",
  gastos_eficiencia_energetica: "Obras de mejora energética en tu vivienda (total)",
  gastos_salud: "Gastos de salud (total)",
  gastos_deporte_cultura: "Gastos de deporte y cultura (total)",
  viudedad: "Viudo o viuda",
  vivienda_en_municipio_rural: "Vivienda comprada en municipio rural",
  municipio_rural_o_despoblacion: "Municipio rural o en riesgo de despoblación",
  num_hijos_gastos_educativos: "Hijos escolarizados con gastos educativos",
  num_hijos_primer_ciclo_infantil: "Hijos en primer ciclo de infantil (0-3)",
  descendientes: "Detalle de cada hijo",
  datos_conyuge: "Cifras de tu cónyuge",
  preferencias_optimizacion: "Preferencias del optimizador",
  edad_conyuge: "Edad de tu cónyuge",
  grado_discapacidad_conyuge: "Grado de discapacidad de tu cónyuge (%)",
  necesita_asistencia_conyuge: "Tu cónyuge necesita ayuda de terceras personas o tiene movilidad reducida",
  num_familiares_discapacidad_65_a_cargo: "Familiares a cargo con discapacidad del 65 % o más (total)",
  num_personas_acogidas_mayores_o_discapacidad: "Mayores o personas con discapacidad acogidos sin contraprestación",
  habitantes_municipio: "Habitantes de tu municipio",
  dias_alquiler_vivienda_habitual: "Días del año con el alquiler en vigor",
  dias_acogimiento: "Días del año de convivencia con los menores acogidos",
  dias_desempleo: "Días del año en desempleo",
  num_declaraciones_mismo_alquiler: "Declaraciones que deducen por el mismo alquiler",
  num_declaraciones_misma_vivienda: "Declaraciones que deducen por la misma vivienda",
  progenitores_trabajan: "Los dos progenitores trabajan",
  victima_violencia_genero: "Víctima de violencia de género",
  victima_terrorismo: "Víctima del terrorismo",
  vivienda_protegida: "Vivienda protegida",
  vivienda_nueva_construccion: "Vivienda de nueva construcción",
  primera_vivienda: "Primera vivienda en propiedad",
  ejercicio_viudedad: "Año en que enviudaste",
  ejercicio_adquisicion_vivienda: "Año de compra de la vivienda habitual",
  ejercicio_traslado_residencia: "Año del traslado de residencia",
  gastos_instalaciones_renovables: "Instalaciones de energía renovable o autoconsumo",
  gastos_deporte: "Gastos de deporte",
  gastos_cultura: "Gastos de cultura",
  gastos_honorarios_sanitarios: "Honorarios de profesionales sanitarios",
  gastos_salud_bucodental: "Dentista y salud bucodental",
  gastos_salud_mental: "Salud mental (psicología, psiquiatría)",
  gastos_optica: "Gafas, lentillas y prótesis",
  gastos_enfermedades_raras: "Gastos por enfermedades raras",
  gastos_ela: "Gastos por ELA",
  pagos_adquisicion_vivienda: "Compra de la vivienda habitual (sin intereses)",
  pagos_rehabilitacion_vivienda: "Rehabilitación de la vivienda habitual (sin intereses)",
  intereses_prestamo_vivienda: "Intereses del préstamo de la vivienda",
  donativos_medio_ambiente: "Donativos a medio ambiente",
  donativos_cultura_patrimonio: "Donativos a cultura y patrimonio",
  donativos_investigacion: "Donativos a investigación y universidades",
  donativos_cooperacion_fines_sociales: "Donativos a cooperación y fines sociales",
  donativos_lengua_propia: "Donativos al fomento de la lengua propia",
  donativos_fundaciones_autonomicas: "Donativos a fundaciones de tu comunidad",
  gastos_veterinarios: "Gastos veterinarios",
  adquisicion_vehiculo_electrico: "Compra de vehículo eléctrico",
  inversion_economia_social: "Inversión en entidades de economía social",
};

/** Educational stages of a child (`descendientes[].etapa_educativa`), in school order. */
export const ETAPA_LABEL: Record<string, string> = {
  infantil_primer_ciclo: "Infantil, primer ciclo (0-3)",
  infantil_segundo_ciclo: "Infantil, segundo ciclo (3-6)",
  primaria: "Primaria",
  eso: "ESO",
  educacion_especial: "Educación especial",
  bachillerato: "Bachillerato",
  fp_basica: "FP básica",
  fp_grado_medio: "FP de grado medio",
  fp_grado_superior: "FP de grado superior",
  universidad: "Universidad",
};

// Enum values the backend stores as ASCII slugs.
const VALUE_LABEL: Record<string, string> = {
  actividad_economica: "Actividad económica",
  capital_mobiliario: "Capital mobiliario (intereses, dividendos)",
  capital_inmobiliario: "Capital inmobiliario (alquileres)",
  ganancias_patrimoniales: "Ganancias patrimoniales",
  autonomo: "Autónomo",
  estimacion_directa_normal: "Estimación directa normal",
  estimacion_directa_simplificada: "Estimación directa simplificada",
  estimacion_objetiva: "Estimación objetiva (módulos)",
  modulos: "Estimación objetiva (módulos)",
  pareja_de_hecho: "Pareja de hecho",
};

const MONEY_FACTS = new Set([
  "importe_renta_aproximado",
  "volumen_facturacion",
  "rendimiento_trabajo_bruto",
  "cotizaciones_seguridad_social",
  "retenciones",
  "rendimiento_actividad_neto",
  "rendimiento_alquiler_neto",
  "rendimiento_capital_mobiliario",
  "ganancias_patrimoniales",
  "aportacion_plan_pensiones",
  "contribucion_empresa_plan_pensiones",
  "donativos",
  "pagos_vivienda_habitual_pre2013",
  "gastos_guarderia",
  "alquiler_vivienda_habitual",
  "gastos_escolaridad",
  "gastos_ensenanza_idiomas",
  "gastos_uniformes_escolares",
  "cotizaciones_empleada_hogar",
  "donativos_fundaciones_madrid",
  "base_imponible_resto_unidad_familiar",
  "gastos_libros_texto",
  "gastos_cuidado_familiares",
  "donativos_autonomicos",
  "inversion_vivienda_habitual",
  "inversion_empresas_nueva_creacion",
  "gastos_eficiencia_energetica",
  "gastos_salud",
  "gastos_deporte_cultura",
  "gastos_instalaciones_renovables",
  "gastos_deporte",
  "gastos_cultura",
  "gastos_honorarios_sanitarios",
  "gastos_salud_bucodental",
  "gastos_salud_mental",
  "gastos_optica",
  "gastos_enfermedades_raras",
  "gastos_ela",
  "pagos_adquisicion_vivienda",
  "pagos_rehabilitacion_vivienda",
  "intereses_prestamo_vivienda",
  "donativos_medio_ambiente",
  "donativos_cultura_patrimonio",
  "donativos_investigacion",
  "donativos_cooperacion_fines_sociales",
  "donativos_lengua_propia",
  "donativos_fundaciones_autonomicas",
  "gastos_veterinarios",
  "adquisicion_vehiculo_electrico",
  "inversion_economia_social",
]);
// Cents are kept when present: showing 6.502,57 € as "6503 €" would misstate the figure.
const EUR = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR", minimumFractionDigits: 0, maximumFractionDigits: 2 });

/** "tipo_renta" → "Tipo de renta"; unknown names are de-snake-cased. */
export function factLabel(name: string): string {
  const known = FACT_LABEL[name];
  if (known) return known;
  const spaced = name.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** The `descendientes` fact holds the JSON text of a list (one object per child): say how
 *  many there are rather than showing the JSON. It is edited in the simulator. */
function childrenSummary(value: Fact["value"]): string {
  let list: unknown = value;
  if (typeof value === "string") {
    try {
      list = JSON.parse(value);
    } catch {
      return "No se puede leer · vuelve a guardarlo desde el simulador";
    }
  }
  if (!Array.isArray(list)) return "No se puede leer · vuelve a guardarlo desde el simulador";
  if (list.length === 0) return "Sin detalle";
  return `${list.length} ${list.length === 1 ? "hijo" : "hijos"} · se edita en el simulador`;
}

export function factValue(fact: Pick<Fact, "name" | "value">): string {
  const { name, value } = fact;
  if (typeof value === "boolean") return value ? "Sí" : "No";
  if (typeof value === "number") return MONEY_FACTS.has(name) ? EUR.format(value) : String(value);
  if (name === "descendientes") return childrenSummary(value);
  if (name === DATOS_CONYUGE) return spouseSummary(value);
  if (name === PREFERENCIAS_OPTIMIZACION) return preferencesSummary(value);
  if (typeof value === "string") {
    if (name === "residencia_fiscal") return value === "ES" ? "España" : value === "OTHER" ? "Fuera de España" : value;
    const known = VALUE_LABEL[value];
    if (known) return known;
    const spaced = value.replace(/_/g, " ");
    return spaced.charAt(0).toUpperCase() + spaced.slice(1);
  }
  return JSON.stringify(value);
}

export type FactInput =
  | { kind: "choice"; options: Array<{ value: string | boolean; label: string }> }
  | { kind: "money" }
  | { kind: "number" }
  | { kind: "year" }
  | { kind: "text" };

const choice = (values: string[]): FactInput => ({
  kind: "choice",
  options: values.map((value) => ({ value, label: factValue({ name: "", value }) })),
});

const FACT_INPUT: Record<string, FactInput> = {
  residencia_fiscal: {
    kind: "choice",
    options: [
      { value: "ES", label: "España" },
      { value: "OTHER", label: "Fuera de España" },
    ],
  },
  periodo_fiscal: { kind: "year" },
  tipo_renta: choice(["trabajo", "actividad_economica", "capital_mobiliario", "capital_inmobiliario", "ganancias_patrimoniales", "otros"]),
  categoria_contribuyente: choice(["asalariado", "autonomo", "pensionista", "sociedad", "estudiante", "desempleado"]),
  regimen_tributario: choice(["estimacion_directa_normal", "estimacion_directa_simplificada", "estimacion_objetiva"]),
  periodicidad_iva: choice(["trimestral", "mensual"]),
  estado_civil: choice(["soltero", "casado", "pareja_de_hecho", "separado", "divorciado", "viudo"]),
  plantilla: { kind: "number" },
};

const YES_NO: FactInput = {
  kind: "choice",
  options: [
    { value: true, label: "Sí" },
    { value: false, label: "No" },
  ],
};

/** Which control edits a fact: a fixed list where the values are an enum,
 *  a number box for amounts, free text otherwise. */
export function factInput(fact: Pick<Fact, "name" | "value">): FactInput {
  if (typeof fact.value === "boolean") return YES_NO;
  if (MONEY_FACTS.has(fact.name)) return { kind: "money" };
  const known = FACT_INPUT[fact.name];
  if (known) {
    // A stored value outside the list (e.g. a country code) must stay editable as text.
    if (known.kind === "choice" && !known.options.some((o) => o.value === fact.value)) return { kind: "text" };
    return known;
  }
  return typeof fact.value === "number" ? { kind: "number" } : { kind: "text" };
}

/** "Aún me falta: tipo de renta y ejercicio" from raw fact names. */
export function missingFactsSentence(missing: string[]): string {
  if (missing.length === 0) return "";
  const labels = missing.map((m) => factLabel(m).toLowerCase());
  const joined = labels.length === 1 ? labels[0] : `${labels.slice(0, -1).join(", ")} y ${labels[labels.length - 1]}`;
  return `Falta por saber: ${joined}.`;
}

// ---- Agent activity --------------------------------------------------------

const SOURCE_TYPE_LABEL: Record<string, string> = {
  normativa: "leyes y reglamentos",
  jurisprudencia: "jurisprudencia",
  doctrina_administrativa: "doctrina de la DGT y el TEAC",
  aeat: "manuales de la AEAT",
};

/** One persistent row of the "what the advisor did" timeline. */
export interface ActivityRow {
  kind: "search" | "case" | "calc" | "team" | "verify" | "revise";
  text: string;
  detail?: string;
  outcome?: string;
  failed?: boolean;
}

function searchDetail(args: Record<string, unknown>): string | undefined {
  const parts: string[] = [];
  if (typeof args.tipo_fuente === "string") parts.push(SOURCE_TYPE_LABEL[args.tipo_fuente] ?? args.tipo_fuente);
  if (typeof args.tributo === "string") parts.push(args.tributo);
  if (typeof args.ambito === "string") parts.push(args.ambito.replace("autonomico:", "autonómico: "));
  if (args.solo_vinculante === true) parts.push("solo vinculante");
  if (typeof args.desde_fecha === "string") parts.push(`desde ${args.desde_fecha}`);
  return parts.length ? `Solo en ${parts.join(" · ")}` : undefined;
}

const TEAM_MEMBER: Record<string, string> = {
  investigador_normativa: "Investigador de normativa",
  investigador_doctrina: "Investigador de doctrina y jurisprudencia",
  critico: "Revisor adversarial",
  planificador: "Planificador",
};

/** Fold raw progress events into human-readable rows (tool results attach to
 *  the call they answer; "thinking" is transient and produces no row). */
export function activityRows(events: ProgressEvent[]): ActivityRow[] {
  const rows: ActivityRow[] = [];
  for (const event of events) {
    if (event.type === "team") {
      if (event.stage === "planning" && event.status === "done" && event.complex) {
        rows.push({ kind: "team", text: "Consulta compleja: la reparto entre dos investigadores y un revisor" });
      } else if (event.stage === "escalation") {
        rows.push({ kind: "team", text: "Las fuentes discrepan: pido una segunda investigación al equipo" });
      } else if (event.stage === "critic" && event.status === "start") {
        rows.push({ kind: "team", text: "Revisión adversarial del borrador, como lo haría un inspector" });
      } else if (event.stage === "specialist" && event.status === "done") {
        const last = [...rows].reverse().find((r) => r.kind === "team" && r.outcome === undefined && r.text.startsWith("Consulta al especialista"));
        if (last) last.outcome = "informe recibido";
      } else if (event.stage === "critic" && event.status === "done") {
        const last = rows[rows.length - 1];
        const target = last && last.kind === "team" && last.outcome === undefined ? last : rows.find((r) => r.kind === "team" && r.text.startsWith("Revisión adversarial") && r.outcome === undefined);
        if (target) target.outcome = event.objections ? "con objeciones: reescribo la respuesta" : "sin objeciones";
      }
    } else if (event.type === "tool_call") {
      if (event.tool.startsWith("consultar_") && !["consultar_expediente", "consultar_vigencia"].includes(event.tool)) {
        const area = event.tool.slice("consultar_".length).replace(/_/g, " ");
        const pregunta = typeof event.arguments.pregunta === "string" ? event.arguments.pregunta : undefined;
        rows.push({ kind: "team", text: `Consulta al especialista en ${area}`, detail: pregunta });
      } else if (event.agent?.startsWith("especialista_")) {
        const area = event.agent.slice("especialista_".length).replace(/_/g, " ");
        rows.push({ kind: "calc", text: event.tool === "leer_expediente" ? `Especialista en ${area}: repasa tus datos` : `Especialista en ${area}: ${event.tool.replace(/_/g, " ")}` });
      } else if (event.tool === "buscar_fuentes") {
        const query = typeof event.arguments.consulta === "string" ? event.arguments.consulta : "…";
        const who = event.agent ? TEAM_MEMBER[event.agent] : undefined;
        const filters = searchDetail(event.arguments);
        rows.push({ kind: "search", text: query, detail: [who, filters].filter(Boolean).join(" · ") || undefined });
      } else if (event.tool === "registrar_hechos") {
        const facts = Array.isArray(event.arguments.hechos) ? event.arguments.hechos : [];
        const names = facts
          .map((f) => (typeof f === "object" && f !== null && "nombre" in f ? factLabel(String(f.nombre)).toLowerCase() : ""))
          .filter(Boolean);
        rows.push({ kind: "case", text: `Expediente: anotado${names.length ? ` ${names.join(", ")}` : " un dato nuevo"}` });
      } else if (event.tool === "consultar_expediente") {
        rows.push({ kind: "case", text: "Expediente: repaso de lo que ya sé de tu situación" });
      } else if (event.tool === "evaluar_obligaciones") {
        rows.push({ kind: "case", text: "Obligaciones: comprobación de las que te aplican" });
      } else if (event.tool === "calcular_irpf") {
        const escenarios = Array.isArray(event.arguments.escenarios) ? event.arguments.escenarios.length : 1;
        rows.push({ kind: "calc", text: escenarios > 1 ? `Cálculo del IRPF: ${escenarios} escenarios comparados` : "Cálculo del IRPF" });
      } else if (event.tool === "listar_beneficios_fiscales") {
        rows.push({ kind: "calc", text: "Revisión de reducciones y deducciones a tu alcance" });
      } else if (event.tool === "calendario_fiscal") {
        rows.push({ kind: "calc", text: "Consulta de tus próximos plazos" });
      } else if (event.tool === "consultar_vigencia") {
        const ref = typeof event.arguments.referencia === "string" ? event.arguments.referencia : "";
        rows.push({ kind: "search", text: `¿Sigue vigente? ${ref}`.trim() });
      }
    } else if (event.type === "tool_result") {
      const last = rows[rows.length - 1];
      if (!last || last.outcome !== undefined) continue;
      if (event.is_error) {
        last.outcome = "no se pudo completar";
        last.failed = true;
      } else if (last.kind === "search") {
        const count = /^(\d+) fragmentos/.exec(event.summary);
        last.outcome = count ? (count[1] === "1" ? "1 fragmento" : `${count[1]} fragmentos`) : "sin resultados";
      } else {
        last.outcome = "hecho";
      }
    } else if (event.type === "verifying") {
      rows.push({
        kind: "verify",
        text:
          event.citations > 0
            ? `Verificación de cada afirmación contra ${event.citations === 1 ? "la fuente citada" : `las ${event.citations} fuentes citadas`}`
            : "Revisión de la respuesta",
      });
    } else if (event.type === "revising") {
      rows.push({
        kind: "revise",
        text:
          event.problems === 1
            ? "Corrección de una afirmación que no cuadraba con su fuente"
            : `Corrección de ${event.problems} afirmaciones que no cuadraban con sus fuentes`,
      });
    }
  }
  return rows;
}

/** The transient one-liner shown next to the spinner. */
export function statusLine(events: ProgressEvent[]): string {
  const last = events[events.length - 1];
  if (!last) return "Conectando con el asesor…";
  if (last.type === "text_delta") return "Redactando la respuesta…";
  if (last.type === "team") {
    if (last.stage === "planning") return "Valorando si la consulta necesita al equipo…";
    if (last.stage === "critic") return last.status === "start" ? "Un revisor busca fallos en el borrador…" : "Leyendo la revisión…";
    if (last.stage === "specialist") return last.status === "start" ? "Consultando al especialista…" : "Leyendo el informe del especialista…";
    return "El equipo está investigando…";
  }
  if (last.type === "thinking") return last.step === 1 ? "Analizando tu consulta…" : "Leyendo lo encontrado y decidiendo el siguiente paso…";
  if (last.type === "tool_call") {
    if (last.agent?.startsWith("especialista_")) return "El especialista está calculando…";
    if (last.agent) return `${TEAM_MEMBER[last.agent] ?? "El equipo"}: buscando…`;
    return last.tool === "buscar_fuentes" ? "Buscando en la normativa…" : "Trabajando con tus datos…";
  }
  if (last.type === "tool_result") return "Leyendo lo encontrado…";
  if (last.type === "verifying") return "Verificando la respuesta contra las fuentes…";
  return "Corrigiendo la respuesta…";
}

// ---- Sources & verification ------------------------------------------------

/** DOM id of a source card, the jump target of the [F#] chips of one turn. */
export function sourceAnchorId(scope: string, fragmentId: string): string {
  return `src-${scope}-${fragmentId}`;
}

/** "4 de 5 afirmaciones comprobadas" for the verdict banner. */
export function verificationSummary(claims: ClaimCheck[]): string | null {
  if (claims.length === 0) return null;
  const ok = claims.filter((c) => c.verdict === "respaldada").length;
  if (ok === claims.length) {
    return claims.length === 1 ? "1 afirmación comprobada" : `${claims.length} afirmaciones comprobadas`;
  }
  return `${ok} de ${claims.length} afirmaciones comprobadas`;
}


// ---- Autonomous communities ------------------------------------------------

/** The communities the calculators know, as the simulator and the optimiser offer them. */
export const COMUNIDADES: Array<{ value: string; label: string }> = [
  { value: "", label: "Sin indicar" },
  { value: "Andalucía", label: "Andalucía" },
  { value: "Aragón", label: "Aragón" },
  { value: "Asturias", label: "Principado de Asturias" },
  { value: "Baleares", label: "Illes Balears" },
  { value: "Canarias", label: "Canarias" },
  { value: "Cantabria", label: "Cantabria" },
  { value: "Castilla-La Mancha", label: "Castilla-La Mancha" },
  { value: "Castilla y León", label: "Castilla y León" },
  { value: "Cataluña", label: "Cataluña" },
  { value: "Comunidad Valenciana", label: "Comunitat Valenciana" },
  { value: "Extremadura", label: "Extremadura" },
  { value: "Galicia", label: "Galicia" },
  { value: "La Rioja", label: "La Rioja" },
  { value: "Madrid", label: "Comunidad de Madrid" },
  { value: "Murcia", label: "Región de Murcia" },
  { value: "Otra", label: "Navarra, País Vasco, Ceuta o Melilla" },
];

// Accent- and case-insensitive match of a saved community name against the list.
const plain = (text: string) =>
  text
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();

/** The list value for a `comunidad_autonoma` fact: "" when absent, "Otra" when not in the list. */
export function comunidadDeExpediente(value: unknown): string {
  if (typeof value !== "string") return "";
  const saved = plain(value);
  const match = COMUNIDADES.find((c) => c.value && c.value !== "Otra" && (saved.includes(plain(c.value)) || saved.includes(plain(c.label))));
  return match ? match.value : "Otra";
}

// ---- Money -----------------------------------------------------------------

const EUR_CENTS = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR" });

/** "-1500.00" → "-1.500,00 €". Accepts the decimal strings the API returns. */
export function formatMoney(value: string | number): string {
  const amount = typeof value === "number" ? value : Number(value);
  return Number.isFinite(amount) ? EUR_CENTS.format(amount) : String(value);
}

/** Read a File as base64 (without the data-URL prefix). */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("No se pudo leer el archivo."));
    reader.onload = () => {
      const result = typeof reader.result === "string" ? reader.result : "";
      resolve(result.slice(result.indexOf(",") + 1));
    };
    reader.readAsDataURL(file);
  });
}
