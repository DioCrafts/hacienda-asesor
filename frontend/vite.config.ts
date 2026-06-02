import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Vite proxies /api/* to the FastAPI backend so the React client and
// the Python API can share the same origin in development (no CORS
// preflight, no environment-specific URL). Set ``VITE_API_BASE`` if
// you need to override.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
