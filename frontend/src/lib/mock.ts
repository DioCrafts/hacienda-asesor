// Static UI copy: starter prompts and the topics the corpus covers. These are
// presentation strings — NOT tax data. Anything the assistant presents as an
// answer, a citation or an obligation must come from the backend.

export const TOPICS = ["IRPF", "IVA", "Autónomos", "Modelo 720", "Sucesiones y donaciones", "Plusvalía"] as const;

export const STARTER_QUESTIONS = [
  {
    title: "Venta de vivienda",
    prompt: "He vendido mi vivienda habitual y quiero comprar otra. ¿Qué plazo tengo para reinvertir y no tributar?",
  },
  {
    title: "Empezar como autónomo",
    prompt: "Voy a darme de alta como autónomo. ¿Qué modelos tengo que presentar y con qué plazos?",
  },
  {
    title: "Bienes en el extranjero",
    prompt: "Tengo una cuenta bancaria en otro país. ¿A partir de qué importe debo presentar el modelo 720?",
  },
  {
    title: "Donación de padres a hijos",
    prompt: "Mis padres quieren donarme dinero para comprar un piso. ¿Cómo tributa y qué plazo hay para declararlo?",
  },
] as const;
