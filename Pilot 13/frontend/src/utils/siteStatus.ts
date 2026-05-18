import type { Site } from '../types';

export type SiteStatus = 'critical' | 'warning' | 'normal' | 'safe';

export function getSiteStatus(site: Pick<Site, 'risk_level'>): SiteStatus {
  return site.risk_level as SiteStatus;
}

export const STATUS_HEX_COLORS: Record<SiteStatus, string> = {
  critical: '#ef4444',
  warning:  '#eab308',
  normal:   '#22c55e',
  safe:     '#22c55e',  // same green as normal
};

export const STATUS_TEXT_CLASSES: Record<SiteStatus, string> = {
  critical: 'text-red-400',
  warning:  'text-yellow-400',
  normal:   'text-green-400',
  safe:     'text-green-400',
};

export const STATUS_BG_CLASSES: Record<SiteStatus, string> = {
  critical: 'bg-red-500',
  warning:  'bg-yellow-500',
  normal:   'bg-green-500',
  safe:     'bg-green-500',
};

export const STATUS_BADGE_BG_CLASSES: Record<SiteStatus, string> = {
  critical: 'bg-red-600',
  warning:  'bg-yellow-600',
  normal:   'bg-green-600',
  safe:     'bg-green-600',
};

/** For use on light backgrounds (popup cards, etc.) */
export const STATUS_TEXT_DARK_CLASSES: Record<SiteStatus, string> = {
  critical: 'text-red-600',
  warning:  'text-yellow-600',
  normal:   'text-green-600',
  safe:     'text-green-600',
};
