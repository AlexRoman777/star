import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "../../api/client";
import type { OperationalEvent } from "../../api/types";

const MAX_VISIBLE_EVENTS = 1_000;
const EVENT_GROUP_WINDOW_MS = 10_000;
type ConnectionState = "connecting" | "live" | "paused" | "retrying";
export type DisplayMode = "summary" | "raw";

export interface EventGroup {
  key: string;
  event: OperationalEvent;
  count: number;
  firstAt: string;
  lastAt: string;
}

export function detailString(event: OperationalEvent, key: string): string | null {
  const value = event.details[key];
  return typeof value === "string" ? value : null;
}

export function detailNumber(event: OperationalEvent, key: string): number | null {
  const value = event.details[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function taskLabel(taskType: string | null): string {
  const labels: Record<string, string> = { "asset.technical_preview": "Retired technical check", "asset.apple_vision": "Retired Apple Vision", "asset.fast_rating": "Retired photo rating", "group.cohort_triage": "Retired group ordering", "asset.primary_review": "Primary review", "asset.apple_order_check": "Manual Mac order check", "asset.deep_review": "Advanced review", "group.highlight_compare": "Highlight comparison", "asset.description_embedding": "Search embedding" };
  return taskType ? labels[taskType] ?? taskType : "Task";
}

export function formatEventTime(value: string) {
  return new Date(value).toLocaleTimeString([], { hour12: false });
}

function boundedReason(reason: string): string | null {
  return reason ? (reason.length > 96 ? `${reason.slice(0, 93)}…` : reason) : null;
}

function failureMetrics(event: OperationalEvent): string | null {
  const attempt = detailNumber(event, "attempt");
  const delay = detailNumber(event, "retry_delay_seconds");
  const reason = boundedReason(detailString(event, "reason") ?? "");
  const bits: string[] = [];
  if (attempt !== null) bits.push(`attempt ${attempt}`);
  if (delay !== null && delay > 0) bits.push(`retry ${Math.round(delay)}s`);
  if (reason) bits.push(reason);
  return bits.length > 0 ? bits.join(" · ") : null;
}

function failureSummary(event: OperationalEvent): string {
  const label = taskLabel(detailString(event, "task_type"));
  const errorClass = detailString(event, "error_class");
  const parts = [label];
  if (errorClass) parts.push(`error ${errorClass}`);
  const metrics = failureMetrics(event);
  if (metrics) parts.push(metrics);
  return parts.join(" · ");
}

export function eventDetailSummary(event: OperationalEvent): string {
  const taskType = detailString(event, "task_type");
  const route = detailString(event, "routing_key");
  const model = detailString(event, "model_key");

  const simple: Record<string, string> = {
    "task.completed": `${taskLabel(taskType)} stored successfully`,
    "apple_vision.completed": "Retired Apple Vision evidence stored",
  };
  if (event.event_type === "task.failed" || event.event_type === "task.returned") return failureSummary(event);
  if (event.event_type === "task.leased") return [taskLabel(taskType), route, model].filter(Boolean).join(" · ");
  if (event.event_type.startsWith("model.") && event.event_type.endsWith(".completed")) return completedModelSummary(event);
  if (event.event_type === "processing.batch.planned") return plannedBatchSummary(event);
  return simple[event.event_type] ?? event.event_type;
}

function completedModelSummary(event: OperationalEvent): string {
  const duration = detailNumber(event, "duration_ms");
  const labels: Record<string, string> = {
    "model.primary_review.completed": "Primary rating stored",
    "model.deep_review.completed": "Advanced rating stored",
    "model.highlight_comparison.completed": "Highlight comparison stored",
    "model.quick_rating.completed": "Retired group ordering stored",
  };
  const label = labels[event.event_type] ?? "Model result stored";
  return [label, detailString(event, "runtime"), duration === null ? null : `${Math.round(duration / 100) / 10}s`].filter(Boolean).join(" · ");
}

function plannedBatchSummary(event: OperationalEvent): string {
  const assets = detailNumber(event, "assets");
  if (assets === null) return "Coordinator replenished the active work window";
  return `${assets} ${assets === 1 ? "photo" : "photos"} added to the active work window`;
}

export function coalesceEvents(events: OperationalEvent[]): EventGroup[] {
  const groups = new Map<string, EventGroup>();
  for (const event of events) {
    const timestamp = new Date(event.created_at).getTime();
    const bucket = Number.isFinite(timestamp) ? Math.floor(timestamp / EVENT_GROUP_WINDOW_MS) : 0;
    const key = `${bucket}:${event.source}:${event.severity}:${event.event_type}`;
    const current = groups.get(key);
    if (current) {
      current.count += 1;
      current.event = event;
      current.lastAt = event.created_at;
    } else {
      groups.set(key, { key, event, count: 1, firstAt: event.created_at, lastAt: event.created_at });
    }
  }
  return Array.from(groups.values());
}

function eventQuery(source: string, severity: string) {
  const params = new URLSearchParams({ limit: "200" });
  if (source) params.set("source", source);
  if (severity) params.set("severity", severity);
  return params;
}

export function useOperationsEvents(source: string, severity: string, paused: boolean) {
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [events, setEvents] = useState<OperationalEvent[]>([]);
  const cursor = useRef(0);
  const history = useQuery({
    queryKey: ["operation-events", source, severity],
    queryFn: () => apiFetch<OperationalEvent[]>("/operations/events?" + eventQuery(source, severity)),
  });

  useEffect(() => {
    if (!history.data) return;
    const chronological = [...history.data].reverse();
    // Seed the append-only live stream from its authoritative history snapshot.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setEvents(chronological);
    cursor.current = chronological.at(-1)?.id ?? 0;
  }, [history.data]);

  useEffect(() => {
    if (paused) {
      // Paused is an owner-controlled external stream state.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setConnectionState("paused");
      return;
    }
    setConnectionState("connecting");
    const params = eventQuery(source, severity);
    params.set("after_id", String(cursor.current));
    const stream = new EventSource("/api/v1/operations/events/stream?" + params.toString());
    stream.onopen = () => setConnectionState("live");
    stream.onerror = () => setConnectionState("retrying");
    stream.addEventListener("operation", (event) => {
      const incoming = JSON.parse((event as MessageEvent<string>).data) as OperationalEvent;
      cursor.current = Math.max(cursor.current, incoming.id);
      setEvents((current) => current.some((item) => item.id === incoming.id)
        ? current
        : [...current, incoming].slice(-MAX_VISIBLE_EVENTS));
    });
    return () => stream.close();
  }, [paused, severity, source, history.dataUpdatedAt]);

  return { connectionState, events, setEvents, history };
}


export function filteredEvents(events: OperationalEvent[], textFilter: string): OperationalEvent[] {
  const needle = textFilter.trim().toLocaleLowerCase();
  if (!needle) return events;
  return events.filter((event) => (
    [event.message, event.event_type, event.source, JSON.stringify(event.details)]
      .join(" ")
      .toLocaleLowerCase()
      .includes(needle)
  ));
}

