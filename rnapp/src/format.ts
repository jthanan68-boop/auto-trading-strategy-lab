export function money(value: number): string {
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export function signed(value: number): string {
  return `${value >= 0 ? "+" : ""}${money(value)}`;
}

export function qty(value: number): string {
  return value < 1 ? value.toFixed(6) : value.toFixed(4);
}

export function pct(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

export function shortDate(ts: string): string {
  const d = new Date(ts.endsWith("Z") ? ts : `${ts}Z`);
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" }).format(d);
}

export function time(ts: string): string {
  const d = new Date(ts.endsWith("Z") ? ts : `${ts}Z`);
  return d.toLocaleTimeString("en-IN", { timeStyle: "medium" });
}

export function dateTime(ts: string): string {
  const d = new Date(ts.endsWith("Z") ? ts : `${ts}Z`);
  return d.toLocaleString("en-IN", { dateStyle: "short", timeStyle: "medium" });
}