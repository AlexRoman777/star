import { useQueryClient } from "@tanstack/react-query";
import { ReactNode, useEffect, useRef, useState } from "react";

import { LiveUpdatesContext, type LiveUpdateState, useLiveUpdateState } from "./liveUpdateContext";
import { recordLiveUpdateState } from "./pollingPolicy";
import {
  isImmediateSystemEvent,
  queryKeysForSystemEvent,
  SYSTEM_EVENT_TYPES,
  type SystemEventEnvelope,
  type SystemEventType,
} from "./systemEvents";

const COALESCE_WINDOW_MS = 10_000;
const IMMEDIATE_WINDOW_MS = 100;
const FALLBACK_AFTER_MS = 10_000;

export function SystemEventsProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [state, setLocalState] = useState<LiveUpdateState>(() => typeof window.EventSource === "undefined" ? "fallback" : "connecting");
  const openedOnce = useRef(false);
  const setState = (next: LiveUpdateState) => {
    recordLiveUpdateState(next);
    setLocalState(next);
  };
  recordLiveUpdateState(state);

  useEffect(() => {
    if (typeof window.EventSource === "undefined") {
      return;
    }

    const pendingKeys = new Map<string, readonly string[]>();
    const immediateKeys = new Map<string, readonly string[]>();
    let coalesceTimer: number | undefined;
    let immediateTimer: number | undefined;
    let fallbackTimer: number | undefined;

    const flush = () => {
      coalesceTimer = undefined;
      const keys = [...pendingKeys.values()];
      pendingKeys.clear();
      void Promise.all(
        keys.map((queryKey) =>
          queryClient.invalidateQueries({
            queryKey,
            type: "active",
            refetchType: "active",
          }),
        ),
      );
    };

    const flushImmediate = () => {
      immediateTimer = undefined;
      const keys = [...immediateKeys.values()];
      immediateKeys.clear();
      void Promise.all(keys.map((queryKey) => queryClient.invalidateQueries({
        queryKey,
        type: "active",
        refetchType: "active",
      })));
    };

    const schedule = (eventType: SystemEventType, envelope?: SystemEventEnvelope) => {
      const immediate = isImmediateSystemEvent(eventType);
      const target = immediate ? immediateKeys : pendingKeys;
      for (const queryKey of queryKeysForSystemEvent(eventType, envelope)) {
        target.set(JSON.stringify(queryKey), queryKey);
      }
      if (immediate) immediateTimer ??= window.setTimeout(flushImmediate, IMMEDIATE_WINDOW_MS);
      else coalesceTimer ??= window.setTimeout(flush, COALESCE_WINDOW_MS);
    };

    const stream = new EventSource("/api/v1/system/events/stream");
    stream.onopen = () => {
      if (fallbackTimer !== undefined) window.clearTimeout(fallbackTimer);
      fallbackTimer = undefined;
      setState("live");
      if (openedOnce.current) {
        void queryClient.invalidateQueries({ type: "active", refetchType: "active" });
      }
      openedOnce.current = true;
    };
    stream.onerror = () => {
      setState("reconnecting");
      if (fallbackTimer !== undefined) window.clearTimeout(fallbackTimer);
      fallbackTimer = window.setTimeout(() => setState("fallback"), FALLBACK_AFTER_MS);
    };
    for (const eventType of SYSTEM_EVENT_TYPES) {
      stream.addEventListener(eventType, (event) => {
        const data = (event as MessageEvent<string>).data;
        if (!data) {
          schedule(eventType);
          return;
        }
        try {
          const envelope = JSON.parse(data) as SystemEventEnvelope;
          schedule(eventType, envelope.event === eventType ? envelope : undefined);
        } catch {
          schedule(eventType);
        }
      });
    }

    return () => {
      stream.close();
      if (coalesceTimer !== undefined) window.clearTimeout(coalesceTimer);
      if (immediateTimer !== undefined) window.clearTimeout(immediateTimer);
      if (fallbackTimer !== undefined) window.clearTimeout(fallbackTimer);
    };
  }, [queryClient]);

  return (
    <LiveUpdatesContext.Provider value={state}>
      {children}
    </LiveUpdatesContext.Provider>
  );
}

export function LiveUpdateStatus() {
  const state = useLiveUpdateState();
  const label = state === "live"
    ? "Live"
    : state === "fallback"
      ? "Fallback polling"
      : "Reconnecting";
  return (
    <span
      className={`live-signal live-update-status ${state === "live" ? "active" : state}`}
      aria-live="polite"
      title="Page data updates automatically from Star."
    >
      <i />
      {label}
    </span>
  );
}
