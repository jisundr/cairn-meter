import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

// Two fixtures: "populated" (seeded via e2e/fixtures/seed.py, under a
// scratch HOME so server.py's import-time DEFAULT_STORE_DIR/
// DEFAULT_CLAUDE_PROJECTS_DIR resolution can't pick up this machine's real
// sessions) and "cold-start" (a scratch HOME with no store at all).
//
// Each fixture is a single `server.py` process: vite.config.ts's
// `build.outDir` points at `../static`, which server.py's own static_dir
// route serves alongside its JSON API, so there's no second `vite preview`
// process or `/api` proxy to run. `npm run build` must still precede
// `playwright test` - server.py serves whatever's already in `static/`,
// it doesn't build it.
const FRONTEND_ROOT = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(FRONTEND_ROOT, "..");
const SCRATCH_ROOT = path.resolve(FRONTEND_ROOT, ".e2e-scratch");
const POPULATED_SCRATCH = path.join(SCRATCH_ROOT, "populated");
const COLD_START_SCRATCH = path.join(SCRATCH_ROOT, "cold-start");

// Away from the product default (4318) so a running dashboard is never reused.
const POPULATED_PORT = 4518;
const COLD_START_PORT = 4520;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: "list",
  use: {
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "populated",
      testDir: "./e2e/populated",
      use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${POPULATED_PORT}` },
    },
    {
      name: "cold-start",
      testDir: "./e2e/cold-start",
      use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${COLD_START_PORT}` },
    },
  ],
  webServer: [
    {
      command: `python3 e2e/fixtures/seed.py "${POPULATED_SCRATCH}" && python3 "${REPO_ROOT}/server.py" ${POPULATED_PORT}`,
      url: `http://127.0.0.1:${POPULATED_PORT}/api/projects`,
      cwd: FRONTEND_ROOT,
      env: { HOME: POPULATED_SCRATCH },
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: `python3 "${REPO_ROOT}/server.py" ${COLD_START_PORT}`,
      url: `http://127.0.0.1:${COLD_START_PORT}/api/projects`,
      cwd: FRONTEND_ROOT,
      // Overridden too (not left pointing at the real machine's HOME) so
      // DEFAULT_STORE_DIR/DEFAULT_CLAUDE_PROJECTS_DIR (both computed once
      // from Path.home() at import time) can't pick up real sessions and
      // break the hermetic empty-state fixture.
      env: { HOME: COLD_START_SCRATCH },
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
  ],
});
