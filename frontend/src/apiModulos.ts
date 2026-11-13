// Client for the household modules (hacienda_gpt/api/routers/modulos.py): which modules exist and
// which are on for this user, the pending actions the specialists prepared, and running a module's
// tool or document reader from the UI.
//
// Everything goes through api.ts's `request`: same session token, timeout and error messages.

import { request } from "./api";

export type TipoHecho = "string" | "number" | "boolean" | "json";
export type Efecto = "lectura" | "prepara" | "actua";

export interface HechoInfo {
  nombre: string;
  tipo: TipoHecho;
  etiqueta: string;
  descripcion: string;
  sensible: boolean;
}

export interface HerramientaInfo {
  nombre: string;
  efecto: Efecto;
  descripcion: string;
  // Takes a whole file (`contenido` / `contenido_base64`), uploaded straight to the tool.
  acepta_fichero?: boolean;
}

export interface FuenteInfo {
  identificador: string;
  descripcion: string;
  url: string;
}

export interface ModuloInfo {
  clave: string;
  nombre: string;
  descripcion: string;
  activo: boolean;
  obligatorio: boolean;
  elegido: boolean;
  elegido_el?: string | null;
  hechos: HechoInfo[];
  herramientas: HerramientaInfo[];
  fuentes: FuenteInfo[];
  lee_documentos?: boolean;
  herramienta_de_documento?: string | null;
}

export interface ModulosResponse {
  configurable: boolean;
  modulos: ModuloInfo[];
}

export type EstadoAccion = "pendiente" | "ejecutando" | "ejecutada" | "fallida" | "rechazada";

export interface AccionPendiente {
  id: string;
  case_id: string;
  modulo: string;
  herramienta: string;
  argumentos: Record<string, unknown>;
  resumen: string;
  estado: EstadoAccion;
  created_at: string;
  decided_at?: string | null;
  resultado?: string | null;
  es_error: boolean;
}

export interface ResultadoHerramienta {
  contenido: string;
  resumen: string;
  es_error: boolean;
  citas: string[];
}

export interface EjecucionResponse {
  resultado: ResultadoHerramienta | null;
  accion: AccionPendiente | null;
  case_updated: boolean;
}

export interface CampoPropuesto {
  campo: string;
  valor: string;
  evidencia: string;
  origen?: string;
}

export interface DocumentoModuloResponse {
  modulo: string;
  herramienta_destino: string | null;
  propuesta: { campos?: CampoPropuesto[]; avisos?: string[]; descartados?: string[] } & Record<string, unknown>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isModulos(value: unknown): value is ModulosResponse {
  return isRecord(value) && typeof value.configurable === "boolean" && Array.isArray(value.modulos);
}

function isAccion(value: unknown): value is AccionPendiente {
  return isRecord(value) && typeof value.id === "string" && typeof value.estado === "string";
}

function isAcciones(value: unknown): value is { case_id: string; acciones: AccionPendiente[] } {
  return isRecord(value) && Array.isArray(value.acciones);
}

function isEjecucion(value: unknown): value is EjecucionResponse {
  return isRecord(value) && "resultado" in value && "accion" in value;
}

function isDocumento(value: unknown): value is DocumentoModuloResponse {
  return isRecord(value) && typeof value.modulo === "string" && isRecord(value.propuesta);
}

/** The modules this service offers and whether each one is on for the signed-in user. */
export function getModulos(signal?: AbortSignal): Promise<ModulosResponse> {
  return request("/modulos", undefined, isModulos, { signal, method: "GET" });
}

/** Switch a module on or off; `borrarDatos` (only when switching off) erases its facts too. */
export function setModulo(clave: string, activo: boolean, borrarDatos = false): Promise<ModulosResponse> {
  return request(`/me/modulos/${encodeURIComponent(clave)}`, { activo, borrar_datos: borrarDatos }, isModulos, { method: "PUT" });
}

export function getAcciones(caseId: string, signal?: AbortSignal): Promise<AccionPendiente[]> {
  return request(`/acciones?case_id=${encodeURIComponent(caseId)}`, undefined, isAcciones, { signal, method: "GET" }).then(
    (resp) => resp.acciones,
  );
}

export function confirmarAccion(id: string): Promise<AccionPendiente> {
  return request(`/acciones/${encodeURIComponent(id)}/confirmar`, {}, isAccion);
}

export function rechazarAccion(id: string): Promise<AccionPendiente> {
  return request(`/acciones/${encodeURIComponent(id)}/rechazar`, {}, isAccion);
}

/** Run one of a module's tools for this case (acting tools only become pending actions). */
export function runModuleTool(opts: {
  clave: string;
  herramienta: string;
  caseId: string;
  argumentos: Record<string, unknown>;
}): Promise<EjecucionResponse> {
  return request(
    `/modulos/${encodeURIComponent(opts.clave)}/herramientas/${encodeURIComponent(opts.herramienta)}`,
    { case_id: opts.caseId, argumentos: opts.argumentos },
    isEjecucion,
  );
}

/** A module reads a document and PROPOSES data; nothing is stored. */
export function readModuleDocument(opts: {
  clave: string;
  caseId: string;
  filename: string;
  contentBase64: string;
}): Promise<DocumentoModuloResponse> {
  return request(
    `/modulos/${encodeURIComponent(opts.clave)}/documento`,
    { case_id: opts.caseId, filename: opts.filename, content_base64: opts.contentBase64 },
    isDocumento,
    { timeoutMs: 120_000 },
  );
}

/** The module a notification belongs to: the tax kinds are "fiscal", the weekly digest has none. */
export function moduloDeKind(kind: string): string | null {
  if (kind.startsWith("modulo:")) return kind.slice("modulo:".length);
  return kind === "resumen_semanal" ? null : "fiscal";
}

/** A tax fact a module can fill in; nothing is written until the user accepts it. */
export interface PropuestaHecho {
  nombre: string;
  valor: number;
  titulo: string;
  explicacion: string;
  modulo: string;
  desglose: Record<string, string>;
}

function isPropuestas(value: unknown): value is { case_id: string; propuestas: PropuestaHecho[] } {
  return isRecord(value) && Array.isArray(value.propuestas);
}

export function getPropuestas(caseId: string, signal?: AbortSignal): Promise<PropuestaHecho[]> {
  return request(`/propuestas?case_id=${encodeURIComponent(caseId)}`, undefined, isPropuestas, { signal, method: "GET" }).then(
    (resp) => resp.propuestas,
  );
}
