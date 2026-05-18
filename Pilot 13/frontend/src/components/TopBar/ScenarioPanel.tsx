import React, { useState } from 'react';
import type { ScenarioType } from '../../api/client';

interface ScenarioDef {
  label: string;
  description: string;
  color: string; // Tailwind bg class for the badge
}

const SCENARIOS: Record<ScenarioType, ScenarioDef> = {
  balanced: {
    label: 'Balanced',
    description: '10% critical (6-12h) · 20% warning (24-36h) · 70% normal (48-72h)',
    color: 'bg-blue-600',
  },
  high_demand: {
    label: 'High Demand',
    description: '40% consumers critical (4-12h) · producers normal (48-72h)',
    color: 'bg-orange-600',
  },
  high_production: {
    label: 'High Production',
    description: '40% producers near overflow (4-12h) · consumers normal',
    color: 'bg-emerald-600',
  },
  capacity_crisis: {
    label: 'Capacity Crisis',
    description: 'Balanced pressures · all truck capacities temporarily set to 1',
    color: 'bg-yellow-600',
  },
};

const DETERMINISTIC_SEED = 42;

interface Props {
  isOpen: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLButtonElement | null>;
  onGenerate: (scenarioType: ScenarioType, seed?: number) => Promise<void>;
  loading: boolean;
}

export const ScenarioPanel: React.FC<Props> = ({
  isOpen,
  onClose,
  anchorRef,
  onGenerate,
  loading,
}) => {
  const [selected, setSelected] = useState<ScenarioType>('balanced');
  const [deterministic, setDeterministic] = useState(false);

  if (!isOpen) return null;

  // Position panel below the anchor button
  const rect = anchorRef.current?.getBoundingClientRect();
  const top = rect ? rect.bottom + 8 : 60;
  const right = rect ? window.innerWidth - rect.right : 16;

  const handleApply = async () => {
    await onGenerate(selected, deterministic ? DETERMINISTIC_SEED : undefined);
    onClose();
  };

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-40"
        onClick={onClose}
      />

      {/* Panel */}
      <div
        className="fixed z-50 w-80 bg-slate-800 border border-slate-600 rounded-lg shadow-xl p-4"
        style={{ top, right }}
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-white">Generate Scenario</h3>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-white text-lg leading-none"
          >
            ×
          </button>
        </div>

        <p className="text-xs text-slate-400 mb-3">
          Overwrites bay pressures using hours-to-critical logic. Does not affect solver
          or real data permanently.
        </p>

        {/* Scenario list */}
        <div className="flex flex-col gap-1.5 mb-3">
          {(Object.entries(SCENARIOS) as [ScenarioType, ScenarioDef][]).map(
            ([key, def]) => (
              <button
                key={key}
                onClick={() => setSelected(key)}
                className={`w-full text-left px-3 py-2 rounded border text-sm transition-colors ${
                  selected === key
                    ? 'border-blue-500 bg-slate-700 text-white'
                    : 'border-transparent bg-slate-700/50 text-slate-300 hover:bg-slate-700'
                }`}
              >
                <div className="flex items-center gap-2">
                  <span
                    className={`inline-block w-2 h-2 rounded-full flex-shrink-0 ${def.color}`}
                  />
                  <span className="font-medium">{def.label}</span>
                </div>
                <p className="text-xs text-slate-400 mt-0.5 ml-4">{def.description}</p>
              </button>
            )
          )}
        </div>

        {/* Deterministic mode toggle */}
        <label className="flex items-center gap-2 mb-4 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={deterministic}
            onChange={(e) => setDeterministic(e.target.checked)}
            className="w-3.5 h-3.5 accent-blue-500"
          />
          <span className="text-xs text-slate-300">
            Deterministic mode
            <span className="text-slate-500 ml-1">(same result every time)</span>
          </span>
        </label>

        <button
          onClick={handleApply}
          disabled={loading}
          className={`w-full py-2 rounded text-sm font-medium transition-colors ${
            loading
              ? 'bg-slate-600 text-slate-400 cursor-not-allowed'
              : 'bg-amber-600 hover:bg-amber-500 text-white'
          }`}
        >
          {loading ? 'Applying…' : `Apply "${SCENARIOS[selected].label}"`}
        </button>

      </div>
    </>
  );
};

export default ScenarioPanel;
