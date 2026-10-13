import { useState } from "react";
import { AccentPicker } from "./AccentPicker";
import { RefreshIcon } from "./icons";

// Header block shared by every tab-bar screen (Overview, Sessions List). The
// Session Drilldown screen replaces this entirely with its own
// `.drill-header` (App.tsx never mounts this component while a session is
// open). `showTabs`/`onRefresh`/`updatedLabel`/`refreshing` are each
// independently optional because the screens don't render the same
// right-hand cluster: the empty state keeps the nav but drops
// refresh/updated-at; the disconnected state drops the nav entirely too;
// the sessions list keeps refresh but never shows updated-at.
//
// Below design.css's 900px breakpoint, `nav.app-tabs` hides and a
// `.dropdown-btn.nav-dropdown` (labeled with the active tab) takes over
// navigating between the same tabs - CSS-only for which one is visible,
// both always rendered so no JS breakpoint check duplicates that logic.
export type AppTab = "overview" | "sessions";

const ALL_TABS: AppTab[] = ["overview", "sessions"];
const TAB_LABEL: Record<AppTab, string> = { overview: "Overview", sessions: "Sessions" };
const TAB_PATH: Record<AppTab, string> = { overview: "/", sessions: "/sessions" };

interface AppHeaderProps {
  activeTab: AppTab;
  onTabChange: (tab: AppTab) => void;
  connected: boolean;
  showTabs?: boolean;
  onRefresh?: () => void;
  updatedLabel?: string | null;
  // True while a click-triggered refresh is in flight: the refresh button
  // spins and is disabled. Pages that omit it are unchanged.
  refreshing?: boolean;
}

export function AppHeader({
  activeTab,
  onTabChange,
  connected,
  showTabs = true,
  onRefresh,
  updatedLabel,
  refreshing,
}: AppHeaderProps) {
  const [navOpen, setNavOpen] = useState(false);

  return (
    <header className="app">
      <div className="app-left">
        <span className="wordmark">
          <span className="dim">~/ </span>cairn<span className="sep">/</span>
          <span className="sub">meter</span>
        </span>
        {showTabs && (
          <>
            <nav className="app-tabs">
              {ALL_TABS.map((tab) => (
                <a
                  key={tab}
                  href={TAB_PATH[tab]}
                  className={activeTab === tab ? "active" : undefined}
                  onClick={(e) => {
                    e.preventDefault();
                    onTabChange(tab);
                  }}
                >
                  {TAB_LABEL[tab]}
                </a>
              ))}
            </nav>
            <div className="dropdown-wrap">
              <button
                type="button"
                className="dropdown-btn nav-dropdown"
                data-testid="nav-dropdown"
                onClick={() => setNavOpen((o) => !o)}
              >
                {TAB_LABEL[activeTab]}
                <svg viewBox="0 0 24 24" fill="none" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="6 9 12 15 18 9" />
                </svg>
              </button>
              {navOpen && (
                <div className="dropdown-menu" data-testid="nav-dropdown-menu">
                  {ALL_TABS.filter((tab) => tab !== activeTab).map((tab) => (
                    <div
                      key={tab}
                      className="dropdown-item"
                      data-testid={`nav-dropdown-option-${tab}`}
                      onClick={() => {
                        onTabChange(tab);
                        setNavOpen(false);
                      }}
                    >
                      {TAB_LABEL[tab]}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        )}
      </div>
      <div className="app-right">
        <AccentPicker />
        <span
          className={connected ? "status-dot" : "status-dot err"}
          title={connected ? "Connected to the local server" : "Can't reach the local server"}
        />
        {onRefresh && (
          <button
            className={refreshing ? "icon-btn spinning" : "icon-btn"}
            title="Refresh now"
            aria-label="Refresh now"
            aria-busy={refreshing}
            disabled={refreshing}
            onClick={onRefresh}
          >
            <RefreshIcon />
          </button>
        )}
        {updatedLabel && <span className="updated-at mono">{updatedLabel}</span>}
      </div>
    </header>
  );
}
