export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return String(n);
}

export function formatCost(cost: number | "unknown" | null): string {
  if (cost === null || cost === "unknown") return "unknown";
  return `$${cost.toFixed(2)}`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  return `${seconds.toFixed(1)}s`;
}

export function formatTimeOfDay(iso: string): string {
  // ISO strings from server.py are UTC ("...Z"); Date's local-time getters
  // render the viewer's own time zone, which the browser already knows.
  const d = new Date(iso);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

export function formatDayLabel(dateStr: string): string {
  // dateStr: "YYYY-MM-DD"
  const [, month, day] = dateStr.split("-");
  return `${month}-${day}`;
}

// Readable form of a "YYYY-MM-DD" calendar day, e.g. "Sep 28, Mon" for the
// current year, or "Jan 5 2019, Sat" for a past/future year. Parsed and
// formatted as a UTC calendar date (not local time) so the displayed date
// always matches the literal input regardless of the viewer's time zone -
// same UTC-day convention as Overview's sessionOverlapsDate/eventOnDate
// day-boundary checks. `now` defaults to the real clock but is injectable
// so "is this the current year" is testable without depending on it.
export function formatDateLabel(dateStr: string, now: Date = new Date()): string {
  const [year, month, day] = dateStr.split("-").map(Number);
  const d = new Date(Date.UTC(year, month - 1, day));
  const monthName = d.toLocaleString("en-US", { month: "short", timeZone: "UTC" });
  const weekday = d.toLocaleString("en-US", { weekday: "short", timeZone: "UTC" });
  const yearPart = year === now.getUTCFullYear() ? "" : ` ${year}`;
  return `${monthName} ${d.getUTCDate()}${yearPart}, ${weekday}`;
}

// Fallback label for a session with no saved title yet (parser.py found no
// "ai-title" record) - first 8 chars of the session's full UUID, matching
// the mockup's `a8de42…c884`-style short id.
export function shortId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 6)}…${id.slice(-4)}`;
}

export function formatStarted(iso: string): string {
  const d = new Date(iso);
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${year}-${month}-${day} ${hh}:${mm}`;
}

// Session-total runtime ("41m", "1h 05m") - mm:ss/h:mm reads better than
// formatDuration's decimal-seconds form (built for the ~seconds-long call
// rows in a Drilldown) once a session runs to minutes or hours.
export function formatSessionDuration(startIso: string, endIso: string): string {
  const totalSeconds = Math.max(0, (new Date(endIso).getTime() - new Date(startIso).getTime()) / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m`;
  return `${Math.round(totalSeconds)}s`;
}

export function formatRelativeToNow(iso: string, now: Date = new Date()): string {
  const then = new Date(iso).getTime();
  const diffMs = now.getTime() - then;
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return `${Math.max(0, diffSec)}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  return `${diffHr}h ago`;
}
