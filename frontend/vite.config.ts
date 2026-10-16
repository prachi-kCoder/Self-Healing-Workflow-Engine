import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Browser requests stay same-origin. In Compose, point the proxy at the API
// container; for local development it defaults to localhost:8000.
const apiTarget = process.env.VITE_API_PROXY_TARGET || 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  test: { environment: 'jsdom' },
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    proxy: { '/api': { target: apiTarget, changeOrigin: true } },
  },
  preview: {
    host: '0.0.0.0',
    port: 4173,
    strictPort: true,
    proxy: { '/api': { target: apiTarget, changeOrigin: true } },
  },
});
