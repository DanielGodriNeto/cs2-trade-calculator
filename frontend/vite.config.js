import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Python server (web/server.py) serves the built bundle from frontend/dist/ as static
// files, falling back to index.html for any non-/api/ path (SPA client-side routing). `base`
// is relative so asset URLs work regardless of the mount path. In dev, `npm run dev` runs Vite
// on :5173 and proxies /api to the Python server on :5000, so you get HMR against live data.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:5000",
    },
  },
});
