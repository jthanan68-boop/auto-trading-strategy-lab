import { PriceChart } from "../components/PriceChart";
import { SessionBar } from "../components/SessionBar";
import { money, type Bot } from "../useBot";

/**
 * The live price when a quote is available, the newest bar's close when it is
 * not, and the move against the prior daily close either way.
 */
function quote(bot: Bot, name: string) {
  const series = bot.bars[name];
  const live = bot.quotes[name];
  const last = series?.at(-1);
  const prev = series?.at(-2);
  const price = live?.usable ? live.price : last?.close;
  if (price === undefined) return null;
  const basis = prev?.close ?? price;
  return {
    price,
    change: basis === 0 ? 0 : ((price - basis) / basis) * 100,
    live: live?.usable ?? false,
    source: live?.source,
    age: live?.age_seconds,
    delayed: live?.delayed ?? false,
  };
}

function age(seconds: number | undefined): string {
  if (seconds === undefined) return "";
  if (seconds < 90) return `${Math.round(seconds)}s old`;
  return `${Math.round(seconds / 60)}m old`;
}

export function MarketsPage({ bot }: { bot: Bot }) {
  const { status, symbol, setSymbol, bars, barsState } = bot;
  const active = bars[symbol] ?? [];
  const shown = quote(bot, symbol);

  return (
    <>
      <SessionBar bars={active} symbol={symbol} />

      <div className="split">
        <nav className="rail" aria-label="Symbols">
          {(status?.symbols ?? []).map((name) => {
            const q = quote(bot, name);
            const stale = bot.barsState[name] !== "ready";
            return (
              <button
                key={name}
                className={`rail-item ${name === symbol ? "on" : ""}`}
                onClick={() => setSymbol(name)}
                aria-current={name === symbol}
              >
                <span className="rail-sym">
                  {name}
                  {q?.live && <span className="rail-dot" title="live price" aria-hidden="true" />}
                </span>
                {q ? (
                  <>
                    <span className="rail-price">{money(q.price)}</span>
                    <span className={`rail-change ${q.change >= 0 ? "pos" : "neg"}`}>
                      {q.change >= 0 ? "+" : ""}{q.change.toFixed(2)}%
                    </span>
                  </>
                ) : (
                  <span className="rail-price dim">{stale ? "loading" : "-"}</span>
                )}
              </button>
            );
          })}
        </nav>

        <section className="panel grow">
          <div className="panel-head">
            <h2 className="panel-title">{symbol}</h2>
            {shown && (
              <span className="panel-aside mono">
                {money(shown.price)}{" "}
                <span className={shown.change >= 0 ? "pos" : "neg"}>
                  {shown.change >= 0 ? "+" : ""}{shown.change.toFixed(2)}%
                </span>{" "}
                <span className="dim">vs prior close</span>
              </span>
            )}
          </div>
          <div className="panel-body">
            {barsState[symbol] === "ready" && active.length ? (
              <PriceChart bars={active} symbol={symbol} />
            ) : barsState[symbol] === "error" ? (
              <div className="empty">
                No cached price history for {symbol}. Run the strategy lab once to populate the cache.
              </div>
            ) : (
              <div className="empty">Loading {symbol} price history</div>
            )}
          </div>
          <p className="panel-foot dim">
            {shown?.live
              ? `Live price from ${shown.source}, ${age(shown.age)}${shown.delayed ? " (delayed by the venue)" : ""}. `
              : "No live quote; showing the newest daily close. "}
            Daily candles from Yahoo Finance. The newest candle is the current session and is still
            forming until the market closes.
          </p>
        </section>
      </div>
    </>
  );
}