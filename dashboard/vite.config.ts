import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative base so the built bundle works from any mount point or origin.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    // `npm run dev` proxies to the FastAPI server so the dev build is also same-origin.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});