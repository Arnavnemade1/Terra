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

// The shell never waits on a loopback probe; the map starts after host discovery.
configureRequests();
void tilesReady;
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
