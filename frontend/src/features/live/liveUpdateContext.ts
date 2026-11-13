import { createContext, useContext } from "react";

export type LiveUpdateState = "connecting" | "live" | "reconnecting" | "fallback";

export const LiveUpdatesContext = createContext<LiveUpdateState>("fallback");

export function useLiveUpdateState(): LiveUpdateState {
  return useContext(LiveUpdatesContext);
}
