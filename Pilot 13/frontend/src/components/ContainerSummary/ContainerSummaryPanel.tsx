import React, { useState, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { useAppStore } from '../../stores/appStore';
import * as api from '../../api/client';
import { pressureToKg, kgToMwh } from '../../utils/conversions';
import type { Site, Bay, SitePatchRequest } from '../../types';

type SiteFilter = 'all' | 'producers' | 'consumers';

// ── Confirm modal (portal, reusable within this file) ──────────────────────

interface ConfirmSaveModalProps {
  onConfirm: () => void;
  onCancel: () => void;
}

const ConfirmSaveModal: React.FC<ConfirmSaveModalProps> = ({ onConfirm, onCancel }) =>
  createPortal(
    <div className="fixed inset-0 flex items-center justify-center" style={{ zIndex: 9999 }}>
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60" onClick={onCancel} />
      {/* Dialog */}
      <div className="relative bg-slate-800 border border-slate-600 rounded-xl p-6 shadow-2xl w-72">
        <h3 className="text-sm font-semibold text-white mb-1 text-center">Save changes?</h3>
        <p className="text-xs text-slate-400 text-center mb-4">
          This will update the bay in the system.
        </p>
        <div className="flex gap-3">
          <button
            onClick={onConfirm}
            className="flex-1 py-2 bg-blue-600 hover:bg-blue-700 rounded-lg text-sm font-medium text-white transition-colors"
          >
            Yes, save
          </button>
          <button
            onClick={onCancel}
            className="flex-1 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg text-sm font-medium text-white transition-colors"
          >
            No, cancel
          </button>
        </div>
      </div>
    </div>,
    document.body
  );

// ── Alternating group backgrounds ──────────────────────────────────────────

const GROUP_ROW_BG = ['bg-slate-800/25', ''] as const;
const GROUP_HEADER_BG = ['bg-slate-700/20', 'bg-slate-800/10'] as const;

// ── Draft state type ───────────────────────────────────────────────────────

interface BayDraft {
  serial_number: string; // always string; leading zeros preserved
  pressure_bar: number;
}

// ── SiteGroup ──────────────────────────────────────────────────────────────

const SiteGroup: React.FC<{ site: Site; groupIndex: number }> = ({ site, groupIndex }) => {
  const { loadSites } = useAppStore();

  const [collapsed, setCollapsed] = useState(false);
  const [editingBayId, setEditingBayId] = useState<string | null>(null);
  const [draft, setDraft] = useState<BayDraft | null>(null);
  const [pendingConfirm, setPendingConfirm] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const isProducer = site.site_type === 'production';
  const nameColor = isProducer ? 'text-emerald-400' : 'text-orange-400';
  const rowBg = GROUP_ROW_BG[groupIndex % 2];
  const headerBg = GROUP_HEADER_BG[groupIndex % 2];
  const withSerial = site.bays.filter((b) => b.serial_number?.length === 4).length;

  // ── Edit lifecycle ─────────────────────────────────────────────────────

  const startEdit = (bay: Bay) => {
    setEditingBayId(bay.bay_id);
    setDraft({ serial_number: bay.serial_number ?? '', pressure_bar: bay.pressure_bar });
    setSaveError(null);
  };

  const cancelEdit = () => {
    setEditingBayId(null);
    setDraft(null);
    setSaveError(null);
    setPendingConfirm(false);
  };

  const requestSave = () => {
    if (draft) setPendingConfirm(true);
  };

  const confirmSave = async () => {
    if (!draft || !editingBayId) return;
    setPendingConfirm(false);
    setSaving(true);
    setSaveError(null);
    try {
      const patchRequest: SitePatchRequest = {
        bays: [
          {
            bay_id: editingBayId,
            pressure_bar: draft.pressure_bar,
            // Only persist a complete 4-digit S/N; otherwise clear it
            serial_number: draft.serial_number.length === 4 ? draft.serial_number : null,
          },
        ],
      };
      await api.patchSite(site.id, patchRequest);
      // Re-read from source of truth — keeps all panels in sync
      await loadSites();
      setEditingBayId(null);
      setDraft(null);
    } catch (err) {
      const message =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        (err as Error)?.message ||
        'Save failed';
      setSaveError(message);
    } finally {
      setSaving(false);
    }
  };

  const handleSerialChange = (value: string) => {
    // Digits only, max 4, leading zeros preserved (string treatment)
    const digits = value.replace(/\D/g, '').slice(0, 4);
    setDraft((d) => (d ? { ...d, serial_number: digits } : d));
  };

  const handlePressureChange = (value: string) => {
    const pressure = Math.max(0, Math.min(250, parseInt(value) || 0));
    setDraft((d) => (d ? { ...d, pressure_bar: pressure } : d));
  };

  return (
    <>
      {/* ── Site section header ── */}
      <tr
        className={`${headerBg} border-t-2 border-slate-600/50 cursor-pointer select-none`}
        onClick={() => setCollapsed(!collapsed)}
      >
        <td colSpan={6} className="px-3 py-2">
          <div className="flex items-center gap-2">
            <svg
              className={`w-3 h-3 text-slate-500 flex-shrink-0 transition-transform duration-150 ${
                collapsed ? '-rotate-90' : ''
              }`}
              fill="currentColor"
              viewBox="0 0 20 20"
            >
              <path
                fillRule="evenodd"
                d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z"
                clipRule="evenodd"
              />
            </svg>
            <span className={`font-semibold text-xs ${nameColor}`}>{site.name}</span>
            <span className="text-slate-500 text-[11px] font-normal">
              {site.bays.length} bay{site.bays.length !== 1 ? 's' : ''}
              {withSerial > 0 && (
                <>
                  {' '}·{' '}
                  <span className="text-slate-400">{withSerial} S/N</span>
                </>
              )}
            </span>
            <span
              className={`ml-auto text-[10px] px-1.5 py-0.5 rounded font-medium ${
                isProducer
                  ? 'bg-emerald-900/40 text-emerald-500'
                  : 'bg-orange-900/40 text-orange-500'
              }`}
            >
              {isProducer ? 'producer' : 'consumer'}
            </span>
          </div>
        </td>
      </tr>

      {/* ── Bay rows ── */}
      {!collapsed &&
        site.bays.map((bay) => {
          const isEditing = editingBayId === bay.bay_id;
          // Live-preview usable kg/MWh from draft pressure while editing
          const displayPressure = isEditing && draft ? draft.pressure_bar : bay.pressure_bar;
          const kgAtFloor = pressureToKg(site.usable_floor_bar);
          const kgUsable = isEditing
            ? Math.max(0, pressureToKg(displayPressure) - kgAtFloor)
            : bay.kg_usable;
          const mwhUsable = isEditing ? kgToMwh(kgUsable) : bay.mwh_usable;

          return (
            <React.Fragment key={bay.bay_id}>
              <tr
                className={`${rowBg} border-t border-slate-700/25 hover:bg-slate-700/20 transition-colors ${
                  isEditing ? 'ring-1 ring-inset ring-blue-500/30' : ''
                }`}
              >
                {/* Bay ID — muted, monospace, indented */}
                <td className="pl-8 pr-3 py-1.5 font-mono text-slate-500 text-[11px] w-[24%]">
                  {bay.bay_id}
                </td>

                {/* S/N — neutral (no producer green), editable */}
                <td className="px-3 py-1.5 font-mono w-[16%]">
                  {isEditing && draft ? (
                    <input
                      type="text"
                      inputMode="numeric"
                      maxLength={4}
                      value={draft.serial_number}
                      onChange={(e) => handleSerialChange(e.target.value)}
                      placeholder="0000"
                      className={`w-14 px-1.5 py-0.5 rounded text-[11px] text-center font-mono focus:outline-none focus:ring-1 focus:ring-blue-500 bg-slate-700 border text-slate-200 ${
                        draft.serial_number.length === 4
                          ? 'border-blue-500'
                          : 'border-slate-500'
                      }`}
                    />
                  ) : bay.serial_number && bay.serial_number.length === 4 ? (
                    <span className="text-slate-300 tracking-wide">{bay.serial_number}</span>
                  ) : (
                    <span className="text-slate-600">—</span>
                  )}
                </td>

                {/* Pressure — pill in view, number input while editing */}
                <td className="px-3 py-1.5 w-[22%]">
                  {isEditing && draft ? (
                    <div className="flex items-center gap-1">
                      <input
                        type="number"
                        min="0"
                        max="250"
                        value={draft.pressure_bar}
                        onChange={(e) => handlePressureChange(e.target.value)}
                        className="w-16 px-1.5 py-0.5 rounded text-[11px] focus:outline-none focus:ring-1 focus:ring-blue-500 bg-slate-700 border border-slate-500 text-slate-200 tabular-nums"
                      />
                      <span className="text-slate-500 text-[10px]">bar</span>
                    </div>
                  ) : (
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-slate-700 border border-slate-600 text-slate-100 font-semibold text-[11px] tabular-nums">
                      {bay.pressure_bar}
                      <span className="text-slate-400 font-normal">bar</span>
                    </span>
                  )}
                </td>

                {/* Usable kg — live-updates from draft pressure while editing */}
                <td className="px-3 py-1.5 text-right text-slate-400 text-[11px] tabular-nums w-[13%]">
                  {kgUsable.toFixed(0)}
                </td>

                {/* Usable MWh — live-updates from draft pressure while editing */}
                <td className="px-3 py-1.5 text-right text-slate-400 text-[11px] tabular-nums w-[13%]">
                  {mwhUsable.toFixed(2)}
                </td>

                {/* Actions */}
                <td className="px-2 py-1.5 text-right w-[12%]">
                  {isEditing ? (
                    <div className="flex items-center justify-end gap-1">
                      <button
                        onClick={requestSave}
                        disabled={saving}
                        title="Save bay"
                        className="px-2 py-0.5 rounded text-[10px] bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white font-medium transition-colors"
                      >
                        {saving ? '…' : 'Save'}
                      </button>
                      <button
                        onClick={cancelEdit}
                        title="Cancel edit"
                        className="px-1.5 py-0.5 rounded text-[10px] bg-slate-600 hover:bg-slate-500 text-slate-200 transition-colors"
                      >
                        ✕
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={(e) => {
                        e.stopPropagation(); // don't collapse the section
                        startEdit(bay);
                      }}
                      title="Edit bay"
                      className="p-1 rounded text-slate-600 hover:text-slate-300 hover:bg-slate-700/50 transition-colors"
                    >
                      {/* Pencil icon */}
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={2}
                          d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"
                        />
                      </svg>
                    </button>
                  )}
                </td>
              </tr>

              {/* Inline error row — appears below the editing bay row */}
              {isEditing && saveError && (
                <tr className={rowBg}>
                  <td colSpan={6} className="pl-8 pr-3 py-1 text-[11px] text-red-400">
                    {saveError}
                  </td>
                </tr>
              )}
            </React.Fragment>
          );
        })}

      {/* Confirm modal — portal to document.body */}
      {pendingConfirm && (
        <ConfirmSaveModal
          onConfirm={confirmSave}
          onCancel={() => setPendingConfirm(false)}
        />
      )}
    </>
  );
};

// ── ContainerSummaryPanel ──────────────────────────────────────────────────

export const ContainerSummaryPanel: React.FC = () => {
  const { sites, containerSummaryOpen, setContainerSummaryOpen } = useAppStore();

  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<SiteFilter>('all');

  const filteredSites = useMemo(() => {
    let result = sites;

    if (filter === 'producers') {
      result = result.filter((s) => s.site_type === 'production');
    } else if (filter === 'consumers') {
      result = result.filter((s) => s.site_type !== 'production');
    }

    if (search.trim()) {
      const q = search.trim().toLowerCase();
      result = result.filter(
        (s) =>
          s.name.toLowerCase().includes(q) ||
          s.bays.some(
            (b) =>
              b.bay_id.toLowerCase().includes(q) ||
              (b.serial_number && b.serial_number.includes(q))
          )
      );
    }

    return result;
  }, [sites, filter, search]);

  const totalBays = useMemo(() => sites.reduce((sum, s) => sum + s.bays.length, 0), [sites]);
  const serialCount = useMemo(
    () =>
      sites.reduce(
        (sum, s) => sum + s.bays.filter((b) => b.serial_number?.length === 4).length,
        0
      ),
    [sites]
  );

  if (!containerSummaryOpen) return null;

  return (
    <div
      className="fixed inset-y-0 right-0 w-[640px] bg-slate-900 border-l border-slate-700 shadow-2xl z-50 flex flex-col"
      style={{ top: '56px' }}
    >
      {/* ── Header ── */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700 flex-shrink-0 bg-slate-800">
        <div>
          <h2 className="text-sm font-semibold text-white">Container Summary</h2>
          <p className="text-xs text-slate-400 mt-0.5">
            {totalBays} bays total · {serialCount} with serial numbers
          </p>
        </div>
        <button
          onClick={() => setContainerSummaryOpen(false)}
          className="p-1.5 rounded hover:bg-slate-700 text-slate-400 hover:text-white transition-colors"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      {/* ── Controls ── */}
      <div className="flex items-center gap-2 px-4 py-2 border-b border-slate-700 flex-shrink-0 bg-slate-800">
        <input
          type="text"
          placeholder="Search site, bay, or serial..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 px-3 py-1.5 bg-slate-700 border border-slate-600 rounded text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500"
        />
        <div className="flex rounded overflow-hidden border border-slate-600">
          {(['all', 'producers', 'consumers'] as SiteFilter[]).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 text-xs capitalize ${
                filter === f
                  ? 'bg-blue-600 text-white'
                  : 'bg-slate-700 text-slate-400 hover:bg-slate-600'
              }`}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {/* ── Grouped table ── */}
      <div className="flex-1 overflow-y-auto">
        {filteredSites.length === 0 ? (
          <div className="flex items-center justify-center h-32 text-sm text-slate-500">
            No sites match your search
          </div>
        ) : (
          <table className="w-full text-xs">
            {/* Sticky column header — 6 columns now (+ Actions) */}
            <thead className="sticky top-0 z-10 bg-slate-800 shadow-sm">
              <tr className="border-b border-slate-600">
                <th className="px-3 py-2 text-left text-slate-400 font-medium w-[24%]">Bay</th>
                <th className="px-3 py-2 text-left text-slate-400 font-medium w-[16%]">S/N</th>
                <th className="px-3 py-2 text-left text-slate-400 font-medium w-[22%]">Pressure</th>
                <th className="px-3 py-2 text-right text-slate-400 font-medium w-[13%]">Usable kg</th>
                <th className="px-3 py-2 text-right text-slate-400 font-medium w-[13%]">Usable MWh</th>
                <th className="w-[12%]" /> {/* Actions column — no label */}
              </tr>
            </thead>
            <tbody>
              {filteredSites.map((site, idx) => (
                <SiteGroup key={site.id} site={site} groupIndex={idx} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
};

export default ContainerSummaryPanel;
