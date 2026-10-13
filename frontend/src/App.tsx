import { useEffect, useState } from "react";
import type { AppTab } from "./components/AppHeader";
import { Drilldown } from "./pages/Drilldown";
import { Overview } from "./pages/Overview";
import { SessionsList } from "./pages/SessionsList";

type View = { kind: "tab"; tab: AppTab } | { kind: "session"; sessionId: string };

function pathForView(view: View): string {
  if (view.kind === "session") return `/sessions/${encodeURIComponent(view.sessionId)}`;
  return view.tab === "sessions" ? "/sessions" : "/";
}

function parseView(pathname: string): View {
  const sessionMatch = pathname.match(/^\/sessions\/(.+)$/);
  if (sessionMatch) return { kind: "session", sessionId: decodeURIComponent(sessionMatch[1]) };
  return { kind: "tab", tab: pathname.startsWith("/sessions") ? "sessions" : "overview" };
}

// Path-based nav, no router library, so a hard reload of /sessions or
// /sessions/<id> lands back on the same view instead of resetting to
// Overview. Each page owns its own `.shell` + header (their header props
// differ too much across states - e.g. Overview/Disconnected drops the nav
// entirely - to hoist into one shared wrapper here).
export function App() {
  const initialView = parseView(window.location.pathname);
  const [activeTab, setActiveTabState] = useState<AppTab>(initialView.kind === "tab" ? initialView.tab : "sessions");
  const [viewSessionId, setViewSessionId] = useState<string | null>(
    initialView.kind === "session" ? initialView.sessionId : null,
  );

  function navigateToTab(tab: AppTab) {
    setActiveTabState(tab);
    setViewSessionId(null);
    const path = pathForView({ kind: "tab", tab });
    if (path !== window.location.pathname) window.history.pushState(null, "", path);
  }

  function navigateToSession(sessionId: string) {
    setViewSessionId(sessionId);
    setActiveTabState("sessions");
    const path = pathForView({ kind: "session", sessionId });
    if (path !== window.location.pathname) window.history.pushState(null, "", path);
  }

  useEffect(() => {
    // Regression guard (App.test.tsx): each page owns its own query-string
    // state (sort/dir/page/range/project) that this normalization must not
    // silently drop, so the original search string is kept verbatim.
    const view = parseView(window.location.pathname);
    window.history.replaceState(null, "", pathForView(view) + window.location.search);
  }, []);

  useEffect(() => {
    function onPopState() {
      const view = parseView(window.location.pathname);
      if (view.kind === "session") {
        setViewSessionId(view.sessionId);
        setActiveTabState("sessions");
      } else {
        setViewSessionId(null);
        setActiveTabState(view.tab);
      }
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  if (viewSessionId) {
    return (
      <Drilldown
        sessionId={viewSessionId}
        activeTab={activeTab}
        onTabChange={navigateToTab}
        onBack={() => navigateToTab("sessions")}
      />
    );
  }

  if (activeTab === "overview") {
    return <Overview activeTab={activeTab} onTabChange={navigateToTab} />;
  }

  return <SessionsList activeTab={activeTab} onTabChange={navigateToTab} onSelectSession={navigateToSession} />;
}
