// Client for the deterministic tax optimiser (POST /optimize): the best
// combination of the choices the law leaves to a household. No model call.
//
// Shapes mirror hacienda_gpt/tax/optimizador.py (PlanOptimo, Restricciones,
// PosicionLatente). Amounts are decimals sent as strings ("1082.46").
// Everything goes through api.ts's `request`: session token, timeout, errors.

import type { CalcLine } from "./api";
import { request } from "./api";

export type Naturaleza = "ahorro_definitivo" | "diferimiento" | "requiere_gastar";
export type CausaNoDisponible = "fecha" | "restricciones" | "faltan_datos" | "sin_margen" | "no_calculable";
export type TipoActivo = "valores_cotizados" | "valores_no_cotizados" | "otro_elemento";
export type Titular = "contribuyente" | "conyuge";
export type Tributacion = "individual" | "conjunta_biparental" | "conjunta_monoparental";

export interface PosicionLatente {
  descripcion?: string;
  importe: number;
  tipo?: TipoActivo;
  titular?: Titular;
}

export interface Restricciones {
  aportacion_maxima_pensiones?: number;
  aportacion_maxima_pensiones_conyuge?: number;
  acepta_iliquidez?: boolean;
  donativo_previsto?: number;
  presupuesto_donativos?: number;
  donativo_recurrente_importe_anterior?: number;
  perdidas_latentes?: PosicionLatente[];
  ganancias_latentes?: PosicionLatente[];
}

export interface OptimizeRequest {
  case_id?: string;
  ejercicio?: number;
  datos: Record<string, unknown>;
  conyuge?: Record<string, unknown>;
  restricciones: Restricciones;
  fecha?: string;
}

/** The calculator's result for one return (only the fields this screen reads). */
export interface ResultadoDeclaracion {
  ejercicio: number;
  tributacion: string;
  resultado: string;
  cuota_liquida: string;
  base_liquidable_general: string;
  base_liquidable_ahorro: string;
  lineas: CalcLine[];
  avisos: string[];
  parametros_no_verificados: string[];
  fuente: string;
}

export interface Declaracion {
  titular: "contribuyente" | "conyuge" | "conjunta";
  resultado: ResultadoDeclaracion;
}

export interface ResultadoHogar {
  tributacion: Tributacion;
  declaraciones: Declaracion[];
  /** Positive: to pay; negative: refund. */
  resultado_total: string;
  cuota_liquida_total: string;
}

export interface PalancaElegida {
  id: string;
  nombre: string;
  titular: "contribuyente" | "conyuge" | "hogar";
  accion: string;
  importe: string | null;
  opcion: string | null;
  naturaleza: Naturaleza;
  /** Euros the lever locks, spends or realises (0 for a filing option). */
  compromiso: string;
  /** Tax the plan would pay more without this lever. */
  ahorro_marginal: string;
  /** Saving of the lever alone, at its own best value (can be negative). */
  ahorro_aislado: string;
  accion_aislada: string;
  ahorro_por_euro: string | null;
  coste_neto: string | null;
  plazo: string | null;
  referencia: string;
  verificado: boolean;
  requisitos: string[];
  explicacion: string;
}

export interface PalancaNoDisponible {
  id: string;
  nombre: string;
  causa: CausaNoDisponible;
  detalle: string;
  datos_que_faltan: string[];
}

export interface PalancaDescartada {
  id: string;
  nombre: string;
  motivo: string;
  referencia: string;
}

export interface CambioLinea {
  concepto: string;
  antes: string;
  despues: string;
  referencia: string | null;
}

export interface TextoLegal {
  referencia: string;
  boe_id: string;
  texto: string;
}

export interface Busqueda {
  metodo: "exhaustiva" | "heuristica";
  evaluaciones: number;
  limite_evaluaciones: number;
  combinaciones: number;
  candidatos: Record<string, number>;
  nota: string;
}

export interface PlanOptimo {
  ejercicio: number;
  fecha: string;
  fase: "antes_del_cierre" | "solo_opciones_de_declaracion";
  con_conyuge: boolean;
  base: ResultadoHogar;
  optimo: ResultadoHogar;
  ahorro_total: string;
  suma_ahorros_aislados: string;
  interaccion: string;
  explicacion_interaccion: string[];
  palancas: PalancaElegida[];
  no_disponibles: PalancaNoDisponible[];
  descartadas: PalancaDescartada[];
  oportunidades: string[];
  cambios_traza: CambioLinea[];
  citas: string[];
  textos_legales: TextoLegal[];
  avisos: string[];
  supuestos: string[];
  datos_que_faltan: string[];
  busqueda: Busqueda;
  explicacion: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const isStrings = (value: unknown): value is string[] => Array.isArray(value) && value.every((item) => typeof item === "string");

function isHogar(value: unknown): value is ResultadoHogar {
  return (
    isRecord(value) &&
    typeof value.tributacion === "string" &&
    typeof value.resultado_total === "string" &&
    Array.isArray(value.declaraciones) &&
    value.declaraciones.every((d) => isRecord(d) && typeof d.titular === "string" && isRecord(d.resultado) && Array.isArray(d.resultado.lineas))
  );
}

function isPalanca(value: unknown): value is PalancaElegida {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.accion === "string" &&
    typeof value.naturaleza === "string" &&
    typeof value.ahorro_marginal === "string" &&
    typeof value.ahorro_aislado === "string" &&
    typeof value.verificado === "boolean" &&
    isStrings(value.requisitos)
  );
}

function isPlan(value: unknown): value is PlanOptimo {
  return (
    isRecord(value) &&
    typeof value.ejercicio === "number" &&
    typeof value.fase === "string" &&
    typeof value.ahorro_total === "string" &&
    typeof value.interaccion === "string" &&
    isHogar(value.base) &&
    isHogar(value.optimo) &&
    Array.isArray(value.palancas) &&
    value.palancas.every(isPalanca) &&
    Array.isArray(value.no_disponibles) &&
    Array.isArray(value.descartadas) &&
    Array.isArray(value.cambios_traza) &&
    Array.isArray(value.textos_legales) &&
    isStrings(value.explicacion_interaccion) &&
    isStrings(value.avisos) &&
    isStrings(value.supuestos) &&
    isStrings(value.datos_que_faltan) &&
    isStrings(value.oportunidades) &&
    isStrings(value.citas) &&
    isRecord(value.busqueda) &&
    typeof value.explicacion === "string"
  );
}

/** The optimal plan for the household. A household with many levers can take a second or two. */
export function optimizar(body: OptimizeRequest, signal?: AbortSignal): Promise<PlanOptimo> {
  return request("/optimize", body, isPlan, { signal, timeoutMs: 60_000 });
}
