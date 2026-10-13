import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AppHeader } from "./AppHeader";

function renderHeader(props: Partial<React.ComponentProps<typeof AppHeader>> = {}) {
  return render(
    <AppHeader
      activeTab="overview"
      onTabChange={() => {}}
      connected
      onRefresh={() => {}}
      updatedLabel={null}
      {...props}
    />,
  );
}

describe("AppHeader", () => {
  it("renders the refresh button spinning, disabled and busy only while refreshing", () => {
    const view = renderHeader({ refreshing: true });

    const busy = screen.getByRole("button", { name: "Refresh now" });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(busy).toHaveClass("spinning");
    view.unmount();

    renderHeader();
    const idle = screen.getByRole("button", { name: "Refresh now" });
    expect(idle).toBeEnabled();
    expect(idle).not.toHaveClass("spinning");
  });

  it("marks the active tab and calls onTabChange when another tab is clicked", () => {
    const onTabChange = vi.fn();
    renderHeader({ onTabChange });

    expect(screen.getByRole("link", { name: "Overview" })).toHaveClass("active");
    expect(screen.getByRole("link", { name: "Sessions" })).not.toHaveClass("active");
    expect(screen.queryByRole("link", { name: "Kanban" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "Sessions" }));
    expect(onTabChange).toHaveBeenCalledWith("sessions");
  });

  it("shows the disconnected title on the status dot when not connected", () => {
    renderHeader({ connected: false });

    expect(screen.getByTitle("Can't reach the local server")).toBeInTheDocument();
  });

  // The mobile-collapse dropdown is CSS-hidden above 900px, not
  // React-unmounted - always in the DOM, so it's testable independent of
  // viewport width the same way the full-width `nav.app-tabs` already is.
  it("the collapsed-nav dropdown is labeled with the active tab and lists the other tab", () => {
    const onTabChange = vi.fn();
    renderHeader({ activeTab: "sessions", onTabChange });

    expect(screen.getByTestId("nav-dropdown")).toHaveTextContent("Sessions");
    fireEvent.click(screen.getByTestId("nav-dropdown"));
    expect(screen.getByTestId("nav-dropdown-option-overview")).toBeInTheDocument();
    expect(screen.queryByTestId("nav-dropdown-option-sessions")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("nav-dropdown-option-overview"));
    expect(onTabChange).toHaveBeenCalledWith("overview");
  });

  it("names the app cairn/meter", () => {
    const { container } = renderHeader();

    expect(container.querySelector(".wordmark")).toHaveTextContent("cairn/meter");
  });

  it("mounts the accent picker", () => {
    renderHeader();

    expect(screen.getByTestId("accent-picker-btn")).toBeInTheDocument();
  });

  it("has no attention pill or mute toggle", () => {
    renderHeader();

    expect(screen.queryByTestId("attention-pill")).not.toBeInTheDocument();
    expect(screen.queryByTestId("attention-mute-toggle")).not.toBeInTheDocument();
  });
});
