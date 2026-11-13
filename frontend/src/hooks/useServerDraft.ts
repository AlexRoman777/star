import type { Dispatch, SetStateAction } from "react";
import { useState } from "react";

const UNSET = Symbol("unset");

function signatureKey(value: unknown): unknown {
  // Object signatures compare by value: server payloads are routinely
  // re-materialized with fresh identity while remaining unchanged.
  return typeof value === "object" && value !== null ? JSON.stringify(value) : value;
}

/**
 * Local editable state that resets whenever the server value changes.
 *
 * This is React's documented render-phase "adjust state when props change"
 * pattern, replacing the setState-in-effect version that needed lint
 * suppressions. Pass `signature` when several drafts should reset together on
 * one server identity (for example a decision id) instead of on their own
 * field values.
 */
export function useServerDraft<T>(
  serverValue: T,
  signature: unknown = UNSET,
): [T, Dispatch<SetStateAction<T>>] {
  const current = signatureKey(signature === UNSET ? serverValue : signature);
  const [draft, setDraft] = useState(serverValue);
  const [lastSignature, setLastSignature] = useState(current);
  if (!Object.is(lastSignature, current)) {
    setLastSignature(current);
    setDraft(serverValue);
    return [serverValue, setDraft];
  }
  return [draft, setDraft];
}
