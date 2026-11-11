import { test } from "node:test";
import assert from "node:assert/strict";
import { sourceExport, sourceCsv, sourceScopeMatches } from "../src/result-export.js";
test("source export filters the current page and excludes private payloads", () => {
  const packet = sourceExport(
    [
      {
        eventId: "=cmd",
        eventType: "environment.reading",
        occurredAt: "2026-11-09T00:00:00Z",
        deviceId: "sensor-1",
        rawPayload: "PRIVATE",
        token: "SECRET",
      },
    ],
    { query: "", type: "environment.reading", imagesOnly: false },
    { siteId: "synthetic-area-A", speciesId: "saithe" },
  );
  assert.equal(packet.rows.length, 1);
  assert.equal(JSON.stringify(packet).includes("SECRET"), false);
  assert.equal(JSON.stringify(packet).includes("PRIVATE"), false);
  assert.match(sourceCsv(packet), /"'=cmd"/);
  assert.equal(
    sourceExport(
      packet.rows,
      { query: "not-found", type: "all", imagesOnly: false },
      { siteId: "s", speciesId: "f" },
    ).rows.length,
    0,
  );
  assert.throws(() => sourceExport(Array(1001).fill({}), {}, {}));
});

test('source export refuses a changed site or authenticated scope',()=>{
 const binding={scopeHash:'account-a',siteId:'site-a'};
 assert.equal(sourceScopeMatches(binding,{scopeHash:'account-a'},'site-a'),true);
 assert.equal(sourceScopeMatches(binding,{scopeHash:'account-a'},'site-b'),false);
 assert.equal(sourceScopeMatches(binding,{scopeHash:'account-b'},'site-a'),false);
 assert.equal(sourceScopeMatches(binding,null,'site-a'),false);
});
