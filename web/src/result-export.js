import { filterSourceEvents } from "./source-filter.js";
// Export only the bounded, already authorized page; never include raw payloads or credentials.
export function sourceExport(events, filters, scope) {
  if (!Array.isArray(events) || events.length > 500)
    throw Error("当前页来源数量超过导出上限。");
  const rows = filterSourceEvents(events, filters, []).rows.map((e) => ({
    eventId: e.eventId,
    eventType: e.eventType,
    occurredAt: e.occurredAt,
    deviceId: e.deviceId,
    imageAvailable: Boolean(e.imageAvailable),
  }));
  return {
    schema: "marine.source-page/v1",
    siteId: String(scope.siteId).slice(0, 128),
    dataSource: ["synthetic","field"].includes(scope.dataSource) ? scope.dataSource : "unconfirmed",
    
    coverage: "当前已载入页，未加载页不在此导出中；数据来源请以站点登记为准",
    exportedAt: new Date().toISOString(),
    rows,
  };
}
export function sourceCsv(packet) {
  const fields = [
    "eventId",
    "eventType",
    "occurredAt",
    "deviceId",
    "imageAvailable",
  ];
  const cell = (value) =>
    '"' +
    String(value ?? "")
      .replace(/^[=+@\-\t\r]/, "'$&")
      .replaceAll('"', '""') +
    '"';
  return (
    "\ufeff" +
    [
      fields.join(","),
      ...packet.rows.map((r) => fields.map((k) => cell(r[k])).join(",")),
    ].join("\r\n")
  );
}

export function sourceScopeMatches(binding, actor, siteId) {
 return Boolean(binding && actor && typeof actor.scopeHash === 'string' && actor.scopeHash && binding.scopeHash === actor.scopeHash && binding.siteId === siteId);
}
