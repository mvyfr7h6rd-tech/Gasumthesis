import React, { useState, useEffect, useRef } from 'react';

const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function pad(n: number) { return String(n).padStart(2, '0'); }
function formatTime(d: Date) { return `${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function formatDate(d: Date) {
  return `${DAY_NAMES[d.getDay()]}, ${d.getDate()} ${MONTH_NAMES[d.getMonth()]} ${d.getFullYear()}`;
}
function toDateInputValue(d: Date) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export const LiveClock: React.FC = () => {
  const [now, setNow] = useState(() => new Date());
  const [editingTime, setEditingTime] = useState(false);
  const [editingDate, setEditingDate] = useState(false);
  const [timeDraft, setTimeDraft] = useState('');
  const timeRef = useRef<HTMLInputElement>(null);
  const dateRef = useRef<HTMLInputElement>(null);

  // Tick every 30 s — stops if manually edited
  const [autoTick, setAutoTick] = useState(true);
  useEffect(() => {
    if (!autoTick) return;
    const id = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(id);
  }, [autoTick]);

  const startTimeEdit = () => {
    setTimeDraft(formatTime(now));
    setEditingTime(true);
    setAutoTick(false);
    setTimeout(() => timeRef.current?.select(), 0);
  };

  const commitTime = () => {
    const match = timeDraft.match(/^(\d{1,2}):(\d{2})$/);
    if (match) {
      const updated = new Date(now);
      updated.setHours(parseInt(match[1], 10));
      updated.setMinutes(parseInt(match[2], 10));
      updated.setSeconds(0);
      setNow(updated);
    }
    setEditingTime(false);
  };

  const startDateEdit = () => {
    setEditingDate(true);
    setAutoTick(false);
    setTimeout(() => dateRef.current?.showPicker?.(), 50);
  };

  const commitDate = (value: string) => {
    if (value) {
      const [y, m, d] = value.split('-').map(Number);
      const updated = new Date(now);
      updated.setFullYear(y, m - 1, d);
      setNow(updated);
    }
    setEditingDate(false);
  };

  return (
    <div className="flex flex-col items-end select-none gap-0.5">
      {/* Time row */}
      {editingTime ? (
        <div className="flex items-center gap-1">
          <input
            ref={timeRef}
            type="text"
            value={timeDraft}
            onChange={e => setTimeDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') commitTime();
              if (e.key === 'Escape') setEditingTime(false);
            }}
            onBlur={commitTime}
            className="w-14 bg-slate-700 border border-blue-500 rounded px-1 py-0.5 text-sm font-mono text-white text-center"
            placeholder="HH:MM"
          />
        </div>
      ) : (
        <button
          onClick={startTimeEdit}
          title="Click to adjust time"
          className="text-sm font-mono font-semibold text-white hover:text-blue-300 transition-colors leading-none"
        >
          {formatTime(now)}
        </button>
      )}

      {/* Date row */}
      {editingDate ? (
        <input
          ref={dateRef}
          type="date"
          defaultValue={toDateInputValue(now)}
          onChange={e => commitDate(e.target.value)}
          onBlur={e => commitDate(e.target.value)}
          onKeyDown={e => { if (e.key === 'Escape') setEditingDate(false); }}
          className="bg-slate-700 border border-blue-500 rounded px-1 py-0.5 text-[10px] text-white w-28"
        />
      ) : (
        <button
          onClick={startDateEdit}
          title="Click to change date"
          className="text-[10px] text-slate-400 hover:text-blue-300 transition-colors leading-none"
        >
          {formatDate(now)}
        </button>
      )}
    </div>
  );
};
