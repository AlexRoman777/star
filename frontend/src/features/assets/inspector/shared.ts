import type { AssetAnalysis } from "../../../api/types";

export function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export function display(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function voteSourceLabel(source: string): string {
  if (source === "primary_photo_review") return "Photo review";
  if (source === "deep_photo_adjudication") return "Advanced review";
  if (source === "local_roaster") return "Roast";
  return "Technical screen";
}

export function votePurpose(source: string): string {
  if (source === "primary_photo_review") return "Complete photographic judgment, description, family value, privacy, editing, and rating in one pass";
  if (source === "deep_photo_adjudication") return "Conditional large-model adjudication for highlights, uncertainty, or conflicting evidence";
  if (source === "local_roaster") return "Opt-in, honest-but-funny critique — roasts the photo, never the person; never authorizes deletion";
  return "Deterministic focus, blur, exposure, clipping, noise, and artifact checks";
}

export function percentage(value: unknown): string | null {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : null;
}


export function duration(value: unknown): string | null {
  if (typeof value !== "number") return null;
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} s`;
}

export function voteModelLabel(source: string, details: Record<string, unknown>): string {
  const model = details.model_id;
  if (typeof model === "string" && model) return model;
  return voteSourceLabel(source);
}

export function textValue(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export type InspectorRatingVote = NonNullable<AssetAnalysis["rating_votes"]>[number];
