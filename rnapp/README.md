# StratBot React Native Android App

A React Native (Expo) port of the StratBot dashboard, built from the existing
Vite + React web app in `dashboard/`. The app mirrors the 5-page web dashboard
and runs on Android as a standalone APK.

## Pages

| Tab       | Web page                | What it shows                       |
|-----------|-------------------------|-------------------------------------|
| Overview  | OverviewPage            | Equity, exposure, pending orders    |
| Markets   | MarketsPage             | Symbol watchlist + price chart      |
| Orders    | OrdersPage              | Queued + executed fills             |
| Positions | PositionsPage           | Open position book                  |
| Risk      | RiskPage                | Risk limits, events, kill switch    |

## Layout

The web dashboard has a fixed left sidebar on desktop and a bottom tab bar
on mobile. The RN app uses a bottom tab navigator (React Navigation) with the
same 5 tabs, which is the correct mobile layout.

## Styling

All visual tokens (colors, spacing, radii) live in `src/theme.ts`. The
instrument-panel look — brass accents, green/red only on money — is preserved
from the web dashboard's `styles.css`.

## Charts

`lightweight-charts` does not run in React Native. The web dashboard's
`EquityChart` and `PriceChart` are replaced by `react-native-svg` based
components (see `src/components/`).

## API

The API client in `src/api.ts` is a direct port of `dashboard/src/api.ts`.
Set `EXPO_PUBLIC_API_URL` to point at your FastAPI server, e.g.:

```bash
EXPO_PUBLIC_API_URL=http://192.168.1.10:8000
```

If unset, defaults to empty string (same-origin when served from the API).

## Build

```bash
cd rnapp
npm install
npx expo start --android
```

To build an APK:
```bash
npx expo build:android
```

## Running the bot

The RN app is a client for the existing Python FastAPI server. Start the bot
and server as usual:

```powershell
.\\.venv\\Scripts\\python run_bot.py --forever --sleep 900 --serve
```