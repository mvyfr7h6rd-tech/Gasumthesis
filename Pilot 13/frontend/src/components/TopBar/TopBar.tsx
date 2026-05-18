import React, { useState, useRef } from 'react';
import { useAppStore } from '../../stores/appStore';
import { FlaringCostPanel, ConsumptionRatePanel, ActiveConstraintsPanel } from './OverridePanels';
import { ScenarioPanel } from './ScenarioPanel';
import { SituationDrawer } from '../Operations/SituationDrawer';
import { NetworkStatusPanel } from './NetworkStatusPanel';
import { LiveClock } from './LiveClock';
import type { ScenarioType } from '../../api/client';

export const TopBar: React.FC = () => {
  const {
    criticalCount,
    warningCount,
    activeLayers,
    toggleLayer,
    customSpeedKmh,
    constraintOverrides,
    flaringOverrides,
    consumptionOverrides,
    containerSummaryOpen,
    setContainerSummaryOpen,
    scenarioLoading,
    generateScenario,
    sites,
  } = useAppStore();

  const [flaringPanelOpen, setFlaringPanelOpen] = useState(false);
  const [consumptionPanelOpen, setConsumptionPanelOpen] = useState(false);
  const [constraintsPanelOpen, setConstraintsPanelOpen] = useState(false);
  const [scenarioPanelOpen, setScenarioPanelOpen] = useState(false);
  const [aiSituationOpen, setAiSituationOpen] = useState(false);
  const [networkStatusOpen, setNetworkStatusOpen] = useState(false);

  // Refs for panel anchor positioning
  const flaringButtonRef = useRef<HTMLButtonElement>(null);
  const consumptionButtonRef = useRef<HTMLButtonElement>(null);
  const constraintsButtonRef = useRef<HTMLButtonElement>(null);
  const scenarioButtonRef = useRef<HTMLButtonElement>(null);

  const handleGenerateScenario = async (scenarioType: ScenarioType, seed?: number) => {
    await generateScenario(scenarioType, seed);
  };

  const hasFlaringOverrides = Object.keys(flaringOverrides).length > 0;
  const hasConsumptionOverrides = Object.keys(consumptionOverrides).length > 0;

  return (
    <header className="text-white px-4 py-3 flex items-center justify-between shadow-lg bg-slate-800 relative z-40">
      {/* Logo and Title */}
      <div className="flex items-center gap-3">
        <img src="/lumina-logo.png" alt="LUMINA logo" className="w-8 h-8 rounded-lg object-cover" />
        <div>
          <h1 className="text-lg font-semibold">LUMINA</h1>
          <p className="text-xs text-slate-400">Logistics Control Center</p>
        </div>
      </div>

      {/* Live Clock */}
      <LiveClock />

      {/* Status Badges */}
      <div className="flex items-center gap-4">
        {criticalCount > 0 && (
          <span className="px-3 py-1 bg-red-600 rounded-full text-sm font-medium">
            {criticalCount} Critical
          </span>
        )}
        {warningCount > 0 && (
          <span className="px-3 py-1 bg-yellow-600 rounded-full text-sm font-medium">
            {warningCount} Warning
          </span>
        )}
      </div>

      {/* Layer Toggles */}
      <div className="flex items-center gap-2">
        <span className="text-sm text-slate-400 mr-2">Layers:</span>
        <button
          onClick={() => toggleLayer('producers')}
          className={`px-3 py-1 rounded text-sm ${
            activeLayers.has('producers')
              ? 'bg-emerald-700 text-white ring-1 ring-emerald-300'
              : 'bg-slate-700 text-emerald-400/60'
          }`}
        >
          Producers
        </button>
        <button
          onClick={() => toggleLayer('consumers')}
          className={`px-3 py-1 rounded text-sm ${
            activeLayers.has('consumers')
              ? 'bg-orange-600 text-white ring-1 ring-orange-300'
              : 'bg-slate-700 text-orange-400/60'
          }`}
        >
          Consumers
        </button>
      </div>

      {/* Rate Override Panels */}
      <div className="flex items-center gap-2">
        <button
          ref={flaringButtonRef}
          onClick={() => {
            setFlaringPanelOpen(!flaringPanelOpen);
            setConsumptionPanelOpen(false);
          }}
          className={`px-3 py-1 rounded text-sm flex items-center gap-1 ${
            hasFlaringOverrides
              ? 'bg-amber-600 text-white'
              : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
          }`}
        >
          Producers: Flaring Cost
          {hasFlaringOverrides && (
            <span className="w-2 h-2 bg-white rounded-full"></span>
          )}
        </button>
        <FlaringCostPanel
          isOpen={flaringPanelOpen}
          onClose={() => setFlaringPanelOpen(false)}
          anchorRef={flaringButtonRef}
        />
        <button
          ref={consumptionButtonRef}
          onClick={() => {
            setConsumptionPanelOpen(!consumptionPanelOpen);
            setFlaringPanelOpen(false);
          }}
          className={`px-3 py-1 rounded text-sm flex items-center gap-1 ${
            hasConsumptionOverrides
              ? 'bg-amber-600 text-white'
              : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
          }`}
        >
          Consumption Rate
          {hasConsumptionOverrides && (
            <span className="w-2 h-2 bg-white rounded-full"></span>
          )}
        </button>
        <ConsumptionRatePanel
          isOpen={consumptionPanelOpen}
          onClose={() => setConsumptionPanelOpen(false)}
          anchorRef={consumptionButtonRef}
        />
      </div>

      {/* Active Constraints */}
      <div className="flex items-center">
        <button
          ref={constraintsButtonRef}
          onClick={() => {
            setConstraintsPanelOpen(!constraintsPanelOpen);
            setFlaringPanelOpen(false);
            setConsumptionPanelOpen(false);
          }}
          className={`px-3 py-1 rounded text-sm flex items-center gap-1.5 ${
            constraintsPanelOpen
              ? 'bg-slate-600 text-white'
              : (customSpeedKmh !== null || Object.values(constraintOverrides).some((v) => v !== undefined))
              ? 'bg-blue-600 text-white'
              : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
          }`}
        >
          Active Constraints
          {(customSpeedKmh !== null || Object.values(constraintOverrides).some((v) => v !== undefined)) && (
            <span className="w-2 h-2 bg-white rounded-full flex-shrink-0"></span>
          )}
        </button>
        <ActiveConstraintsPanel
          isOpen={constraintsPanelOpen}
          onClose={() => setConstraintsPanelOpen(false)}
          anchorRef={constraintsButtonRef}
        />
      </div>

      {/* Scenario Generator Button */}
      <button
        ref={scenarioButtonRef}
        onClick={() => {
          setScenarioPanelOpen(!scenarioPanelOpen);
          setFlaringPanelOpen(false);
          setConsumptionPanelOpen(false);
        }}
        disabled={scenarioLoading}
        className={`px-3 py-1 rounded text-sm flex items-center gap-1 ${
          scenarioLoading
            ? 'bg-slate-600 text-slate-400 cursor-not-allowed'
            : scenarioPanelOpen
            ? 'bg-amber-600 text-white'
            : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
        }`}
      >
        {scenarioLoading ? 'Applying…' : 'Generate Scenario'}
      </button>
      <ScenarioPanel
        isOpen={scenarioPanelOpen}
        onClose={() => setScenarioPanelOpen(false)}
        anchorRef={scenarioButtonRef}
        onGenerate={handleGenerateScenario}
        loading={scenarioLoading}
      />

      {/* Network Status Button */}
      <button
        onClick={() => { setNetworkStatusOpen(!networkStatusOpen); setAiSituationOpen(false); }}
        disabled={sites.length === 0}
        className={`px-3 py-1 rounded text-sm flex items-center gap-1.5 disabled:opacity-40 disabled:cursor-not-allowed ${
          networkStatusOpen
            ? 'bg-blue-600 text-white'
            : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
        }`}
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 10h16M4 14h16M4 18h16" />
        </svg>
        Network Status
      </button>

      {/* AI Situation Button */}
      <button
        onClick={() => { setAiSituationOpen(!aiSituationOpen); setNetworkStatusOpen(false); }}
        disabled={sites.length === 0}
        className={`px-3 py-1 rounded text-sm flex items-center gap-1.5 disabled:opacity-40 disabled:cursor-not-allowed ${
          aiSituationOpen
            ? 'bg-emerald-600 text-white'
            : 'bg-slate-700 text-emerald-300 hover:bg-slate-600'
        }`}
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
        </svg>
        AI Situation
      </button>

      {/* Container Summary Button */}
      <button
        onClick={() => setContainerSummaryOpen(!containerSummaryOpen)}
        className={`px-3 py-1 rounded text-sm ${
          containerSummaryOpen
            ? 'bg-blue-600 text-white'
            : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
        }`}
      >
        Container Summary
      </button>

      {/* Panels */}
      {networkStatusOpen && (
        <NetworkStatusPanel sites={sites} onClose={() => setNetworkStatusOpen(false)} />
      )}
      {aiSituationOpen && (
        <SituationDrawer sites={sites} onClose={() => setAiSituationOpen(false)} />
      )}
    </header>
  );
};

export default TopBar;
