import type { Route } from '../types';

export function getRouteServiceTimeHours(route: Route): number {
  return route.stops.reduce((sum, stop) => sum + (stop.service_time_hours ?? 0), 0);
}

export function getRouteLegDriveTimeHours(route: Route): number {
  return route.legs.reduce((sum, leg) => sum + (leg.drive_time_hours ?? 0), 0);
}

export function getRouteDisplayTimeHours(route: Route): number {
  const serviceTimeHours = getRouteServiceTimeHours(route);
  const legDriveTimeHours = getRouteLegDriveTimeHours(route);
  const minimumFeasibleTimeHours =
    legDriveTimeHours > 0 ? legDriveTimeHours + serviceTimeHours : route.total_time_hours;
  return Math.max(route.total_time_hours, minimumFeasibleTimeHours);
}

export function getRouteDriveTimeHours(route: Route): number {
  const serviceTimeHours = getRouteServiceTimeHours(route);
  const legDriveTimeHours = getRouteLegDriveTimeHours(route);
  const inferredDriveTimeHours = Math.max(route.total_time_hours - serviceTimeHours, 0);
  return Math.max(inferredDriveTimeHours, legDriveTimeHours);
}

export function getRouteStopDisplayTimes(route: Route): number[] {
  const displayTimes: number[] = [];

  for (let i = 0; i < route.stops.length; i += 1) {
    if (i === 0) {
      displayTimes.push(0);
      continue;
    }

    const previousDisplay = displayTimes[i - 1] ?? 0;
    const legDrive = route.legs[i - 1]?.drive_time_hours ?? 0;
    const serviceTime = route.stops[i].service_time_hours ?? 0;
    const reconstructed = previousDisplay + legDrive + serviceTime;
    const backendReported = route.stops[i].arrival_time_hours ?? 0;

    displayTimes.push(Math.max(reconstructed, backendReported));
  }

  return displayTimes;
}
