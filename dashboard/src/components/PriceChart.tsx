import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  type IChartApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Bar } from "../api";

interface Candle {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
}

export function PriceChart({ bars, symbol }: { bars: Bar[]; symbol: string }) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);

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
      timeScale: { borderColor: "#263230", timeVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      autoSize: true,
    });

    const seen = new Map<UTCTimestamp, Bar>();
    for (const bar of bars) {
      const stamp = bar.ts.endsWith("Z") ? bar.ts : `${bar.ts}Z`;
      const d = new Date(stamp);
      if (isNaN(d.getTime())) continue;
      seen.set(Math.floor(d.getTime() / 1000) as UTCTimestamp, bar);
    }

    const candles: Candle[] = [...seen.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([time, bar]) => ({
        time,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      }));

    if (candles.length > 0) {
      instance.addSeries(CandlestickSeries, {
        upColor: "#3fb98c",
        downColor: "#e2574c",
        borderUpColor: "#3fb98c",
        borderDownColor: "#e2574c",
        wickUpColor: "#3fb98c",
        wickDownColor: "#e2574c",
      }).setData(candles);

      instance.timeScale().fitContent();
    }

    chart.current = instance;
    return () => {
      instance.remove();
      chart.current = null;
    };
  }, [bars, symbol]);

  return <div className="chart" ref={container} />;
}