import type { InferenceSampling, InferenceSettings, InferenceStageSettings } from "../../api/types";

export const INFERENCE_SETTINGS_KEY = ["inference-settings"] as const;

type Stage = keyof InferenceSettings;
type Effort = InferenceStageSettings["reasoning_effort"];

export const STAGES: ReadonlyArray<{ key: Stage; label: string; hint: string }> = [
  { key: "primary", label: "Primary review", hint: "Every photo, NVIDIA Ministral" },
  { key: "advanced", label: "Advanced review", hint: "Selected photos, Qwen 27B on either machine" },
  { key: "group", label: "Group tasks", hint: "Highlight comparison sheets" },
];

export const KNOBS: ReadonlyArray<{ key: keyof InferenceSampling; label: string; hint: string; min: number; max: number; step: number }> = [
  { key: "temperature", label: "Temperature", hint: "Randomness: 0 is deterministic, higher explores more", min: 0, max: 2, step: 0.05 },
  { key: "top_p", label: "Top p", hint: "Nucleus: only tokens inside this probability mass count", min: 0, max: 1, step: 0.05 },
  { key: "top_k", label: "Top k", hint: "Only the k most likely tokens count (0 = off)", min: 0, max: 200, step: 1 },
  { key: "min_p", label: "Min p", hint: "Drop tokens below this share of the top token's probability", min: 0, max: 1, step: 0.01 },
  { key: "repeat_penalty", label: "Repeat penalty", hint: "Above 1 discourages repeating earlier text", min: 0.5, max: 2, step: 0.01 },
  { key: "presence_penalty", label: "Presence penalty", hint: "Above 0 pushes toward words not yet used; below 0 pulls back", min: -2, max: 2, step: 0.1 },
];

export const EFFORTS: ReadonlyArray<{ key: Effort; label: string }> = [{ key: "off", label: "Off" }, { key: "low", label: "Low" }, { key: "medium", label: "Medium" }];
export const TOKEN_BOUNDS = { min: 64, max: 8_192 };
export const EDGE_BOUNDS = { min: 512, max: 2_048 };

function inRange(value: number, bounds: { min: number; max: number }): boolean {
  return Number.isFinite(value) && value >= bounds.min && value <= bounds.max;
}

/** Field paths that are out of bounds; empty when the document can be saved. */
export function inferenceSettingsErrors(settings: InferenceSettings): string[] {
  const errors: string[] = [];
  for (const stage of STAGES) {
    const item = settings[stage.key];
    for (const knob of KNOBS) if (!inRange(item.sampling[knob.key], knob)) errors.push(`${stage.key}.${knob.key}`);
    if (!inRange(item.max_completion_tokens, TOKEN_BOUNDS)) errors.push(`${stage.key}.max_completion_tokens`);
    if (!inRange(item.max_media_edge, EDGE_BOUNDS)) errors.push(`${stage.key}.max_media_edge`);
    if (!EFFORTS.some((effort) => effort.key === item.reasoning_effort)) errors.push(`${stage.key}.reasoning_effort`);
  }
  return errors;
}
