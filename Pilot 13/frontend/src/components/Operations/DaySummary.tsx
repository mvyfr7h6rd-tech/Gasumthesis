import React from 'react';
import type { Route } from '../../types';

interface DaySummaryProps {
  routes: Route[];
}

export const DaySummary: React.FC<DaySummaryProps> = ({ routes }) => {
  if (!routes || routes.length === 0) return null;

  const routesByDay = routes.reduce((acc, route) => {
    const day = route.day_index;
    if (!acc[day]) acc[day] = [];
    acc[day].push(route);
    return acc;
  }, {} as Record<number, Route[]>);

  const days = Object.keys(routesByDay).map(Number).sort((a, b) => a - b);

  const dayAccents = ['text-blue-400', 'text-emerald-400', 'text-amber-400', 'text-purple-400'];

  return (
    <div>
      <p className="text-xs font-medium text-slate-400 mb-1">Activity by Day</p>
      <div className="space-y-1">
        {days.map((day) => {
          const dayRoutes = routesByDay[day];
          const totalDist = dayRoutes.reduce((s, r) => s + r.total_distance_km, 0);
          const accent = dayAccents[(day - 1) % dayAccents.length];
          return (
            <div key={day} className="text-xs leading-snug">
              <span className={`font-medium ${accent}`}>Day {day}</span>
              <span className="text-slate-500"> — {totalDist.toFixed(0)} km</span>
              <span className="text-slate-500 ml-1.5">
                {dayRoutes.map((r) => `${r.truck_id} ${r.total_distance_km.toFixed(0)} km`).join(' · ')}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default DaySummary;
