import { useEffect, useState, useCallback, useRef } from 'react';
import { TopBar } from './components/TopBar';
import { Map } from './components/Map';
import { SitePriority } from './components/SitePriority';
import { SiteDetail } from './components/SiteDetail';
import { Operations } from './components/Operations';
import { ContainerSummaryPanel } from './components/ContainerSummary/ContainerSummaryPanel';
import { useAppStore } from './stores/appStore';
import 'leaflet/dist/leaflet.css';

const PANEL_MIN = 340;
const PANEL_DEFAULT = 380;
const LS_KEY = 'gasum-right-panel-width';

function App() {
  const { loadSites, selectedSiteId } = useAppStore();

  // Resizable right panel width
  const [panelWidth, setPanelWidth] = useState(() => {
    const saved = localStorage.getItem(LS_KEY);
    return saved ? Math.max(PANEL_MIN, Number(saved)) : PANEL_DEFAULT;
  });
  const dragging = useRef(false);

  // Load sites exactly once on mount.
  // mountedRef guards against React 18 Strict Mode's double-invocation of effects.
  const mountedRef = useRef(false);
  useEffect(() => {
    if (mountedRef.current) return;
    mountedRef.current = true;
    loadSites();
  }, [loadSites]);

  // Persist panel width
  useEffect(() => {
    localStorage.setItem(LS_KEY, String(panelWidth));
  }, [panelWidth]);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    const onMove = (ev: MouseEvent) => {
      if (!dragging.current) return;
      const maxW = window.innerWidth * 0.5;
      const newWidth = Math.min(maxW, Math.max(PANEL_MIN, window.innerWidth - ev.clientX));
      setPanelWidth(newWidth);
    };
    const onUp = () => {
      dragging.current = false;
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, []);

  return (
    <div className="h-screen flex flex-col bg-slate-950 relative">
      {/* Top Bar */}
      <TopBar />

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left Sidebar - Site Priority */}
        <aside className="w-72 flex-shrink-0 border-r border-slate-700">
          <SitePriority />
        </aside>

        {/* Center - Map */}
        <main className="flex-1 relative min-w-0 z-0">
          <Map />
        </main>

        {/* Right Sidebar - Operations or Site Detail */}
        <aside
          className="flex-shrink-0 border-l border-slate-700 relative z-20"
          style={{ width: panelWidth }}
        >
          {/* Drag handle */}
          <div
            className="absolute left-0 top-0 bottom-0 w-1.5 cursor-col-resize z-30 hover:bg-blue-500/40 active:bg-blue-500/60 transition-colors"
            onMouseDown={onMouseDown}
          />
          {selectedSiteId ? <SiteDetail /> : <Operations />}
        </aside>
      </div>

      {/* Container Summary Panel (slide-in drawer) */}
      <ContainerSummaryPanel />
    </div>
  );
}

export default App;
