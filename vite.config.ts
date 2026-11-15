import { defineConfig, type HtmlTagDescriptor, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import cesium from 'vite-plugin-cesium';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';

const src = fileURLToPath(new URL('./src', import.meta.url));
const page = (name: string) => fileURLToPath(new URL(name, import.meta.url));

// vite-plugin-cesium adds Cesium.js (6.1 MB, 1.8 MB gzipped, a classic render-blocking script) and widgets.css to
// every HTML page. Only the map (app.html) uses Cesium; the landing's globe is three.js. On app.html the script is
// deferred: deferred scripts run in document order and the plugin prepends it to <head>, so it still runs before
// the app's module script (which reads the global Cesium), while the page can paint during the download.
const cesiumPlugin = cesium({ devMinifyCesium: true }) as Plugin & { transformIndexHtml: () => HtmlTagDescriptor[] };
const cesiumForMap: Plugin = {
  ...cesiumPlugin,
  transformIndexHtml(_html, ctx) {
    if (!ctx.filename.endsWith('app.html')) return [];
    return cesiumPlugin.transformIndexHtml().map(tag => tag.tag === 'script' ? { ...tag, attrs: { ...tag.attrs, defer: true } } : tag);
  },
};

export default defineConfig({
  plugins: [react(), cesiumForMap, tailwindcss()],
  resolve: { alias: { '@': src } },
  // No secret is eligible for client-side environment substitution.
  envPrefix: 'TERRA_PUBLIC_',

  // The API, Temporal and the tile cache write into .data constantly; without ignoring it (and the other non-web
  // trees) the watcher treated every write as a change and reloaded the page every few seconds.
  server: { port: 5173, strictPort: true, host: '127.0.0.1', watch: { ignored: ['**/.data/**', '**/.venv/**', '**/backend/**', '**/scripts/**', '**/tests/**', '**/dist/**', '**/docs/**', '**/*.db', '**/*.db-*', '**/*.log'] }, fs: { strict: true, deny: ['**/.env', '**/.env.*', '**/.git/**', '**/.data/**', '**/.venv/**', '**/backend/**', '**/scripts/**', '**/tests/**', '**/*.pem', '**/*.key'] }, proxy: { '/api': `http://127.0.0.1:${process.env.TERRA_API_PORT || 8000}` } },
  // Two pages: index.html (landing) and app.html (the satellite map).
  build: { chunkSizeWarningLimit: 900, rollupOptions: { input: { index: page('index.html'), app: page('app.html') } } },
});
