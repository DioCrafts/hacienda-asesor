# React + TypeScript + Vite

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...

      // Remove tseslint.configs.recommended and replace with this
      tseslint.configs.recommendedTypeChecked,
      // Alternatively, use this for stricter rules
      tseslint.configs.strictTypeChecked,
      // Optionally, add this for stylistic rules
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```

You can also install [eslint-plugin-react-x](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x'
import reactDom from 'eslint-plugin-react-dom'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs['recommended-typescript'],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```


## Qué hace la interfaz

- **Una llamada por turno a `/agent/stream`** (NDJSON): mientras el agente trabaja se muestran en vivo sus
  búsquedas, la verificación y las correcciones; el botón de enviar pasa a ser *Detener*.
- **Citas navegables**: cada `[F#]` de la respuesta es un chip que salta a su tarjeta de fuente (autoridad,
  referencia, fecha, carácter vinculante y pasaje literal).
- **Veredicto y comprobación afirmación por afirmación**, con el motivo cuando algo no queda respaldado.
- **Borrador en vivo**: la respuesta se ve escribirse token a token, marcada como *sin verificar* hasta que
  pasa la comprobación; si el agente la reescribe, el borrador se reinicia.
- **Tu expediente**: panel con los datos que el asesor ha anotado del usuario; cada uno se puede corregir en
  el sitio (`PUT /cases/{id}/facts/{name}`) o eliminar con confirmación (`DELETE`).
- **Historial real** en `localStorage` (`src/lib/storage.ts`): sobrevive a recargas y se puede borrar.
- **Tema claro/oscuro** con tokens CSS (`src/index.css` + `tailwind.config.js`) y diseño adaptado a móvil.
- **Simulador** (botón de la barra superior): calculadora determinista (`POST /simulate`, sin modelo) que
  recalcula mientras se teclea, compara con el expediente guardado, lista las formas de pagar menos con su
  ahorro y permite guardar las cifras en el expediente.
- **Próximos plazos** en el expediente (`GET /cases/{id}/calendar`), según el perfil.
- **Leer un documento** (`POST /cases/{id}/documents`): propone datos con el pasaje literal del que salen y solo
  guarda los que el usuario confirma.
