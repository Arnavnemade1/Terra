import React from 'react';
import ReactDOM from 'react-dom/client';
import '@fontsource-variable/archivo/wdth.css';
import '@fontsource-variable/newsreader/opsz.css';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/ibm-plex-mono/500.css';
import 'cesium/Build/Cesium/Widgets/widgets.css';
import './styles.css';
import './report.css';
import App from './App';
import { configureRequests, tilesReady } from './tiles';

// Tile hosts are settled first (a loopback probe, at most 1.5 s) so every map layer is created with them.
configureRequests();
void tilesReady.then(() => ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>));
