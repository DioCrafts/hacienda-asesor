// Simulator: change a figure, see the tax change. Everything here is the
// deterministic calculator (POST /simulate) — no model call, so it answers as
// you type. The saved case is the baseline; edits are compared against it.

import { Calculator, ChevronDown, Lightbulb, Plus, RotateCcw, Save, Trash2, TriangleAlert, X } from "lucide-react";
import clsx from "clsx";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import type { Benefit, Fact, Simulation } from "../api";
import { ApiError, simulate } from "../api";
import { newId } from "../lib/id";
import { ETAPA_LABEL, factLabel, formatMoney } from "../lib/presentation";

type FieldKind = "money" | "count" | "year" | "flag";
interface FieldDef {
  name: string;
  label: string;
  kind: FieldKind;
  hint?: string;
  // Narrower amounts this field is the total of (the calculator keeps total ≥ sum of parts,
  // and takes an empty total as that sum).
  parts?: FieldDef[];
}
interface Group {
  title: string;
  fields: FieldDef[];
}

const GROUPS: Group[] = [
  {
    title: "Ingresos del año",
    fields: [
      { name: "rendimiento_trabajo_bruto", label: "Salario bruto", kind: "money" },
      { name: "cotizaciones_seguridad_social", label: "Cotizaciones a la Seguridad Social", kind: "money", hint: "Lo que te descuentan en nómina (≈ 6,5 % del bruto)" },
      { name: "retenciones", label: "IRPF ya retenido", kind: "money" },
      { name: "rendimiento_actividad_neto", label: "Beneficio neto de tu actividad", kind: "money", hint: "Si eres autónomo: calcúlalo en el panel «Autónomos»" },
      { name: "rendimiento_capital_mobiliario", label: "Intereses y dividendos", kind: "money" },
      { name: "ganancias_patrimoniales", label: "Ganancias por ventas", kind: "money", hint: "Acciones, fondos, inmuebles… (negativo si son pérdidas)" },
    ],
  },
  {
    title: "Familia",
    fields: [
      { name: "num_descendientes", label: "Hijos a cargo", kind: "count" },
      { name: "num_descendientes_menores_3", label: "De ellos, menores de 3 años", kind: "count" },
    ],
  },
  {
    title: "Lo que puedes mover",
    fields: [
      { name: "aportacion_plan_pensiones", label: "Aportación a plan de pensiones", kind: "money", hint: "Reduce la base hasta 1.500 € al año" },
      { name: "donativos", label: "Donativos", kind: "money" },
    ],
  },
];
// The deductions the Comunidad de Madrid computes with its own rules; the groups below add
// the inputs of its other deductions.
const MADRID_GROUP: Group = {
  title: "Deducciones de la Comunidad de Madrid",
  fields: [
    { name: "edad", label: "Tu edad", kind: "count", hint: "El alquiler solo deduce si tienes menos de 40 años" },
    { name: "alquiler_vivienda_habitual", label: "Alquiler pagado en el año", kind: "money" },
    { name: "hijos_nacidos_ultimos_3_ejercicios", label: "Hijos nacidos en los últimos 3 años", kind: "count" },
    { name: "gastos_escolaridad", label: "Gastos de escolaridad", kind: "money", hint: "Colegio privado no concertado o primer ciclo de infantil" },
    { name: "gastos_ensenanza_idiomas", label: "Clases de idiomas de los hijos", kind: "money" },
    { name: "gastos_uniformes_escolares", label: "Uniformes escolares", kind: "money" },
    { name: "cotizaciones_empleada_hogar", label: "Cotizaciones de empleada de hogar", kind: "money", hint: "Con un hijo menor de 3 años" },
    { name: "base_imponible_resto_unidad_familiar", label: "Rentas netas de tu pareja", kind: "money", hint: "Cuentan para los límites de renta familiar" },
  ],
};
// Inputs of the regional deductions computed from data, by subject. Where a law covers only
// part of an expense (only dental care, only interest…), the part is asked under its total.
const REGIONAL_GROUPS: Group[] = [
  {
    title: "Tu comunidad · tú y tu hogar",
    fields: [
      { name: "edad", label: "Tu edad", kind: "count", hint: "Varias deducciones (alquiler, mayores) dependen de ella" },
      { name: "grado_discapacidad", label: "Tu grado de discapacidad (%)", kind: "count" },
      { name: "edad_conyuge", label: "Edad de tu cónyuge", kind: "count", hint: "En conjunta, a veces basta con que uno cumpla la edad" },
      { name: "grado_discapacidad_conyuge", label: "Discapacidad de tu cónyuge (%)", kind: "count" },
      { name: "base_imponible_resto_unidad_familiar", label: "Rentas netas de tu pareja", kind: "money", hint: "Cuentan para los límites de renta familiar" },
      { name: "dias_desempleo", label: "Días del año en desempleo", kind: "count", hint: "Inscrito como demandante de empleo" },
      { name: "familia_numerosa", label: "Familia numerosa", kind: "flag" },
      { name: "familia_numerosa_especial", label: "De categoría especial", kind: "flag" },
      { name: "familia_monoparental", label: "Familia monoparental", kind: "flag" },
      { name: "viudedad", label: "Soy viudo o viuda", kind: "flag" },
      { name: "ejercicio_viudedad", label: "Año en que enviudaste", kind: "year" },
      { name: "victima_violencia_genero", label: "Tengo reconocida la condición de víctima de violencia de género", kind: "flag" },
      { name: "victima_terrorismo", label: "Tengo reconocida la condición de víctima del terrorismo", kind: "flag" },
    ],
  },
  {
    title: "Tu comunidad · hijos y personas a tu cargo",
    fields: [
      { name: "hijos_nacidos_en_el_ejercicio", label: "Hijos nacidos o adoptados este año", kind: "count" },
      { name: "hijos_nacidos_ultimos_3_ejercicios", label: "Nacidos o adoptados este año o los dos anteriores", kind: "count" },
      { name: "num_hijos_primer_ciclo_infantil", label: "Hijos en primer ciclo de infantil (0-3)", kind: "count" },
      { name: "num_hijos_gastos_educativos", label: "Hijos escolarizados", kind: "count", hint: "Por los que pagas libros o clases" },
      { name: "gastos_guarderia", label: "Guardería o escuela infantil (0-3)", kind: "money" },
      { name: "gastos_libros_texto", label: "Libros de texto y material escolar", kind: "money" },
      { name: "gastos_ensenanza_idiomas", label: "Clases de idiomas de los hijos", kind: "money" },
      { name: "cotizaciones_empleada_hogar", label: "Cotizaciones de empleada de hogar", kind: "money" },
      { name: "gastos_cuidado_familiares", label: "Cuidado de hijos, mayores o personas con discapacidad", kind: "money", hint: "Pagado a cuidadores o centros, sin las cotizaciones" },
      { name: "progenitores_trabajan", label: "Trabajamos los dos progenitores (o yo, si no hay otro)", kind: "flag", hint: "Por cuenta ajena o propia, de alta en la Seguridad Social o en una mutualidad" },
      { name: "num_ascendientes_mayores_75", label: "Ascendientes mayores de 75 a tu cargo", kind: "count" },
      { name: "num_familiares_discapacidad_a_cargo", label: "Familiares con discapacidad a tu cargo", kind: "count" },
      { name: "num_familiares_discapacidad_65_a_cargo", label: "De ellos, con discapacidad del 65 % o más", kind: "count" },
      { name: "num_personas_acogidas_mayores_o_discapacidad", label: "Mayores de 65 o con discapacidad que acoges", kind: "count", hint: "Sin parentesco cercano y sin cobrar por ello" },
      { name: "menores_acogidos", label: "Menores en acogimiento familiar", kind: "count" },
      { name: "dias_acogimiento", label: "Días del año de convivencia con ellos", kind: "count" },
    ],
  },
  {
    title: "Tu comunidad · vivienda y municipio",
    fields: [
      { name: "alquiler_vivienda_habitual", label: "Alquiler pagado en el año", kind: "money" },
      { name: "dias_alquiler_vivienda_habitual", label: "Días del año con el alquiler en vigor", kind: "count", hint: "365 si fue todo el año; algunos límites se prorratean" },
      { name: "num_declaraciones_mismo_alquiler", label: "Declaraciones que deducen por ese alquiler", kind: "count", hint: "Contando la tuya: 2 si lo compartes con otro inquilino" },
      {
        name: "inversion_vivienda_habitual",
        label: "Compra o rehabilitación de tu vivienda habitual (total)",
        kind: "money",
        hint: "Todo lo pagado en el año, hipoteca incluida",
        parts: [
          { name: "pagos_adquisicion_vivienda", label: "Compra: capital y gastos", kind: "money", hint: "Sin los intereses" },
          { name: "pagos_rehabilitacion_vivienda", label: "Rehabilitación", kind: "money", hint: "Sin los intereses" },
          { name: "intereses_prestamo_vivienda", label: "Intereses del préstamo", kind: "money" },
        ],
      },
      { name: "ejercicio_adquisicion_vivienda", label: "Año en que la compraste", kind: "year" },
      { name: "num_declaraciones_misma_vivienda", label: "Declaraciones que deducen por esa vivienda", kind: "count", hint: "Contando la tuya: 2 si la compráis entre dos" },
      { name: "primera_vivienda", label: "Es mi primera vivienda en propiedad", kind: "flag" },
      { name: "vivienda_protegida", label: "Es una vivienda protegida", kind: "flag" },
      { name: "vivienda_nueva_construccion", label: "Es de nueva construcción", kind: "flag" },
      { name: "vivienda_en_municipio_rural", label: "La vivienda que compro está en un municipio rural", kind: "flag" },
      {
        name: "gastos_eficiencia_energetica",
        label: "Obras de mejora energética en tu vivienda (total)",
        kind: "money",
        hint: "Aislamiento, ventanas, calderas, renovables…",
        parts: [{ name: "gastos_instalaciones_renovables", label: "Energía renovable o autoconsumo", kind: "money", hint: "Placas solares, aerotermia…" }],
      },
      { name: "habitantes_municipio", label: "Habitantes de tu municipio", kind: "count", hint: "Varias deducciones exigen un municipio pequeño" },
      { name: "ejercicio_traslado_residencia", label: "Año en que te mudaste a él", kind: "year" },
      { name: "municipio_rural_o_despoblacion", label: "Vivo en un municipio pequeño, rural o en riesgo de despoblación", kind: "flag", hint: "Cada comunidad lo define a su manera; el resultado te dirá el criterio" },
    ],
  },
  {
    title: "Tu comunidad · salud, deporte y otros gastos",
    fields: [
      {
        name: "gastos_salud",
        label: "Gastos de salud (total)",
        kind: "money",
        hint: "Lo pagado y no cubierto por la sanidad pública ni por un seguro",
        parts: [
          { name: "gastos_honorarios_sanitarios", label: "Honorarios de médicos y otros sanitarios", kind: "money", hint: "Sin dentista ni salud mental" },
          { name: "gastos_salud_bucodental", label: "Dentista y ortodoncia", kind: "money" },
          { name: "gastos_salud_mental", label: "Psicología y psiquiatría", kind: "money" },
          { name: "gastos_optica", label: "Gafas, lentillas y prótesis", kind: "money" },
          { name: "gastos_enfermedades_raras", label: "Por una enfermedad rara", kind: "money" },
          { name: "gastos_ela", label: "Por ELA", kind: "money" },
        ],
      },
      {
        name: "gastos_deporte_cultura",
        label: "Deporte y cultura (total)",
        kind: "money",
        parts: [
          { name: "gastos_deporte", label: "Deporte", kind: "money", hint: "Cuotas de gimnasio, clubes o federaciones" },
          { name: "gastos_cultura", label: "Cultura", kind: "money", hint: "Entradas, abonos, cuotas de actividades culturales" },
        ],
      },
      { name: "gastos_veterinarios", label: "Gastos veterinarios", kind: "money" },
      { name: "adquisicion_vehiculo_electrico", label: "Compra de un vehículo eléctrico", kind: "money" },
    ],
  },
  {
    title: "Tu comunidad · donativos e inversiones",
    fields: [
      {
        name: "donativos_autonomicos",
        label: "Donativos a entidades de tu comunidad (total)",
        kind: "money",
        hint: "Cada comunidad incentiva destinos distintos: indica a qué fueron",
        parts: [
          { name: "donativos_medio_ambiente", label: "Medio ambiente", kind: "money" },
          { name: "donativos_cultura_patrimonio", label: "Cultura y patrimonio", kind: "money" },
          { name: "donativos_investigacion", label: "Investigación y universidades", kind: "money" },
          { name: "donativos_cooperacion_fines_sociales", label: "Cooperación y fines sociales", kind: "money", hint: "Desarrollo, pobreza, discapacidad" },
          { name: "donativos_lengua_propia", label: "Fomento de la lengua propia", kind: "money" },
          { name: "donativos_fundaciones_autonomicas", label: "Fundaciones de tu comunidad", kind: "money", hint: "Inscritas en su registro" },
        ],
      },
      { name: "inversion_empresas_nueva_creacion", label: "Inversión en empresas nuevas", kind: "money" },
      { name: "inversion_economia_social", label: "Inversión en economía social", kind: "money", hint: "Cooperativas, sociedades laborales…" },
    ],
  },
];
const MADRID_NAMES = new Set(MADRID_GROUP.fields.map((f) => f.name));
// Madrid: its own group first, then the rest without repeating what it already asks.
const MADRID_EXTRA_GROUPS: Group[] = REGIONAL_GROUPS.map((group) => ({
  title: group.title,
  fields: group.fields.filter((f) => !MADRID_NAMES.has(f.name)),
}));

// ---- Detail of each child --------------------------------------------------

// Expenses the editor offers per child (the calculator accepts any expense input per child;
// other ones already in a saved detail are kept and shown too).
const CHILD_EXPENSES: Array<{ name: string; label: string }> = [
  { name: "gastos_guarderia", label: "Guardería" },
  { name: "gastos_escolaridad", label: "Escolaridad" },
  { name: "gastos_libros_texto", label: "Libros y material" },
  { name: "gastos_ensenanza_idiomas", label: "Idiomas" },
  { name: "gastos_uniformes_escolares", label: "Uniformes" },
];

interface ChildRow {
  id: string;
  fecha: string; // AAAA-MM-DD, or ""
  edad: string; // age at 31 December; ignored when the date is given
  discapacidad: string;
  etapa: string;
  adopcion: string; // year of the adoption, or ""
  internacional: boolean;
  multiple: boolean;
  mesesGuarderia: string;
  gastos: Record<string, string>;
}
type ApiChild = Record<string, unknown>;

const emptyChild = (): ChildRow => ({ id: newId(), fecha: "", edad: "", discapacidad: "", etapa: "", adopcion: "", internacional: false, multiple: false, mesesGuarderia: "", gastos: {} });

const text = (value: unknown): string => (typeof value === "number" || typeof value === "string" ? String(value) : "");

/** The saved `descendientes` fact (JSON text of a list) as editor rows. */
function parseChildrenFact(facts: Fact[]): { rows: ChildRow[]; broken: boolean; saved: boolean } {
  const fact = facts.find((f) => f.name === "descendientes");
  if (!fact) return { rows: [], broken: false, saved: false };
  let list: unknown = fact.value;
  if (typeof list === "string") {
    try {
      list = JSON.parse(list);
    } catch {
      return { rows: [], broken: true, saved: true };
    }
  }
  if (!Array.isArray(list) || list.some((item) => typeof item !== "object" || item === null || Array.isArray(item))) return { rows: [], broken: true, saved: true };
  const rows = (list as Array<Record<string, unknown>>).map((item): ChildRow => {
    const gastos: Record<string, string> = {};
    if (typeof item.gastos === "object" && item.gastos !== null) {
      for (const [name, amount] of Object.entries(item.gastos as Record<string, unknown>)) gastos[name] = text(amount).replace(".", ",");
    }
    return {
      id: newId(),
      fecha: typeof item.fecha_nacimiento === "string" ? item.fecha_nacimiento : "",
      edad: typeof item.fecha_nacimiento === "string" ? "" : text(item.edad),
      discapacidad: text(item.grado_discapacidad),
      etapa: typeof item.etapa_educativa === "string" ? item.etapa_educativa : "",
      adopcion: text(item.ejercicio_adopcion),
      internacional: item.adopcion_internacional === true,
      multiple: item.parto_multiple === true,
      mesesGuarderia: text(item.meses_guarderia),
      gastos,
    };
  });
  return { rows, broken: false, saved: true };
}

/** Age reached at 31 December of `year`, or null while the row does not say. */
function childAge(row: ChildRow, year: number): number | null {
  if (/^\d{4}-\d{2}-\d{2}$/.test(row.fecha)) return year - Number(row.fecha.slice(0, 4));
  const edad = parseNumber(row.edad);
  return edad !== null && Number.isInteger(edad) ? edad : null;
}

/** What is wrong with a row, in words; null when it can be sent. */
function childProblem(row: ChildRow, year: number): string | null {
  const age = childAge(row, year);
  if (age === null) return "indica su fecha de nacimiento o la edad que cumple en el año.";
  if (age < 0) return `nació después de ${year}, el ejercicio que estás simulando.`;
  if (age > 130) return "revisa la fecha de nacimiento o la edad.";
  const whole = (raw: string, max: number) => {
    const n = parseNumber(raw);
    return raw.trim() === "" || (n !== null && Number.isInteger(n) && n >= 0 && n <= max);
  };
  if (!whole(row.discapacidad, 100)) return "el grado de discapacidad va de 0 a 100.";
  if (!whole(row.mesesGuarderia, 12)) return "los meses de guardería van de 0 a 12.";
  if (row.adopcion.trim() !== "") {
    const adopcion = parseNumber(row.adopcion);
    if (adopcion === null || !Number.isInteger(adopcion) || adopcion < year - age || adopcion > year) return `el año de adopción debe estar entre ${year - age} y ${year}.`;
  }
  for (const [name, raw] of Object.entries(row.gastos)) {
    const n = parseNumber(raw);
    if (raw.trim() !== "" && (n === null || n < 0)) return `revisa el importe de «${factLabel(name).toLowerCase()}».`;
  }
  return null;
}

/** A row as the API takes it (defaults left out, so equal details compare equal). */
function childToApi(row: ChildRow): ApiChild {
  const out: ApiChild = {};
  if (/^\d{4}-\d{2}-\d{2}$/.test(row.fecha)) out.fecha_nacimiento = row.fecha;
  else out.edad = parseNumber(row.edad);
  const discapacidad = parseNumber(row.discapacidad);
  if (discapacidad) out.grado_discapacidad = discapacidad;
  if (row.etapa) out.etapa_educativa = row.etapa;
  const adopcion = parseNumber(row.adopcion);
  if (adopcion !== null) {
    out.ejercicio_adopcion = adopcion;
    if (row.internacional) out.adopcion_internacional = true;
  }
  if (row.multiple) out.parto_multiple = true;
  const meses = parseNumber(row.mesesGuarderia);
  if (meses !== null) out.meses_guarderia = meses;
  const gastos: Record<string, number> = {};
  for (const name of Object.keys(row.gastos).sort()) {
    const n = parseNumber(row.gastos[name]);
    if (n) gastos[name] = n;
  }
  if (Object.keys(gastos).length > 0) out.gastos = gastos;
  return out;
}

/** The counts and per-child expense totals a complete detail determines. */
function derivedFromChildren(rows: ChildRow[], year: number): Record<string, string> {
  const out: Record<string, string> = {};
  if (rows.length === 0 || rows.some((row) => childProblem(row, year) !== null)) return out;
  const arrival = (row: ChildRow) => year - (parseNumber(row.adopcion) ?? year - (childAge(row, year) ?? 0));
  const count = (test: (row: ChildRow) => boolean) => String(rows.filter(test).length);
  out.num_descendientes = String(rows.length);
  out.num_descendientes_menores_3 = count((row) => (childAge(row, year) ?? 0) < 3);
  out.hijos_nacidos_en_el_ejercicio = count((row) => arrival(row) === 0);
  out.hijos_nacidos_ultimos_3_ejercicios = count((row) => arrival(row) <= 2);
  out.hijos_adoptados_en_el_ejercicio = count((row) => parseNumber(row.adopcion) === year);
  out.num_hijos_primer_ciclo_infantil = count((row) => row.etapa === "infantil_primer_ciclo");
  const totals: Record<string, number> = {};
  for (const row of rows) {
    for (const [name, raw] of Object.entries(row.gastos)) {
      const n = parseNumber(raw);
      if (n) totals[name] = (totals[name] ?? 0) + n;
    }
  }
  for (const [name, total] of Object.entries(totals)) out[name] = String(Math.round(total * 100) / 100).replace(".", ",");
  return out;
}

const COMUNIDADES = [
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
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
const FIELDS = [...GROUPS, MADRID_GROUP, ...REGIONAL_GROUPS].flatMap((g) => g.fields).flatMap((f) => [f, ...(f.parts ?? [])]);
const FLAGS = new Set(FIELDS.filter((f) => f.kind === "flag").map((f) => f.name));
const TRIBUTACION = [
  { value: "individual", label: "Individual" },
  { value: "conjunta_biparental", label: "Conjunta (matrimonio)" },
  { value: "conjunta_monoparental", label: "Conjunta (monoparental)" },
];
// What the "Probar" button on a lever sets.
const LEVER_PRESET: Record<string, { field: string; value: string }> = {
  plan_pensiones: { field: "aportacion_plan_pensiones", value: "1500" },
  donativos: { field: "donativos", value: "250" },
};

function parseNumber(raw: string): number | null {
  const cleaned = raw.trim().replace(/[€\s]/g, "").replace(/\.(?=\d{3}(\D|$))/g, "").replace(",", ".");
  if (!cleaned || cleaned === "-") return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
}

interface SimulatorPanelProps {
  open: boolean;
  onClose: () => void;
  caseId: string | null;
  facts: Fact[];
  taxPeriod: string | null;
  // Persist edited figures to the case (null removes the fact). Resolves true when saved.
  onSaveFact: (factName: string, value: number | string | boolean | null) => Promise<boolean>;
  busy: boolean;
}

const INPUT_CLASS = "w-full rounded-lg border bg-surface px-3 py-2 text-[16px] text-ink focus:outline-none sm:text-[14px]";
const SMALL_INPUT_CLASS = "w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-[16px] text-ink focus:border-primary focus:outline-none sm:text-[13.5px]";

export function SimulatorPanel({ open, onClose, caseId, facts, taxPeriod, onSaveFact, busy }: SimulatorPanelProps) {
  // Edits layered over the case facts: { field: raw text }. Empty = baseline.
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [tributacion, setTributacion] = useState("individual");
  // null = keep whatever the case says.
  const [comunidadEdit, setComunidadEdit] = useState<string | null>(null);
  // The detail of each child being edited; null = the one saved in the case.
  const [childrenEdit, setChildrenEdit] = useState<ChildRow[] | null>(null);
  // Breakdowns the user opened or closed by hand (otherwise open when they hold a figure).
  const [openParts, setOpenParts] = useState<Record<string, boolean>>({});
  const [simulation, setSimulation] = useState<Simulation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const requestSeq = useRef(0);

  const baseline = useMemo(() => {
    const map: Record<string, string> = {};
    for (const fact of facts) {
      if (typeof fact.value === "number" || typeof fact.value === "boolean") map[fact.name] = String(fact.value);
    }
    return map;
  }, [facts]);

  const savedComunidad = useMemo(() => {
    const value = facts.find((f) => f.name === "comunidad_autonoma")?.value;
    if (typeof value !== "string") return "";
    const saved = plain(value);
    const match = COMUNIDADES.find((c) => c.value && c.value !== "Otra" && (saved.includes(plain(c.value)) || saved.includes(plain(c.label))));
    return match ? match.value : "Otra";
  }, [facts]);

  const savedChildren = useMemo(() => parseChildrenFact(facts), [facts]);
  const children = childrenEdit ?? savedChildren.rows;
  const simulatedYear = simulation?.ejercicio;
  const year = simulatedYear ?? (taxPeriod && /^\d{4}/.test(taxPeriod) ? Number(taxPeriod.slice(0, 4)) : new Date().getFullYear());
  // Figures the detail of the children determines: shown read-only and never sent.
  const derived = useMemo(() => derivedFromChildren(children, year), [children, year]);
  const childProblems = useMemo(() => children.map((row) => childProblem(row, year)), [children, year]);

  const value = (name: string) => derived[name] ?? edits[name] ?? baseline[name] ?? "";
  // Totals smaller than their breakdown: the calculator would reject them, so they are not sent.
  const partsSum = (field: FieldDef) => (field.parts ?? []).reduce((sum, part) => sum + Math.max(0, parseNumber(value(part.name)) ?? 0), 0);
  const totalProblem = (field: FieldDef): string | null => {
    if (!field.parts) return null;
    const total = parseNumber(value(field.name)) ?? 0;
    const sum = partsSum(field);
    return total > 0 && sum > total + 0.005 ? `El desglose suma ${formatMoney(sum)}, más que este total (${formatMoney(total)}). Sube el total o corrige el desglose.` : null;
  };
  const invalid = childProblems.some((problem) => problem !== null) || FIELDS.some((field) => totalProblem(field) !== null);

  const cambios = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const [name, raw] of Object.entries(edits)) {
      if (name in derived) continue;
      if (FLAGS.has(name)) {
        if (raw !== (baseline[name] ?? "false")) out[name] = raw === "true";
        continue;
      }
      const n = parseNumber(raw);
      if (n !== null && String(n) !== baseline[name]) out[name] = n;
    }
    if (tributacion !== "individual") out.tributacion = tributacion;
    if (comunidadEdit !== null && comunidadEdit !== savedComunidad) out.comunidad_autonoma = comunidadEdit === "Otra" ? "otra comunidad" : comunidadEdit || null;
    if (childrenEdit !== null || savedChildren.broken) {
      const detail = children.map(childToApi);
      // A saved detail that cannot be read is always replaced, even by an empty one.
      if (savedChildren.broken || JSON.stringify(detail) !== JSON.stringify(savedChildren.rows.map(childToApi))) out.descendientes = detail;
    }
    return out;
  }, [edits, tributacion, baseline, comunidadEdit, savedComunidad, derived, childrenEdit, children, savedChildren]);
  const comunidad = comunidadEdit ?? savedComunidad;
  const groups = /madrid/i.test(comunidad) ? [...GROUPS, MADRID_GROUP, ...MADRID_EXTRA_GROUPS] : comunidad && comunidad !== "Otra" ? [...GROUPS, ...REGIONAL_GROUPS] : GROUPS;
  const changed = Object.keys(cambios).length > 0;
  const cambiosKey = JSON.stringify(cambios);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Debounced, last-request-wins simulation. Nothing is sent while a figure is marked as
  // inconsistent: the last good result stays on screen.
  useEffect(() => {
    if (!open || invalid) return;
    const seq = ++requestSeq.current;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      const requestYear = taxPeriod ? Number(taxPeriod.slice(0, 4)) : null;
      simulate({
        caseId,
        ejercicio: caseId ? null : requestYear,
        escenarios: [
          { nombre: "Tu expediente", cambios: {} },
          { nombre: "Simulación", cambios: JSON.parse(cambiosKey) as Record<string, unknown> },
        ],
        leversForLastScenario: true,
        signal: controller.signal,
      }).then(
        (result) => {
          if (seq !== requestSeq.current) return;
          setSimulation(result);
          setError(null);
        },
        (err: unknown) => {
          if (controller.signal.aborted || seq !== requestSeq.current) return;
          setError(err instanceof ApiError ? err.message : "No se pudo calcular la simulación.");
        },
      );
    }, 300);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [open, caseId, taxPeriod, cambiosKey, facts, invalid]);

  if (!open) return null;

  const base = simulation?.escenarios[0];
  const sim = simulation?.escenarios[1] ?? base;
  const resultado = sim ? Number(sim.resultado.resultado) : 0;
  const ahorro = simulation?.escenarios[1] ? Number(simulation.escenarios[1].ahorro_frente_a_base) : 0;
  const levers = (simulation?.beneficios ?? []).filter((b) => b.estado === "disponible" || b.estado === "por_confirmar");
  const hasIncome = FIELDS.some((f) => (edits[f.name] ?? baseline[f.name] ?? "") !== "" && f.name.startsWith("rendimiento"));

  async function saveAll() {
    setSaving(true);
    try {
      for (const [name, changedValue] of Object.entries(cambios)) {
        if (name === "descendientes") {
          const detail = changedValue as ApiChild[];
          // An empty detail removes the saved one; otherwise it is stored as JSON text.
          if (detail.length === 0 && !savedChildren.saved) continue;
          if (!(await onSaveFact(name, detail.length > 0 ? JSON.stringify(detail) : null))) return;
          continue;
        }
        const saveable = typeof changedValue === "number" || typeof changedValue === "boolean" || (name === "comunidad_autonoma" && typeof changedValue === "string");
        if (!saveable) continue;
        if (!(await onSaveFact(name, changedValue))) return;
      }
      setEdits({});
      setComunidadEdit(null);
      setChildrenEdit(null);
    } finally {
      setSaving(false);
    }
  }

  function lever(benefit: Benefit) {
    const preset = LEVER_PRESET[benefit.id];
    return (
      <li key={benefit.id} className="rounded-xl border border-line bg-surface px-3.5 py-3">
        <div className="flex items-start justify-between gap-3">
          <p className="text-[14px] font-semibold text-ink">{benefit.nombre}</p>
          {benefit.ahorro_estimado && Number(benefit.ahorro_estimado) > 0 && (
            <span className="flex-shrink-0 rounded-pill bg-grounding-cited-bg px-2 py-0.5 text-[12px] font-bold text-grounding-cited-fg">
              hasta −{formatMoney(benefit.ahorro_estimado)}
            </span>
          )}
        </div>
        <p className="mt-1 text-[13px] leading-snug text-ink-soft">{benefit.detalle}</p>
        <p className="mt-1.5 text-[11.5px] text-muted">
          {benefit.referencia}
          {!benefit.verificado && " · cifras pendientes de contrastar"}
        </p>
        {preset && (
          <button
            type="button"
            onClick={() => setEdits((e) => ({ ...e, [preset.field]: preset.value }))}
            className="mt-2 rounded-lg border border-line px-2.5 py-1 text-[12.5px] font-semibold text-primary-dark transition hover:border-primary"
          >
            Probar en la simulación
          </button>
        )}
      </li>
    );
  }

  function renderField(field: FieldDef) {
    const current = value(field.name);
    const dirty = field.name in cambios;
    if (field.kind === "flag") {
      return (
        <label key={field.name} className="flex items-start gap-2.5 sm:col-span-2">
          <input
            type="checkbox"
            checked={current === "true"}
            onChange={(e) => setEdits((prev) => ({ ...prev, [field.name]: String(e.target.checked) }))}
            className="mt-0.5 h-4 w-4 accent-[rgb(var(--primary))]"
          />
          <span>
            <span className={clsx("text-[13.5px]", dirty ? "font-semibold text-primary-dark" : "text-ink")}>{field.label}</span>
            {field.hint && <span className="block text-[11.5px] leading-snug text-muted">{field.hint}</span>}
          </span>
        </label>
      );
    }
    const fromChildren = field.name in derived;
    const sum = partsSum(field);
    const problem = totalProblem(field);
    const autoTotal = !!field.parts && sum > 0 && !(parseNumber(current) ?? 0);
    const input = (
      <label className="block">
        <span className="text-[12.5px] text-ink-soft">{field.label}</span>
        <span className="relative mt-1 block">
          <input
            value={current}
            onChange={(e) => setEdits((prev) => ({ ...prev, [field.name]: e.target.value }))}
            readOnly={fromChildren}
            aria-invalid={problem ? true : undefined}
            aria-describedby={problem ? `problem-${field.name}` : undefined}
            inputMode={field.kind === "money" ? "decimal" : "numeric"}
            maxLength={field.kind === "year" ? 4 : undefined}
            placeholder={autoTotal ? String(Math.round(sum * 100) / 100).replace(".", ",") : field.kind === "year" ? "AAAA" : "0"}
            className={clsx(
              INPUT_CLASS,
              field.kind === "money" && "pr-7",
              fromChildren && "cursor-default bg-line/40 text-ink-soft",
              problem ? "border-risk-high-fg" : dirty ? "border-primary" : "border-line focus:border-primary",
            )}
          />
          {field.kind === "money" && <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-[13px] text-muted">€</span>}
        </span>
        {fromChildren ? (
          <span className="mt-1 block text-[11.5px] leading-snug text-muted">Sale del detalle de cada hijo.</span>
        ) : problem ? (
          <span id={`problem-${field.name}`} role="alert" className="mt-1 block text-[11.5px] font-semibold leading-snug text-risk-high-fg">
            {problem}
          </span>
        ) : autoTotal ? (
          <span className="mt-1 block text-[11.5px] leading-snug text-muted">Sin total, se toma la suma del desglose: {formatMoney(sum)}.</span>
        ) : (
          field.hint && <span className="mt-1 block text-[11.5px] leading-snug text-muted">{field.hint}</span>
        )}
      </label>
    );
    if (!field.parts) return <div key={field.name}>{input}</div>;
    const isOpen = openParts[field.name] ?? (sum > 0 || problem !== null);
    return (
      <div key={field.name} className="sm:col-span-2">
        {input}
        <details open={isOpen} className="group/parts mt-1.5">
          <summary
            // Opened and closed through state: a re-render (a new result arriving) must not undo the click.
            onClick={(e) => {
              e.preventDefault();
              setOpenParts((prev) => ({ ...prev, [field.name]: !isOpen }));
            }}
            className="flex cursor-pointer list-none items-center gap-1 text-[12.5px] font-semibold text-primary-dark [&::-webkit-details-marker]:hidden"
          >
            <ChevronDown size={14} aria-hidden className="transition group-open/parts:rotate-180" />
            Desglose por tipo{sum > 0 ? ` · ${formatMoney(sum)}` : " (opcional)"}
          </summary>
          <div className="mt-2 grid gap-3 border-l-2 border-line pl-3 sm:grid-cols-2">{field.parts.map(renderField)}</div>
          <p className="mt-1.5 pl-3 text-[11.5px] leading-snug text-muted">Algunas deducciones solo admiten una parte de estos gastos. Cada importe va en una sola casilla.</p>
        </details>
      </div>
    );
  }

  const setChild = (id: string, patch: Partial<ChildRow>) => setChildrenEdit(children.map((row) => (row.id === id ? { ...row, ...patch } : row)));

  function renderChild(row: ChildRow, index: number) {
    const problem = childProblems[index];
    const hasDate = row.fecha !== "";
    const age = childAge(row, year);
    const expenses = [...CHILD_EXPENSES, ...Object.keys(row.gastos).filter((name) => !CHILD_EXPENSES.some((e) => e.name === name)).map((name) => ({ name, label: factLabel(name) }))];
    const small = (label: string, node: ReactNode) => (
      <label className="block">
        <span className="text-[12px] text-ink-soft">{label}</span>
        <span className="mt-0.5 block">{node}</span>
      </label>
    );
    return (
      <li key={row.id} className="rounded-xl border border-line bg-surface px-3 py-3">
        <div className="mb-2 flex items-center justify-between">
          <p className="text-[13px] font-semibold text-ink">
            Hijo {index + 1}
            {age !== null && age >= 0 && <span className="font-normal text-muted"> · {age === 0 ? `nacido en ${year}` : `${age} ${age === 1 ? "año" : "años"} a 31-12-${year}`}</span>}
          </p>
          <button type="button" onClick={() => setChildrenEdit(children.filter((r) => r.id !== row.id))} aria-label={`Quitar el hijo ${index + 1}`} title="Quitar" className="rounded-lg p-1.5 text-muted transition hover:bg-risk-high-bg hover:text-risk-high-fg">
            <Trash2 size={15} />
          </button>
        </div>
        <div className="grid grid-cols-2 gap-2.5">
          {small("Fecha de nacimiento", <input type="date" value={row.fecha} max={`${year}-12-31`} onChange={(e) => setChild(row.id, { fecha: e.target.value })} className={SMALL_INPUT_CLASS} />)}
          {small(
            `o edad a 31-12-${year}`,
            <input
              value={hasDate ? (age !== null && age >= 0 ? String(age) : "") : row.edad}
              onChange={(e) => setChild(row.id, { edad: e.target.value })}
              readOnly={hasDate}
              inputMode="numeric"
              placeholder="0 si nació este año"
              className={clsx(SMALL_INPUT_CLASS, hasDate && "cursor-default bg-line/40 text-ink-soft")}
            />,
          )}
          {small(
            "Etapa educativa",
            <select value={row.etapa} onChange={(e) => setChild(row.id, { etapa: e.target.value })} className={SMALL_INPUT_CLASS}>
              <option value="">Sin indicar</option>
              {Object.entries(ETAPA_LABEL).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>,
          )}
          {small("Discapacidad (%)", <input value={row.discapacidad} onChange={(e) => setChild(row.id, { discapacidad: e.target.value })} inputMode="numeric" placeholder="0" className={SMALL_INPUT_CLASS} />)}
          {small("Meses en guardería este año", <input value={row.mesesGuarderia} onChange={(e) => setChild(row.id, { mesesGuarderia: e.target.value })} inputMode="numeric" placeholder="—" className={SMALL_INPUT_CLASS} />)}
          {small("Año de adopción (si es adoptado)", <input value={row.adopcion} onChange={(e) => setChild(row.id, { adopcion: e.target.value })} inputMode="numeric" maxLength={4} placeholder="—" className={SMALL_INPUT_CLASS} />)}
        </div>
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1.5">
          <label className="flex items-center gap-2 text-[12.5px] text-ink">
            <input type="checkbox" checked={row.multiple} onChange={(e) => setChild(row.id, { multiple: e.target.checked })} className="h-4 w-4 accent-[rgb(var(--primary))]" />
            Parto o adopción múltiple
          </label>
          {row.adopcion.trim() !== "" && (
            <label className="flex items-center gap-2 text-[12.5px] text-ink">
              <input type="checkbox" checked={row.internacional} onChange={(e) => setChild(row.id, { internacional: e.target.checked })} className="h-4 w-4 accent-[rgb(var(--primary))]" />
              Adopción internacional
            </label>
          )}
        </div>
        <p className="mb-1 mt-3 text-[11px] font-semibold uppercase tracking-[0.1em] text-muted">Gastos de este hijo en el año</p>
        <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
          {expenses.map((expense) => (
            <label key={expense.name} className="block">
              <span className="text-[12px] text-ink-soft">{expense.label}</span>
              <span className="relative mt-0.5 block">
                <input
                  value={row.gastos[expense.name] ?? ""}
                  onChange={(e) => setChild(row.id, { gastos: { ...row.gastos, [expense.name]: e.target.value } })}
                  inputMode="decimal"
                  placeholder="0"
                  aria-label={`${expense.label} del hijo ${index + 1}`}
                  className={clsx(SMALL_INPUT_CLASS, "pr-6")}
                />
                <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-[12px] text-muted">€</span>
              </span>
            </label>
          ))}
        </div>
        {problem && (
          <p role="alert" className="mt-2 text-[12px] font-semibold leading-snug text-risk-high-fg">
            Hijo {index + 1}: {problem}
          </p>
        )}
      </li>
    );
  }

  const childrenEditor = (
    <div className="sm:col-span-2">
      <p className="text-[12.5px] font-semibold text-ink">Detalle de cada hijo</p>
      <p className="mt-0.5 text-[11.5px] leading-snug text-muted">
        Opcional. Con él no hace falta contar los hijos a mano y se aplican bien las deducciones que dependen de la edad, la etapa educativa, la discapacidad o los gastos de cada hijo (los
        máximos «por hijo» se calculan hijo a hijo).
      </p>
      {savedChildren.broken && childrenEdit === null && (
        <p role="alert" className="mt-1.5 text-[12px] font-semibold leading-snug text-risk-high-fg">
          El detalle guardado en tu expediente no se puede leer. Vuelve a introducirlo y guárdalo.
        </p>
      )}
      {children.length > 0 && <ul className="mt-2 space-y-2.5">{children.map(renderChild)}</ul>}
      <button
        type="button"
        onClick={() => setChildrenEdit([...children, emptyChild()])}
        disabled={children.length >= 20}
        className="mt-2 inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-[12.5px] font-semibold text-primary-dark transition hover:border-primary disabled:cursor-not-allowed disabled:opacity-50"
      >
        <Plus size={14} aria-hidden />
        {children.length === 0 ? "Añadir el detalle de un hijo" : "Añadir otro hijo"}
      </button>
    </div>
  );

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button type="button" aria-label="Cerrar el simulador" className="absolute inset-0 bg-ink/40" onClick={onClose} />
      <aside role="dialog" aria-modal="true" aria-label="Simulador de IRPF" className="relative flex h-full w-full max-w-[520px] flex-col border-l border-line bg-paper shadow-pop">
        <header className="flex items-center justify-between border-b border-line px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 font-serif text-[19px] font-bold text-ink">
              <Calculator size={18} aria-hidden className="text-primary-dark" />
              Simulador de IRPF{simulation ? ` ${simulation.ejercicio}` : ""}
            </h2>
            <p className="text-[12.5px] text-muted">Cambia una cifra y mira cómo cambia tu declaración</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Cerrar" className="rounded-lg p-2 text-muted hover:bg-line/60 hover:text-ink">
            <X size={18} />
          </button>
        </header>

        <div className="flex-1 space-y-5 overflow-y-auto px-5 pb-5 pt-3">
          {error && !invalid && (
            <p role="alert" className="flex gap-2 rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft">
              <TriangleAlert size={15} aria-hidden className="mt-0.5 flex-shrink-0 text-risk-high-fg" />
              <span>
                {error}
                {simulation && <span className="block text-[12px] text-muted">El resultado de abajo es el del último cálculo válido.</span>}
              </span>
            </p>
          )}

          {sim && (
            <section aria-live="polite" className="sticky top-0 z-10 rounded-xl border border-line bg-surface px-4 py-4 shadow-pop">
              <p className="text-[12px] font-semibold uppercase tracking-[0.1em] text-muted">Resultado estimado</p>
              <p className={clsx("mt-1 font-serif text-[30px] font-bold leading-none", resultado > 0 ? "text-ink" : "text-grounding-cited-fg")}>
                {formatMoney(Math.abs(resultado))}
                <span className="ml-2 text-[14px] font-semibold text-ink-soft">{resultado > 0 ? "a pagar" : resultado < 0 ? "a devolver" : ""}</span>
              </p>
              <p className="mt-2 text-[13px] text-ink-soft">
                Cuota del año: {formatMoney(sim.resultado.cuota_liquida)} · tipo medio {sim.resultado.tipo_medio_efectivo.replace(".", ",")} %
              </p>
              {invalid ? (
                <p role="status" className="mt-2 flex gap-2 rounded-lg bg-risk-high-bg px-3 py-1.5 text-[13px] font-semibold text-risk-high-fg">
                  <TriangleAlert size={15} aria-hidden className="mt-0.5 flex-shrink-0" />
                  Hay datos que no cuadran (marcados en rojo más abajo). Este resultado no incluye tus últimos cambios.
                </p>
              ) : (
                changed && (
                  <p className={clsx("mt-2 rounded-lg px-3 py-1.5 text-[13.5px] font-semibold", ahorro > 0 ? "bg-grounding-cited-bg text-grounding-cited-fg" : ahorro < 0 ? "bg-grounding-uncited-bg text-grounding-uncited-fg" : "bg-grounding-abstain-bg text-grounding-abstain-fg")}>
                    {ahorro > 0 ? `Con estos cambios pagas ${formatMoney(ahorro)} menos` : ahorro < 0 ? `Con estos cambios pagas ${formatMoney(-ahorro)} más` : "Estos cambios no alteran el resultado"}
                    {base && <span className="font-normal"> (antes: {formatMoney(base.resultado.resultado)})</span>}
                  </p>
                )
              )}
              {!hasIncome && <p className="mt-2 text-[12.5px] text-muted">Introduce al menos tu salario bruto para obtener una estimación.</p>}
            </section>
          )}

          {groups.map((group) => (
            <fieldset key={group.title}>
              <legend className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">{group.title}</legend>
              <div className="grid gap-3 sm:grid-cols-2">
                {group.fields.map(renderField)}
                {group.title === "Familia" && childrenEditor}
                {group.title === "Familia" && (
                  <label className="block sm:col-span-2">
                    <span className="text-[12.5px] text-ink-soft">Comunidad autónoma</span>
                    <select value={comunidad} onChange={(e) => setComunidadEdit(e.target.value)} className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-[16px] text-ink focus:border-primary focus:outline-none sm:text-[14px]">
                      {COMUNIDADES.map((c) => (
                        <option key={c.value} value={c.value}>
                          {c.label}
                        </option>
                      ))}
                    </select>
                    <span className="mt-1 block text-[11.5px] leading-snug text-muted">
                      {comunidad === "Madrid"
                        ? "Se aplican la escala, los mínimos y varias deducciones de Madrid."
                        : comunidad === "Otra"
                          ? "Los territorios forales tienen su propio impuesto y Ceuta y Melilla una deducción específica: aquí la parte autonómica es solo una aproximación."
                          : comunidad
                            ? "Se aplican la escala, los mínimos y las deducciones más habituales de tu comunidad (abajo)."
                            : "Sin comunidad, la parte autonómica se aproxima con la escala estatal."}
                    </span>
                  </label>
                )}
                {group.title === "Familia" && (
                  <label className="block sm:col-span-2">
                    <span className="text-[12.5px] text-ink-soft">Tipo de declaración</span>
                    <select value={tributacion} onChange={(e) => setTributacion(e.target.value)} className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-[16px] text-ink focus:border-primary focus:outline-none sm:text-[14px]">
                      {TRIBUTACION.map((t) => (
                        <option key={t.value} value={t.value}>
                          {t.label}
                        </option>
                      ))}
                    </select>
                    {tributacion !== "individual" && (
                      <span className="mt-1 block text-[11.5px] leading-snug text-muted">
                        En conjunta, las cifras deben sumar las rentas de toda la unidad familiar.
                      </span>
                    )}
                  </label>
                )}
              </div>
            </fieldset>
          ))}

          {changed && (
            <div className="flex flex-wrap gap-2">
              {caseId && (
                <button type="button" onClick={() => void saveAll()} disabled={saving || busy || invalid} className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-white transition hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50">
                  <Save size={14} aria-hidden />
                  {saving ? "Guardando…" : "Guardar en mi expediente"}
                </button>
              )}
              <button
                type="button"
                onClick={() => {
                  setEdits({});
                  setTributacion("individual");
                  setComunidadEdit(null);
                  setChildrenEdit(null);
                }}
                className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-[13px] font-semibold text-ink-soft hover:bg-line/60"
              >
                <RotateCcw size={14} aria-hidden />
                Deshacer cambios
              </button>
            </div>
          )}

          {levers.length > 0 && (
            <section>
              <h3 className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
                <Lightbulb size={13} aria-hidden />
                Formas de pagar menos
              </h3>
              <ul className="space-y-2">{levers.map(lever)}</ul>
            </section>
          )}

          {sim && (
            <details className="group rounded-xl border border-line bg-surface">
              <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-2.5 text-[13px] font-semibold text-ink-soft [&::-webkit-details-marker]:hidden">
                Desglose del cálculo
                <ChevronDown size={16} aria-hidden className="text-muted transition group-open:rotate-180" />
              </summary>
              <table className="w-full border-t border-line text-[12.5px]">
                <tbody>
                  {sim.resultado.lineas.map((line, i) => (
                    <tr key={i} className="border-b border-line last:border-0">
                      <td className="px-4 py-1.5 text-ink-soft">
                        {line.concepto}
                        {line.referencia && <span className="block text-[11px] text-muted">{line.referencia}</span>}
                      </td>
                      <td className="whitespace-nowrap px-4 py-1.5 text-right font-mono tabular-nums text-ink">{formatMoney(line.importe)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}

          {sim && sim.resultado.avisos.length > 0 && (
            <details open className="group rounded-xl border border-grounding-uncited-bd bg-grounding-uncited-bg">
              <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-2.5 text-[13px] font-semibold text-grounding-uncited-fg [&::-webkit-details-marker]:hidden">
                Qué no incluye esta estimación ({sim.resultado.avisos.length})
                <ChevronDown size={16} aria-hidden className="transition group-open:rotate-180" />
              </summary>
              <ul className="list-disc space-y-1.5 px-4 pb-3 pl-8 text-[12.5px] leading-snug text-ink-soft">
                {sim.resultado.avisos.map((aviso, i) => (
                  <li key={i}>{aviso}</li>
                ))}
              </ul>
            </details>
          )}
        </div>

        <footer className="border-t border-line px-5 py-3 text-[11.5px] leading-relaxed text-muted">
          Cálculo determinista con los parámetros estatales del ejercicio. No sustituye al borrador de la Agencia Tributaria.
          {sim && <span className="block">{sim.resultado.fuente}.</span>}
        </footer>
      </aside>
    </div>
  );
}
