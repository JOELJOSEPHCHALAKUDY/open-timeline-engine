import assert from "node:assert/strict";
import test from "node:test";

import { normalizeTakeoverMode, personaActivationPhrase, personaStopPhrase } from "./takeover";

test("normalizeTakeoverMode resolves supported values", () => {
  assert.equal(normalizeTakeoverMode("takeover"), "takeover");
  assert.equal(normalizeTakeoverMode("suggest"), "suggest");
  assert.equal(normalizeTakeoverMode("other"), "takeover");
});

test("persona phrases for takeover mode", () => {
  assert.equal(personaActivationPhrase("normal"), "hey advisor take over");
  assert.equal(personaActivationPhrase("naruto"), "hey kurama take over");
  assert.equal(personaActivationPhrase("shadow"), "hey beru take over");
  assert.equal(personaStopPhrase("normal"), "advisor stand down");
  assert.equal(personaStopPhrase("naruto"), "kurama stand down");
  assert.equal(personaStopPhrase("shadow"), "shadow stand down");
});
