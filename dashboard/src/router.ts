import { useEffect, useState } from "react";

/**
 * Hash routing, deliberately hand-rolled.
 *
 * The dashboard is a static bundle served from disk and inside a Capacitor
 * webview, where there is no server to rewrite deep links. Hash routing keeps
 * every page reachable from a cold start with no router dependency and no
 * history-server config.
 */
export function useHashRoute(fallback: string) {
  const read = () => window.location.hash.replace(/^#\/?/, "").trim() || fallback;

  const [route, setRoute] = useState(read);

  useEffect(() => {
    const onChange = () => setRoute(read());
    window.addEventListener("hashchange", onChange);
    if (!window.location.hash) window.location.hash = `/${fallback}`;
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return { route, navigate: (to: string) => { window.location.hash = `/${to}`; } };
}