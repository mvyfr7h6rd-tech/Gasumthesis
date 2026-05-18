import React, { useState } from 'react';
import { useAppStore } from '../../stores/appStore';
import { MAX_CUSTOM_POINTS } from '../../types';

const isValidLat = (v: string) => { const n = parseFloat(v); return !isNaN(n) && n >= -90 && n <= 90; };
const isValidLon = (v: string) => { const n = parseFloat(v); return !isNaN(n) && n >= -180 && n <= 180; };

const RoutingStatusBadge: React.FC<{ status?: string }> = ({ status }) => {
  if (!status || status === 'pending') return null;
  const cfg: Record<string, { text: string; cls: string }> = {
    ok: { text: 'Routed', cls: 'text-green-400 bg-green-900/30' },
    failed: { text: 'Route failed', cls: 'text-red-400 bg-red-900/30' },
    outside_coverage: { text: 'Outside coverage', cls: 'text-amber-400 bg-amber-900/30' },
  };
  const c = cfg[status];
  if (!c) return null;
  return <span className={`text-xs px-1.5 py-0.5 rounded ${c.cls}`}>{c.text}</span>;
};

export const CustomPointsPanel: React.FC = () => {
  const {
    customPoints,
    addCustomPoint,
    removeCustomPoint,
    renameCustomPoint,
    setCustomPointCoordinates,
    isCustomPointInUse,
  } = useAppStore();

  const [expanded, setExpanded] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editLabel, setEditLabel] = useState('');
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Per-point coordinate editing state (keyed by point id)
  const [coordEdits, setCoordEdits] = useState<Record<string, { lat: string; lon: string }>>({});

  const canAddMore = customPoints.length < MAX_CUSTOM_POINTS;

  const handleAddCustomPoint = () => {
    const result = addCustomPoint();
    if (result === null) {
      setDeleteError(`Maximum ${MAX_CUSTOM_POINTS} custom points allowed`);
      setTimeout(() => setDeleteError(null), 3000);
    }
  };

  const handleDelete = (customId: string) => {
    const result = removeCustomPoint(customId);
    if (!result.success && result.error) {
      setDeleteError(result.error);
      setTimeout(() => setDeleteError(null), 3000);
    }
  };

  const handleStartRename = (customId: string, currentLabel: string) => {
    setEditingId(customId);
    setEditLabel(currentLabel);
  };

  const handleSaveRename = (customId: string) => {
    if (editLabel.trim()) {
      renameCustomPoint(customId, editLabel.trim());
    }
    setEditingId(null);
    setEditLabel('');
  };

  const handleCancelRename = () => {
    setEditingId(null);
    setEditLabel('');
  };

  const getCoordEdit = (cpId: string, cp: typeof customPoints[0]) => {
    return coordEdits[cpId] ?? {
      lat: cp.latitude != null ? String(cp.latitude) : '',
      lon: cp.longitude != null ? String(cp.longitude) : '',
    };
  };

  const setCoordEdit = (cpId: string, field: 'lat' | 'lon', value: string) => {
    setCoordEdits((prev) => ({
      ...prev,
      [cpId]: { ...(prev[cpId] ?? { lat: '', lon: '' }), [field]: value },
    }));
  };

  const handleSaveCoordinates = (cpId: string) => {
    const edit = coordEdits[cpId];
    if (!edit) return;
    if (isValidLat(edit.lat) && isValidLon(edit.lon)) {
      setCustomPointCoordinates(cpId, parseFloat(edit.lat), parseFloat(edit.lon));
    }
  };

  const handleClearCoordinates = (cpId: string) => {
    setCustomPointCoordinates(cpId, undefined, undefined);
    setCoordEdits((prev) => {
      const next = { ...prev };
      delete next[cpId];
      return next;
    });
  };

  return (
    <div className="bg-slate-800 rounded-lg border border-slate-600 overflow-hidden">
      {/* Header */}
      <div
        className="flex items-center justify-between p-3 cursor-pointer hover:bg-slate-700/50"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-2">
          <svg
            className={`w-4 h-4 transition-transform ${expanded ? 'rotate-90' : ''}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
          <span className="font-medium text-sm">Custom Points</span>
          <span className="text-xs text-slate-500">
            ({customPoints.length}/{MAX_CUSTOM_POINTS})
          </span>
        </div>
        {canAddMore && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              handleAddCustomPoint();
            }}
            className="text-xs text-blue-400 hover:text-blue-300 px-2 py-1 rounded hover:bg-slate-700"
          >
            + Add
          </button>
        )}
      </div>

      {/* Content */}
      {expanded && (
        <div className="p-3 pt-0 space-y-3 border-t border-slate-700">
          {/* Error message */}
          {deleteError && (
            <div className="p-2 bg-red-900/50 border border-red-600 rounded text-xs text-red-200">
              {deleteError}
            </div>
          )}

          {/* Info text */}
          <p className="text-xs text-slate-500">
            Enter coordinates to route automatically via road network.
          </p>

          {/* Custom points list */}
          {customPoints.length === 0 ? (
            <div className="text-center py-4">
              <p className="text-sm text-slate-500 mb-2">No custom points defined</p>
              <button
                onClick={handleAddCustomPoint}
                className="text-xs text-blue-400 hover:text-blue-300"
              >
                + Create your first custom point
              </button>
            </div>
          ) : (
            <div className="space-y-3">
              {customPoints.map((cp) => {
                const usage = isCustomPointInUse(cp.id);
                const isEditing = editingId === cp.id;
                const edit = getCoordEdit(cp.id, cp);
                const latValid = edit.lat === '' || isValidLat(edit.lat);
                const lonValid = edit.lon === '' || isValidLon(edit.lon);
                const hasCoords = cp.latitude != null && cp.longitude != null;
                const canSave = edit.lat !== '' && edit.lon !== '' && isValidLat(edit.lat) && isValidLon(edit.lon);
                const coordsDirty = hasCoords
                  ? (edit.lat !== String(cp.latitude) || edit.lon !== String(cp.longitude))
                  : (edit.lat !== '' || edit.lon !== '');

                return (
                  <div
                    key={cp.id}
                    className="bg-slate-700/50 rounded-lg p-2 space-y-2"
                  >
                    {/* Custom point header */}
                    <div className="flex items-center justify-between">
                      {isEditing ? (
                        <div className="flex items-center gap-1 flex-1">
                          <input
                            type="text"
                            value={editLabel}
                            onChange={(e) => setEditLabel(e.target.value)}
                            className="flex-1 p-1 bg-slate-600 border border-slate-500 rounded text-sm"
                            autoFocus
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') handleSaveRename(cp.id);
                              if (e.key === 'Escape') handleCancelRename();
                            }}
                          />
                          <button
                            onClick={() => handleSaveRename(cp.id)}
                            className="text-green-400 hover:text-green-300 p-1"
                            title="Save"
                          >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                            </svg>
                          </button>
                          <button
                            onClick={handleCancelRename}
                            className="text-slate-400 hover:text-slate-300 p-1"
                            title="Cancel"
                          >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                            </svg>
                          </button>
                        </div>
                      ) : (
                        <>
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="font-medium text-amber-400">{cp.label}</span>
                            {hasCoords && (
                              <span className="text-xs text-slate-500">
                                {cp.latitude!.toFixed(4)}, {cp.longitude!.toFixed(4)}
                              </span>
                            )}
                            <RoutingStatusBadge status={cp.routingStatus} />
                            {usage.inUse && (
                              <span className="text-xs text-green-400 bg-green-900/30 px-1.5 py-0.5 rounded">
                                In use
                              </span>
                            )}
                          </div>
                          <div className="flex items-center gap-1">
                            <button
                              onClick={() => handleStartRename(cp.id, cp.label)}
                              className="text-slate-500 hover:text-slate-300 p-1"
                              title="Rename"
                            >
                              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                              </svg>
                            </button>
                            <button
                              onClick={() => handleDelete(cp.id)}
                              className={`p-1 ${
                                usage.inUse
                                  ? 'text-slate-600 cursor-not-allowed'
                                  : 'text-slate-500 hover:text-red-400'
                              }`}
                              title={usage.inUse ? `In use by ${usage.usedBy.join(', ')}` : 'Delete'}
                              disabled={usage.inUse}
                            >
                              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                              </svg>
                            </button>
                          </div>
                        </>
                      )}
                    </div>

                    {/* Coordinate inputs */}
                    <div className="space-y-1.5">
                      <div className="flex items-center gap-2">
                        <label className="text-xs text-slate-400 w-8">Lat</label>
                        <input
                          type="number"
                          placeholder="e.g. 60.17"
                          value={edit.lat}
                          onChange={(e) => setCoordEdit(cp.id, 'lat', e.target.value)}
                          className={`flex-1 p-1.5 bg-slate-800 border rounded text-xs ${
                            latValid ? 'border-slate-600' : 'border-red-500'
                          }`}
                          min="-90"
                          max="90"
                          step="0.0001"
                        />
                        <label className="text-xs text-slate-400 w-8">Lon</label>
                        <input
                          type="number"
                          placeholder="e.g. 24.94"
                          value={edit.lon}
                          onChange={(e) => setCoordEdit(cp.id, 'lon', e.target.value)}
                          className={`flex-1 p-1.5 bg-slate-800 border rounded text-xs ${
                            lonValid ? 'border-slate-600' : 'border-red-500'
                          }`}
                          min="-180"
                          max="180"
                          step="0.0001"
                        />
                      </div>
                      {(!latValid || !lonValid) && (
                        <p className="text-xs text-red-400">
                          {!latValid && 'Latitude must be -90 to 90. '}
                          {!lonValid && 'Longitude must be -180 to 180.'}
                        </p>
                      )}
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => handleSaveCoordinates(cp.id)}
                          disabled={!canSave || !coordsDirty}
                          className={`px-2 py-1 rounded text-xs font-medium ${
                            canSave && coordsDirty
                              ? 'bg-blue-600 hover:bg-blue-700 text-white'
                              : 'bg-slate-700 text-slate-500 cursor-not-allowed'
                          }`}
                        >
                          Save
                        </button>
                        {hasCoords && (
                          <button
                            onClick={() => handleClearCoordinates(cp.id)}
                            className="px-2 py-1 rounded text-xs text-slate-400 hover:text-red-400"
                          >
                            Clear
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default CustomPointsPanel;
