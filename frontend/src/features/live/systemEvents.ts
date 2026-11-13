export type SystemEventType =
  | "task.changed"
  | "worker.changed"
  | "program.changed"
  | "batch.changed"
  | "coordinator.changed"
  | "writeback.changed"
  | "asset.changed"
  | "model.changed"
  | "immich.changed"
  | "audit.changed"
  | "collection.changed"
  | "storage.changed";

export interface SystemEventEnvelope {
  event: SystemEventType;
  table?: string;
  entity_id?: string;
  asset_id?: string;
  connection_id?: string;
  batch_id?: string;
  group_id?: string;
}

export const SYSTEM_EVENT_TYPES: readonly SystemEventType[] = [
  "task.changed",
  "worker.changed",
  "program.changed",
  "batch.changed",
  "coordinator.changed",
  "writeback.changed",
  "asset.changed",
  "model.changed",
  "immich.changed",
  "audit.changed",
  "collection.changed",
  "storage.changed",
];

const IMMEDIATE_SYSTEM_EVENTS = new Set<SystemEventType>([
  "program.changed",
]);

const QUERY_KEYS_BY_EVENT: Record<SystemEventType, ReadonlyArray<readonly string[]>> = {
  "task.changed": [
    ["tasks"],
    ["task-counts"],
    ["coordinator-status"],
    ["library-work-summary"],
    ["dashboard"],
    ["processing-batches"],
    ["deep-review-backfills"],
    ["system-overview"],
    ["system-diagnostics"],
  ],
  "worker.changed": [
    ["workers"],
    ["coordinator-status"],
    ["system-overview"],
    ["system-diagnostics"],
    ["operations-summary"],
  ],
  "program.changed": [
    ["library-program"],
    ["library-work-summary"],
    ["coordinator-status"],
    ["dashboard"],
    ["processing-batches"],
    ["deep-review-policy"],
    ["deep-review-backfills"],
  ],
  "batch.changed": [
    ["processing-batches"],
    ["dashboard"],
    ["coordinator-status"],
    ["library-program"],
    ["library-work-summary"],
  ],
  "coordinator.changed": [
    ["coordinator-status"],
    ["system-overview"],
    ["system-diagnostics"],
    ["dashboard"],
    ["operations-summary"],
  ],
  "writeback.changed": [
    ["writeback-batches"],
    ["writeback-batch"],
    ["writeback-operations"],
    ["immich-library-overview"],
    ["operations-summary"],
  ],
  "asset.changed": [
    ["smart-collections"],
    ["smart-collection-preview"],
    ["dashboard"],
    ["library-work-summary"],
    ["model-performance"],
  ],
  "model.changed": [
    ["models"],
    ["model-packages"],
    ["model-performance"],
    ["workers"],
  ],
  "immich.changed": [
    ["instances"],
    ["connections"],
    ["immich-connections"],
    ["immich-library-overview"],
    ["dashboard-library-overviews"],
    ["dashboard"],
  ],
  "audit.changed": [
    ["audit"],
    ["audit-verification"],
    ["audit-checkpoints"],
  ],
  "collection.changed": [
    ["smart-collections"],
    ["smart-collection-preview"],
    ["asset-gallery"],
    ["asset-gallery-facets"],
    ["groups"],
    ["dashboard"],
  ],
  "storage.changed": [
    ["browser-image-cache"],
    ["system-overview"],
    ["system-diagnostics"],
  ],
};

export function queryKeysForSystemEvent(
  eventType: SystemEventType,
  envelope?: SystemEventEnvelope,
): ReadonlyArray<readonly string[]> {
  const keys = [...QUERY_KEYS_BY_EVENT[eventType]];
  if (["program.changed", "task.changed", "batch.changed"].includes(eventType)) keys.push(["run-summary"]);
  const assetId = envelope?.asset_id;
  const connectionId = envelope?.connection_id;
  const batchId = envelope?.batch_id;
  const groupId = envelope?.group_id;
  if (assetId) {
    keys.push(["asset", assetId], ["asset-analysis", assetId], ["asset-inspector", assetId]);
  } else if (eventType === "asset.changed" || eventType === "task.changed") {
    keys.push(["asset-analysis"], ["asset-inspector"]);
  }
  if (connectionId) keys.push(["immich-library-overview", connectionId]);
  if (batchId) keys.push(["writeback-batch", batchId], ["writeback-operations", batchId]);
  if (groupId) keys.push(["group", groupId], ["collection-burst", groupId]);
  const unique = new Map(keys.map((key) => [JSON.stringify(key), key]));
  return [...unique.values()];
}

export function isImmediateSystemEvent(eventType: SystemEventType): boolean {
  return IMMEDIATE_SYSTEM_EVENTS.has(eventType);
}
