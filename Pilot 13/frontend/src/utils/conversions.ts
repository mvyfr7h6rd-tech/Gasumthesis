/**
 * Conversion utilities for pressure, mass, and energy calculations.
 *
 * Anchors:
 * - 200 bar → 1316 kg → 20 MWh
 * - 250 bar → 2829 kg → 43 MWh
 *
 * Energy formula: MWh = (kg / 1000) * 15.2
 */

/**
 * Convert pressure (bar) to mass (kg) using piecewise linear formula.
 *
 * - 0..200 bar: linear from (0, 0 kg) to (200, 1316 kg)
 * - 200..250 bar: linear from (200, 1316 kg) to (250, 2829 kg)
 */
export function pressureToKg(pressureBar: number): number {
  if (pressureBar <= 0) {
    return 0;
  }
  if (pressureBar <= 200) {
    return (pressureBar / 200) * 1316;
  }
  // 200-250 range
  return 1316 + ((pressureBar - 200) / 50) * (2829 - 1316);
}

/**
 * Convert mass (kg) to pressure (bar) using the inverse piecewise formula.
 *
 * - 0..1316 kg: linear from (0 kg, 0 bar) to (1316 kg, 200 bar)
 * - 1316..2829 kg: linear from (1316 kg, 200 bar) to (2829 kg, 250 bar)
 */
export function kgToPressure(kg: number): number {
  if (kg <= 0) {
    return 0;
  }
  if (kg <= 1316) {
    return (kg / 1316) * 200;
  }
  if (kg <= 2829) {
    return 200 + ((kg - 1316) / (2829 - 1316)) * 50;
  }
  return 250;
}

/**
 * Convert mass (kg) to energy (MWh).
 * Formula: MWh = (kg / 1000) * 15.2
 */
export function kgToMwh(kg: number): number {
  return (kg / 1000) * 15.2;
}

/**
 * Convert energy (MWh) to mass (kg).
 * Formula: kg = (MWh / 15.2) * 1000
 */
export function mwhToKg(mwh: number): number {
  return (mwh / 15.2) * 1000;
}

/**
 * Format kg value for display.
 */
export function formatKg(kg: number): string {
  return kg.toFixed(1);
}

/**
 * Format MWh value for display.
 */
export function formatMwh(mwh: number): string {
  return mwh.toFixed(2);
}

/**
 * Format pressure value for display.
 */
export function formatPressure(bar: number): string {
  return `${bar} bar`;
}
