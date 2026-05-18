import React, { useState } from 'react';

interface QuickAddDistanceProps {
  customId: string;
  customLabel: string;
  sites: { id: string; name: string }[];
  existingDistances: Record<string, number>;  // siteId -> km
  onAddDistance: (siteId: string, distanceKm: number) => void;
  onRemoveDistance: (siteId: string) => void;
  onEditDistance: (siteId: string, distanceKm: number) => void;
  suggestedSiteId?: string;  // Pre-select this site (for missing distance prompts)
}

export const QuickAddDistance: React.FC<QuickAddDistanceProps> = ({
  customId: _customId,
  customLabel,
  sites,
  existingDistances,
  onAddDistance,
  onRemoveDistance,
  onEditDistance,
  suggestedSiteId,
}) => {
  // _customId is passed but not used directly in this component (used by parent for callbacks)
  void _customId;
  const [selectedSite, setSelectedSite] = useState<string>(suggestedSiteId || '');
  const [distanceKm, setDistanceKm] = useState<string>('');
  const [editingSiteId, setEditingSiteId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState<string>('');

  // Sites that don't have a distance yet
  const availableSites = sites.filter((s) => existingDistances[s.id] === undefined);
  const hasSites = Object.keys(existingDistances).length > 0;

  const handleAdd = () => {
    if (!selectedSite || !distanceKm) return;
    const km = parseFloat(distanceKm);
    if (isNaN(km) || km <= 0) return;
    onAddDistance(selectedSite, km);
    setSelectedSite('');
    setDistanceKm('');
  };

  const handleStartEdit = (siteId: string, currentKm: number) => {
    setEditingSiteId(siteId);
    setEditValue(currentKm.toString());
  };

  const handleSaveEdit = (siteId: string) => {
    const km = parseFloat(editValue);
    if (!isNaN(km) && km > 0) {
      onEditDistance(siteId, km);
    }
    setEditingSiteId(null);
    setEditValue('');
  };

  const handleCancelEdit = () => {
    setEditingSiteId(null);
    setEditValue('');
  };

  return (
    <div className="bg-slate-700/50 rounded p-2 space-y-2">
      <div className="text-xs text-slate-400 font-medium">
        Distances from {customLabel}
      </div>

      {/* Existing distances */}
      {hasSites && (
        <div className="space-y-1">
          {Object.entries(existingDistances).map(([siteId, km]) => {
            const site = sites.find((s) => s.id === siteId);
            const siteName = site?.name || siteId;
            const isEditing = editingSiteId === siteId;

            return (
              <div
                key={siteId}
                className="flex items-center justify-between text-xs bg-slate-800 rounded px-2 py-1"
              >
                <span className="text-slate-300 truncate flex-1">{siteName}</span>
                {isEditing ? (
                  <div className="flex items-center gap-1">
                    <input
                      type="number"
                      value={editValue}
                      onChange={(e) => setEditValue(e.target.value)}
                      className="w-16 p-1 bg-slate-600 border border-slate-500 rounded text-xs"
                      min="0"
                      step="0.1"
                      autoFocus
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') handleSaveEdit(siteId);
                        if (e.key === 'Escape') handleCancelEdit();
                      }}
                    />
                    <button
                      onClick={() => handleSaveEdit(siteId)}
                      className="text-green-400 hover:text-green-300 p-0.5"
                      title="Save"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                      </svg>
                    </button>
                    <button
                      onClick={handleCancelEdit}
                      className="text-slate-400 hover:text-slate-300 p-0.5"
                      title="Cancel"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                ) : (
                  <div className="flex items-center gap-2">
                    <span className="text-blue-400 font-medium">{km.toFixed(1)} km</span>
                    <button
                      onClick={() => handleStartEdit(siteId, km)}
                      className="text-slate-500 hover:text-slate-300 p-0.5"
                      title="Edit"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                      </svg>
                    </button>
                    <button
                      onClick={() => onRemoveDistance(siteId)}
                      className="text-slate-500 hover:text-red-400 p-0.5"
                      title="Delete"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Add new distance */}
      {availableSites.length > 0 && (
        <div className="flex items-center gap-2">
          <select
            value={selectedSite}
            onChange={(e) => setSelectedSite(e.target.value)}
            className="flex-1 p-1.5 bg-slate-800 border border-slate-600 rounded text-xs"
          >
            <option value="">Select site...</option>
            {availableSites.map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
          <input
            type="number"
            placeholder="km"
            value={distanceKm}
            onChange={(e) => setDistanceKm(e.target.value)}
            className="w-16 p-1.5 bg-slate-800 border border-slate-600 rounded text-xs"
            min="0"
            step="0.1"
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleAdd();
            }}
          />
          <button
            onClick={handleAdd}
            disabled={!selectedSite || !distanceKm}
            className={`px-2 py-1.5 rounded text-xs font-medium ${
              selectedSite && distanceKm
                ? 'bg-blue-600 hover:bg-blue-700 text-white'
                : 'bg-slate-700 text-slate-500 cursor-not-allowed'
            }`}
          >
            Add
          </button>
        </div>
      )}

      {availableSites.length === 0 && hasSites && (
        <div className="text-xs text-slate-500 text-center py-1">
          All sites have distances defined
        </div>
      )}

      {!hasSites && availableSites.length === 0 && (
        <div className="text-xs text-slate-500 text-center py-1">
          No sites available
        </div>
      )}
    </div>
  );
};

export default QuickAddDistance;
