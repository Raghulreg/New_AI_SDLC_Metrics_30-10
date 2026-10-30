import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The backend holds every credential; the browser only talks to /api.
export default defineConfig({
  plugins: [react()],
  envPrefix: "SDLC_PUBLIC_",
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
});
