import { geographyCsv } from "../src/result-export.js";
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

test("map export keeps signed coordinates numeric, unknown counts empty, and site names literal", () => {
 const csv=geographyCsv({siteLocations:[{siteId:' =1+1',longitude:-73.5,latitude:41.2,count:null,dataSource:'synthetic',payload:'omit-secret'}],siteId:'site-a',speciesId:'fish',generatedAt:10},true);
 assert.ok(csv.includes('1970-01-01T00:00:10.000Z')); assert.ok(csv.includes('缓存快照'));
 assert.ok(csv.includes(`"' =1+1","-73.5","41.2","","合成"`));
 assert.ok(!csv.includes('omit-secret'));
});
