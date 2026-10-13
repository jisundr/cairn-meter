import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { App } from "./App";
import type { SessionSummary } from "./api/types";
import { envelope, installFetchMock } from "./test/mockApi";
import { renderWithClient } from "./test/renderWithClient";

const SESSION: SessionSummary = {
  session_id: "a8de42aabbccddeeffgg1234c884",
  project: "cairn-2.0",
  started: "2026-09-25T11:58:00Z",
  ended: "2026-09-25T12:39:00Z",
  agents: ["builder"],
  calls: 12,
  tokens: 184204,
  cost: 12.4,
  usage_limit_hit: false,
};

function installSessionsHandlers() {
  return installFetchMock({
    "/api/projects": () => envelope({ hostname: "test-host", projects: [{ label: "cairn-2.0", parent: null }] }),
    "/api/rollup/session": () => envelope([SESSION]),
  });
}

describe("App", () => {
  // A path/search set by an earlier test in this file would otherwise leak
  // into the next one's initial read.
  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  // Regression: the mount effect that normalizes the URL used to rebuild it
  // from the pathname alone, silently dropping any query string the page
  // just loaded with.
  it("preserves the query string already in the URL when it mounts", async () => {
    window.history.pushState(null, "", "/sessions?sort=tokens&dir=desc&page=1");
    installSessionsHandlers();
    renderWithClient(<App />);

    await waitFor(() => expect(screen.getByTestId("sessions-table")).toBeInTheDocument());
    expect(window.location.pathname).toBe("/sessions");
    const params = new URLSearchParams(window.location.search);
    expect(params.get("sort")).toBe("tokens");
    expect(params.get("dir")).toBe("desc");
    expect(params.get("page")).toBe("1");
  });

  it("offers Overview and Sessions as the only tabs", async () => {
    window.history.pushState(null, "", "/sessions");
    installSessionsHandlers();
    renderWithClient(<App />);

    await waitFor(() => expect(screen.getByTestId("sessions-table")).toBeInTheDocument());
    const tabs = screen.getAllByRole("link").map((a) => a.textContent);
    expect(tabs).toEqual(["Overview", "Sessions"]);
  });

  it("opens the Overview for a path it no longer serves, such as /kanban", async () => {
    window.history.pushState(null, "", "/kanban");
    installSessionsHandlers();
    renderWithClient(<App />);

    await waitFor(() => expect(window.location.pathname).toBe("/"));
  });

  it("follows browser back/forward between Sessions and Overview", async () => {
    window.history.pushState(null, "", "/sessions");
    installSessionsHandlers();
    renderWithClient(<App />);
    await waitFor(() => expect(screen.getByTestId("sessions-table")).toBeInTheDocument());

    window.history.pushState(null, "", "/");
    fireEvent.popState(window);

    await waitFor(() => expect(screen.queryByTestId("sessions-table")).not.toBeInTheDocument());
  });
});
