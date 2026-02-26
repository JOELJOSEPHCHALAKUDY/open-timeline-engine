import assert from "node:assert/strict";
import test from "node:test";

import { buildEventHeaders, normalizeApiUrl, shouldIgnoreCommand } from "./helpers";

test("normalizeApiUrl strips trailing slash", () => {
  assert.equal(normalizeApiUrl("http://localhost:8080/"), "http://localhost:8080");
  assert.equal(normalizeApiUrl("http://localhost:8080"), "http://localhost:8080");
});

test("shouldIgnoreCommand matches ignored prefixes", () => {
  assert.equal(shouldIgnoreCommand("tce.startTask", ["tce.", "workbench."]), true);
  assert.equal(shouldIgnoreCommand("git.openRepository", ["tce.", "workbench."]), false);
});

test("buildEventHeaders returns required TCE scope and auth headers", () => {
  const headers = buildEventHeaders({
    apiToken: "test-token",
    consumerId: "vscode-user",
    role: "user",
    workspaceId: "personal",
    userId: "joel"
  });
  assert.equal(headers.Authorization, "Bearer test-token");
  assert.equal(headers["X-TCE-Consumer"], "vscode-user");
  assert.equal(headers["X-TCE-Role"], "user");
  assert.equal(headers["X-TCE-Workspace"], "personal");
  assert.equal(headers["X-TCE-User"], "joel");
});
