import { defineConfig } from "@capacitor/cli";

/**
 * Conversion-ready config for turning the built PWA into an Android APK.
 *
 * Two things must stay correct here or the APK will load and then silently fail
 * every API call:
 *   1. CapacitorHttp runs requests natively, bypassing the mixed-content block
 *      that stops an https://localhost webview from reaching an http:// server.
 *   2. `androidScheme` stays https so cleartext is off by default.
 *
 * Relative /api/* paths mean no base URL has to be baked into the APK.
 */
const config: CapacitorConfig = {
  appId: "com.stratbot.dashboard",
  appName: "StratBot",
  webDir: "dist",
  server: {
    androidScheme: "https",
    cleartext: false,
  },
  plugins: {
    CapacitorHttp: {
      enabled: true,
    },
  },
};

export default config;