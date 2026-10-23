import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    allowedHosts: ['localhost', ...(process.env.GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN
      ? ['.' + process.env.GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN] : [])],
    proxy: {'/api': {target: process.env.VITE_API_TARGET ?? 'http://localhost:8000', changeOrigin: true}},
  },
});
