import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import ui from './ui-text.json';
import './styles.css';
document.title = ui.brand;
createRoot(document.getElementById('root')).render(<App />);
