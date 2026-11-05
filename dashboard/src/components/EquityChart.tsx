import {
  AreaSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

export interface EquityPoint {
  ts: string;
  equity: number;
}

export function EquityChart({ points }: { points: EquityPoint[] }) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Area"> | null>(null);

  useEffect(() => {
    if (!container.current) return;

    const instance = createChart(container.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#141a19" },
        textColor: "#8c9c97",
        fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
        fontSize: 11,
      },
      grid: { vertLines: { color: "#1e2826" }, horzLines: { color: "#1e2826" } },
      rightPriceScale: { borderColor: "#263230" },
      timeScale: { borderColor: "#263230", timeVisible: true, secondsVisible: false },
      autoSize: true,
    });

    series.current = instance.addSeries(AreaSeries, {
      lineColor: "#d9a441",
      topColor: "rgba(217,164,65,0.20)",
      bottomColor: "rgba(217,164,65,0.01)",
      lineWidth: 2,
      priceFormat: { type: "price", precision: 0, minMove: 1 },
    });

    chart.current = instance;
    return () => {
      instance.remove();
      chart.current = null;
      series.current = null;
    };
  }, []);

  useEffect(() => {
    const target = series.current;
    if (!target || points.length === 0) return;

    // The chart needs strictly ascending time; the API makes no such guarantee.
    const seen = new Map<UTCTimestamp, number>();
    for (const point of points) {
      const d = new Date(point.ts);
      if (isNaN(d.getTime())) continue;
      seen.set(Math.floor(d.getTime() / 1000) as UTCTimestamp, point.equity);
    }
    const ordered = [...seen.entries()].sort((a, b) => a[0] - b[0]);

    target.setData(ordered.map(([time, value]) => ({ time, value })));
    chart.current?.timeScale().fitContent();
  }, [points]);

  return <div className="chart" ref={container} />;
}