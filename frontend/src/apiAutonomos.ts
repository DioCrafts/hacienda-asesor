// Client for the self-employed calculators (POST /autonomos/*). Deterministic
// endpoints: no model call, so they answer as the user types.
//
// Kept apart from api.ts on purpose; it only borrows ApiError and CalcLine
// from there so errors and calculation lines look the same everywhere.

import type { CalcLine } from "./api";
import { ApiError } from "./api";

const API_BASE = (import.meta.env.VITE_API_BASE as string) || "/api";
const TIMEOUT_MS = 30_000;

/** Fields every calculator result carries. Amounts are decimals as strings. */
interface ResultadoBase {
  ejercicio: number;
  lineas: CalcLine[];
  avisos: string[];
  parametros_no_verificados: string[];
  fuente: string;
}

export interface ActividadInput {
  ejercicio: number;
  ingresos_integros?: number;
  seguridad_social_autonomo?: number;
  suministros?: number;
  alquileres?: number;
  servicios_profesionales?: number;
  otros_gastos?: number;
  amortizaciones?: number;
  cifra_negocios_ano_anterior?: number;
  cumple_requisitos_reduccion_dependiente?: boolean;
  otras_rentas_no_exentas?: number;
  discapacidad?: boolean;
  discapacidad_ampliada?: boolean;
  reduccion_rendimientos_trabajo?: number;
}

export interface ResultadoActividad extends ResultadoBase {
  ingresos_integros: string;
  gastos_deducibles: string;
  rendimiento_neto_previo: string;
  gastos_dificil_justificacion: string;
  rendimiento_neto: string;
  reduccion_art_32_2: string;
  reduccion_aplicada: string | null;
  rendimiento_neto_reducido: string;
  /** The figure the IRPF simulator expects as net income of the activity. */
  rendimiento_actividad_neto: string;
}

export type Trimestre = 1 | 2 | 3 | 4;

export interface Modelo130Input {
  ejercicio: number;
  trimestre: Trimestre;
  ingresos_acumulados?: number;
  gastos_acumulados?: number;
  aplicar_gastos_dificil_justificacion?: boolean;
  retenciones_acumuladas?: number;
  pagos_trimestres_anteriores?: number;
  actividad_profesional?: boolean;
  rendimiento_neto_ejercicio_anterior?: number;
  minoracion_pendiente_trimestres_anteriores?: number;
  ingresos_ano_anterior?: number;
  ingresos_con_retencion_ano_anterior?: number;
  inicio_actividad?: boolean;
  ingresos_con_retencion_acumulados?: number;
}

export interface ResultadoModelo130 extends ResultadoBase {
  trimestre: number;
  rendimiento_neto_acumulado: string;
  pago_sobre_rendimiento: string;
  pago_previo_a_minoracion: string;
  minoracion_aplicada: string;
  minoracion_pendiente: string;
  resultado: string;
  /** null = it cannot be told from the figures given. */
  obligado_a_pago_fraccionado: boolean | null;
  motivo_obligacion: string;
  a_ingresar: string;
}

export interface Modelo303Input {
  ejercicio: number;
  trimestre: Trimestre;
  base_tipo_general?: number;
  base_tipo_reducido?: number;
  base_tipo_superreducido?: number;
  cuotas_soportadas_deducibles?: number;
  cuotas_soportadas_bienes_inversion?: number;
  cuotas_a_compensar_periodos_anteriores?: number;
}

export interface ResultadoModelo303 extends ResultadoBase {
  trimestre: number;
  iva_devengado: string;
  iva_deducible: string;
  diferencia: string;
  compensacion_aplicada: string;
  /** Positive: to pay. Negative: balance in the taxpayer's favour. */
  resultado: string;
  tipo_resultado: "a_ingresar" | "a_compensar" | "cero";
  saldo_a_compensar: string;
  puede_solicitar_devolucion: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isResultado(value: unknown, ...amounts: string[]): boolean {
  return (
    isRecord(value) &&
    typeof value.ejercicio === "number" &&
    Array.isArray(value.lineas) &&
    Array.isArray(value.avisos) &&
    typeof value.fuente === "string" &&
    amounts.every((name) => typeof value[name] === "string")
  );
}

async function call<T>(path: string, body: unknown, validate: (value: unknown) => boolean, signal?: AbortSignal): Promise<T> {
  const timeout = AbortSignal.timeout(TIMEOUT_MS);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: combined,
    });
  } catch (err) {
    if (timeout.aborted) throw new ApiError("El servicio tardó demasiado en responder.", null, { cause: err });
    throw err;
  }
  if (!resp.ok) {
    // FastAPI errors are {"detail": "..."}; these endpoints already answer in Spanish.
    const text = await resp.text().catch(() => "");
    let detail = text || resp.statusText;
    try {
      const parsed: unknown = JSON.parse(text);
      if (isRecord(parsed) && typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      // not JSON: keep the raw text
    }
    if (resp.status === 401) detail = "El servicio requiere una clave de acceso que esta aplicación no tiene configurada.";
    if (resp.status === 429) detail = "Demasiadas consultas seguidas. Espera un momento y vuelve a intentarlo.";
    throw new ApiError(detail || `${path}: error ${resp.status}`, resp.status);
  }
  const data: unknown = await resp.json();
  if (!validate(data)) throw new ApiError("El servidor devolvió una respuesta con un formato inesperado.", resp.status);
  return data as T;
}

/** Years the calculators have parameters for, ascending. */
export async function getEjerciciosAutonomos(signal?: AbortSignal): Promise<number[]> {
  const data = await call<{ ejercicios: number[] }>(
    "/autonomos/ejercicios",
    undefined,
    (value) => isRecord(value) && Array.isArray(value.ejercicios) && value.ejercicios.every((year) => typeof year === "number"),
    signal,
  );
  return data.ejercicios;
}

/** Annual net income of the activity (estimación directa simplificada). */
export function calcularActividad(input: ActividadInput, signal?: AbortSignal): Promise<ResultadoActividad> {
  return call("/autonomos/actividad", input, (value) => isResultado(value, "rendimiento_actividad_neto", "rendimiento_neto"), signal);
}

/** Quarterly IRPF payment on account (modelo 130), from cumulative figures. */
export function calcularModelo130(input: Modelo130Input, signal?: AbortSignal): Promise<ResultadoModelo130> {
  return call("/autonomos/modelo-130", input, (value) => isResultado(value, "resultado", "a_ingresar", "pago_previo_a_minoracion"), signal);
}

/** Quarterly VAT return (modelo 303), general regime. */
export function calcularModelo303(input: Modelo303Input, signal?: AbortSignal): Promise<ResultadoModelo303> {
  return call("/autonomos/modelo-303", input, (value) => isResultado(value, "resultado", "saldo_a_compensar", "tipo_resultado"), signal);
}
