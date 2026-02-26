import assert from "node:assert/strict";
import test from "node:test";

import { personaActivationPhrase, personaStopPhrase, resolveTakeoverMode } from "../takeover";

test("resolveTakeoverMode defaults to takeover", () => {
  assert.equal(resolveTakeoverMode("takeover"), "takeover");
  assert.equal(resolveTakeoverMode("suggest"), "suggest");
  assert.equal(resolveTakeoverMode("invalid"), "takeover");
});

test("persona phrase helpers return expected defaults", () => {
  assert.equal(personaActivationPhrase("normal"), "hey advisor take over");
  assert.equal(personaActivationPhrase("naruto"), "hey kurama take over");
  assert.equal(personaActivationPhrase("shadow"), "hey beru take over");
  assert.equal(personaStopPhrase("normal"), "advisor stand down");
  assert.equal(personaStopPhrase("naruto"), "kurama stand down");
  assert.equal(personaStopPhrase("shadow"), "shadow stand down");
});
