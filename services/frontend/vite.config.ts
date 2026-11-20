import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The backend counts sign-in attempts by client address and checks Host and Origin against KYAPEHNU_PUBLIC_URL.
// `xfwd` adds X-Forwarded-For so the backend sees the real peer; `changeOrigin: false` keeps the browser's Host
// (localhost:5173) so the same-origin check passes.
const backend = { target: "http://127.0.0.1:8000", changeOrigin: false, xfwd: true };

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  server: { proxy: { "/api": backend, "/images": backend } },
});
