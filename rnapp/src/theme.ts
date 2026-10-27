export const colors = {
  bg: "#0e1312",
  surface: "#141a19",
  surface2: "#1b2322",
  border: "#263230",
  rule: "#1e2826",
  text: "#e6ecea",
  muted: "#8c9c97",
  dim: "#5d6b67",
  brass: "#d9a441",
  brassSoft: "rgba(217,164,65,0.12)",
  up: "#3fb98c",
  down: "#e2574c",
} as const;

export type ColorName = keyof typeof colors;

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 20,
  xxl: 24,
} as const;

export const radii = {
  sm: 4,
  md: 6,
  lg: 8,
  pill: 999,
} as const;

export const fontSizes = {
  xs: 10,
  sm: 11,
  md: 13,
  lg: 15,
  xl: 17,
  xxl: 21,
  figure: 28,
} as const;