import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
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
        background: { type: ColorType.Solid, color: "#131722" },
        textColor: "#8b93a7",
        fontSize: 11,
      },
      grid: { vertLines: { color: "#1c2130" }, horzLines: { color: "#1c2130" } },
      rightPriceScale: { borderColor: "#232838" },
      timeScale: { borderColor: "#232838", timeVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      autoSize: true,
    });

    const seen = new Map<UTCTimestamp, Bar>();
    for (const bar of bars) {
      const stamp = bar.ts.endsWith("Z") ? bar.ts : `${bar.ts}Z`;
      seen.set(Math.floor(new Date(stamp).getTime() / 1000) as UTCTimestamp, bar);
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
        upColor: "#26a69a",
        downColor: "#ef5350",
        borderUpColor: "#26a69a",
        borderDownColor: "#ef5350",
        wickUpColor: "#26a69a",
        wickDownColor: "#ef5350",
      }).setData(candles);

      instance.addSeries(LineSeries, {
        color: "#4c8dff",
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      }).setData(candles.map((candle) => ({ time: candle.time, value: candle.close })));

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