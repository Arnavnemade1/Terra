import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import cesium from 'vite-plugin-cesium';

export default defineConfig({
  plugins: [react(), cesium()],
  // No secret is eligible for client-side environment substitution.
  envPrefix: 'TERRA_PUBLIC_',

  server: { port: 5173, strictPort: true, host: '127.0.0.1', fs: { strict: true, deny: ['**/.env', '**/.env.*', '**/.git/**', '**/.data/**', '**/.venv/**', '**/backend/**', '**/scripts/**', '**/tests/**', '**/*.pem', '**/*.key'] }, proxy: { '/api': `http://127.0.0.1:${process.env.TERRA_API_PORT || 8000}` } },
  build: { chunkSizeWarningLimit: 900 },
});
