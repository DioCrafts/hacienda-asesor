// Client for the self-employed calculators (POST /autonomos/*) and for the
// activity as the case sees it (POST /simulate). Deterministic endpoints: no
// model call, so they answer as the user types.
//
// Everything goes through api.ts's `request`, so these calls carry the session
// token, the timeout and the error messages of the rest of the app.

import type { CalcLine } from "./api";
import { request } from "./api";

/** Fields every calculator result carries. Amounts are decimals as strings. */
interface ResultadoBase {
  ejercicio: number;
  lineas: CalcLine[];
  avisos: string[];
  parametros_no_verificados: string[];
  fuente: string;
}

export type CeutaMelilla = "ceuta" | "melilla";

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
  rendimientos_irregulares?: number;
  reduccion_inicio_actividad?: boolean;
  ceuta_melilla?: CeutaMelilla;
}

export interface ResultadoActividad extends ResultadoBase {
  ingresos_integros: string;
  gastos_deducibles: string;
  rendimiento_neto_previo: string;
  gastos_dificil_justificacion: string;
  rendimiento_neto: string;
  reduccion_art_32_1: string;
  reduccion_art_32_2: string;
  reduccion_art_32_3: string;
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
  ceuta_melilla?: CeutaMelilla;
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

/** What the profile bridge did with the activity figures of a case (see hacienda_gpt/tax/profile.py). */
export interface ActividadPerfil {
  /** True when the net income was computed from the breakdown. */
  calculado: boolean;
  resultado: ResultadoActividad | null;
  /** Sentence explaining how the figure was obtained. */
  aviso: string;
  /** Other non-exempt income of the case and the work-income reduction used for art. 32.2 (null: not derived). */
  otras_rentas_no_exentas: string | null;
  reduccion_rendimientos_trabajo: string | null;
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

function isActividad(value: unknown): value is ResultadoActividad {
  return isResultado(value, "rendimiento_actividad_neto", "rendimiento_neto");
}
function isModelo130(value: unknown): value is ResultadoModelo130 {
  return isResultado(value, "resultado", "a_ingresar", "pago_previo_a_minoracion");
}
function isModelo303(value: unknown): value is ResultadoModelo303 {
  return isResultado(value, "resultado", "saldo_a_compensar", "tipo_resultado");
}
function isEjercicios(value: unknown): value is { ejercicios: number[] } {
  return isRecord(value) && Array.isArray(value.ejercicios) && value.ejercicios.every((year) => typeof year === "number");
}
function isActividadPerfil(value: unknown): value is ActividadPerfil {
  return isRecord(value) && typeof value.calculado === "boolean" && typeof value.aviso === "string" && (value.resultado === null || isActividad(value.resultado));
}
interface SimulacionConActividad {
  escenarios: Array<{ actividad?: ActividadPerfil | null }>;
}
function isSimulacionConActividad(value: unknown): value is SimulacionConActividad {
  return (
    isRecord(value) &&
    Array.isArray(value.escenarios) &&
    value.escenarios.length > 0 &&
    value.escenarios.every((item) => isRecord(item) && (item.actividad === undefined || item.actividad === null || isActividadPerfil(item.actividad)))
  );
}

/** Years the calculators have parameters for, ascending. */
export async function getEjerciciosAutonomos(signal?: AbortSignal): Promise<number[]> {
  const data = await request("/autonomos/ejercicios", undefined, isEjercicios, { signal, method: "GET" });
  return data.ejercicios;
}

/** Annual net income of the activity (estimación directa simplificada), from the figures alone. */
export function calcularActividad(input: ActividadInput, signal?: AbortSignal): Promise<ResultadoActividad> {
  return request("/autonomos/actividad", input, isActividad, { signal });
}

/**
 * The same computation as the case sees it: `cambios` (actividad_* facts) layered over the saved
 * facts, with the case's other income deciding the art. 32.2 reductions. Null when the case and
 * the changes hold no activity figures.
 */
export async function calcularActividadDelExpediente(opts: {
  caseId: string;
  ejercicio: number;
  cambios: Record<string, unknown>;
  signal?: AbortSignal;
}): Promise<ActividadPerfil | null> {
  const body = { case_id: opts.caseId, ejercicio: opts.ejercicio, datos: {}, escenarios: [{ nombre: "Actividad", cambios: opts.cambios }] };
  const data = await request("/simulate", body, isSimulacionConActividad, { signal: opts.signal });
  return data.escenarios[0].actividad ?? null;
}

/** Quarterly IRPF payment on account (modelo 130), from cumulative figures. */
export function calcularModelo130(input: Modelo130Input, signal?: AbortSignal): Promise<ResultadoModelo130> {
  return request("/autonomos/modelo-130", input, isModelo130, { signal });
}

/** Quarterly VAT return (modelo 303), general regime. */
export function calcularModelo303(input: Modelo303Input, signal?: AbortSignal): Promise<ResultadoModelo303> {
  return request("/autonomos/modelo-303", input, isModelo303, { signal });
}
