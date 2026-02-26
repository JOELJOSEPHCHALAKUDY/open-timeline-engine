import assert from "node:assert/strict";
import test from "node:test";

import { classifyDomainByUrl, hostFromUrl, normalizeHost, parseAllowedSites } from "./helpers";

test("hostFromUrl extracts normalized host", () => {
  assert.equal(hostFromUrl("https://GitHub.com/OpenAI"), "github.com");
});

test("classifyDomainByUrl detects coding sources", () => {
  assert.equal(classifyDomainByUrl("https://github.com/org/repo"), "coding");
  assert.equal(classifyDomainByUrl("https://docs.python.org/3/"), "research");
});

test("normalizeHost accepts host or URL and rejects invalid entries", () => {
  assert.equal(normalizeHost("stackoverflow.com"), "stackoverflow.com");
  assert.equal(normalizeHost("https://developer.mozilla.org"), "developer.mozilla.org");
  assert.equal(normalizeHost("not a url%%%"), null);
});

test("parseAllowedSites deduplicates and normalizes", () => {
  assert.deepEqual(
    parseAllowedSites("github.com\nhttps://github.com,stackoverflow.com"),
    ["github.com", "stackoverflow.com"]
  );
});
