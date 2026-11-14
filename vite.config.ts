import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import cesium from 'vite-plugin-cesium';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';

const src = fileURLToPath(new URL('./src', import.meta.url));
const page = (name: string) => fileURLToPath(new URL(name, import.meta.url));

export default defineConfig({
  plugins: [react(), cesium(), tailwindcss()],
  resolve: { alias: { '@': src } },
  // No secret is eligible for client-side environment substitution.
  envPrefix: 'TERRA_PUBLIC_',

  server: { port: 5173, strictPort: true, host: '127.0.0.1', fs: { strict: true, deny: ['**/.env', '**/.env.*', '**/.git/**', '**/.data/**', '**/.venv/**', '**/backend/**', '**/scripts/**', '**/tests/**', '**/*.pem', '**/*.key'] }, proxy: { '/api': `http://127.0.0.1:${process.env.TERRA_API_PORT || 8000}` } },
  // Two pages: index.html (landing) and app.html (the satellite map).
  build: { chunkSizeWarningLimit: 900, rollupOptions: { input: { index: page('index.html'), app: page('app.html') } } },
});
