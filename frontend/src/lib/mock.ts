// UI labels and demo affordances for the sidebar/composer. These are
// presentation strings (topics, suggested prompts) and placeholder recent
// cases — NOT tax data. Anything the assistant presents as an answer, a
// citation or an obligation must come from the backend, never from here.

export const MOCK_RECENT_CASES = [
  { id: "1", title: "IRPF residente, un pagador", active: true },
  { id: "2", title: "Modelos de autónomo en Madrid", active: false },
  { id: "3", title: "IVA en facturas intracomunitarias", active: false },
  { id: "4", title: "Modelo 720 bienes en el extranje…", active: false },
];

export const TOPICS = ["IRPF", "IVA", "Autónomos", "Modelo 720", "ISD"] as const;

export const SUGGESTED_QUESTIONS = [
  "¿Qué modelos presento como autónomo?",
  "Plazos de la Renta 2024",
  "¿Debo declarar criptomonedas?",
] as const;
