import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { apiFetch } from "../../api/client";
import type { WritebackPolicy, WritebackPolicyResponse } from "../../api/types";
import { useServerDraft } from "../../hooks/useServerDraft";

const WRITEBACK_POLICY_KEY = ["writeback-policy"] as const;

const FIELDS: ReadonlyArray<{ key: "rating" | "tags" | "description"; label: string; hint: string }> = [
  { key: "rating", label: "Star rating", hint: "Write the proposed 1–5★ to Immich" },
  { key: "tags", label: "Tags", hint: "Add Star's content tags" },
  { key: "description", label: "Description", hint: "Write the model's summary" },
];
const CONFIDENCE = { min: 0.1, max: 0.95, step: 0.05 };

function normalise(policy: WritebackPolicy | undefined): Required<WritebackPolicy> {
  return { minimum_confidence: policy?.minimum_confidence ?? 0.7, write_fields: [...(policy?.write_fields ?? ["rating"])], include_private_tags: policy?.include_private_tags ?? true };
}

/** Owner write-back policy: how sure a model must be and which fields Star may write. Saves under the program revision; a 409 reloads. */
export function WritebackPolicyCard() {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: WRITEBACK_POLICY_KEY, queryFn: () => apiFetch<WritebackPolicyResponse>("/library-program/writeback-policy") });
  const server = query.data;
  const [draft, setDraft] = useServerDraft(normalise(server?.policy), server ? `${server.revision} ${JSON.stringify(server.policy)}` : null);
  const [notice, setNotice] = useState<string | null>(null);
  const save = useMutation({
    meta: { invalidates: [WRITEBACK_POLICY_KEY, ["writeback-batches"]] },
    mutationFn: (policy: Required<WritebackPolicy>) => apiFetch<WritebackPolicyResponse>("/library-program/writeback-policy", { method: "PUT", body: JSON.stringify({ expected_revision: server?.revision, policy }) }),
    onSuccess: (updated) => { queryClient.setQueryData(WRITEBACK_POLICY_KEY, updated); setNotice("Saved. The next prepared batch follows this policy."); },
    onError: (error: Error & { status?: number }) => {
      if (error.status === 409) {
        setNotice("The program changed first. Reloaded the current policy; apply your change again.");
        void queryClient.invalidateQueries({ queryKey: WRITEBACK_POLICY_KEY });
      } else setNotice(null);
    },
  });
  if (query.isError) return <section className="panel wb-policy"><p className="form-error">{query.error.message}</p></section>;
  if (!server) return <section className="panel wb-policy"><p className="input-help">Loading write-back policy…</p></section>;
  const dirty = JSON.stringify(draft) !== JSON.stringify(normalise(server.policy));
  const atDefaults = JSON.stringify(draft) === JSON.stringify(normalise(server.defaults));
  const confidenceInvalid = draft.minimum_confidence < CONFIDENCE.min || draft.minimum_confidence > CONFIDENCE.max;
  const toggleField = (key: "rating" | "tags" | "description") => {
    setNotice(null);
    setDraft({ ...draft, write_fields: draft.write_fields.includes(key) ? draft.write_fields.filter((item) => item !== key) : [...draft.write_fields, key] });
  };
  return <section className="panel wb-policy" aria-labelledby="wb-policy-title">
    <div className="bm-section-head">
      <div><p className="eyebrow">Write-back policy</p><h2 id="wb-policy-title">Write back to Immich</h2><p>A proposal is written only when the model's confidence clears this floor, and only for the fields you enable. Existing non-zero Immich ratings stay protected; every batch still needs your approval.</p></div>
      <div className="bm-section-actions"><span className="status-pill">{server.stored ? `revision ${server.revision}` : "built-in defaults"}</span></div>
    </div>
    <div className="wb-policy-body">
      <label className="wb-confidence">
        <span>Minimum confidence<small>{Math.round(draft.minimum_confidence * 100)}% · lower writes more, higher writes only sure proposals</small></span>
        <input type="range" min={CONFIDENCE.min} max={CONFIDENCE.max} step={CONFIDENCE.step} value={draft.minimum_confidence} onChange={(event) => { setNotice(null); setDraft({ ...draft, minimum_confidence: Number(event.currentTarget.value) }); }} aria-valuetext={`${Math.round(draft.minimum_confidence * 100)} percent`} />
        <div className="wb-confidence-scale" aria-hidden="true"><span>10%</span><span>50%</span><span>95%</span></div>
      </label>
      <fieldset className="wb-fields"><legend>Fields Star may write</legend>
        {FIELDS.map((field) => <label key={field.key} className={`bm-toggle${draft.write_fields.includes(field.key) ? " on" : ""}`}>
          <input type="checkbox" checked={draft.write_fields.includes(field.key)} onChange={() => toggleField(field.key)} /><i aria-hidden="true" /><span><strong>{field.label}</strong><small>{field.hint}</small></span>
        </label>)}
        <label className={`bm-toggle${draft.include_private_tags ? " on" : ""}`}>
          <input type="checkbox" checked={draft.include_private_tags} disabled={!draft.write_fields.includes("tags")} onChange={() => { setNotice(null); setDraft({ ...draft, include_private_tags: !draft.include_private_tags }); }} /><i aria-hidden="true" /><span><strong>Private tags</strong><small>Also write star/private/… tags for NSFW-classified photos</small></span>
        </label>
      </fieldset>
    </div>
    {draft.write_fields.length === 0 && <p className="bm-warning">With no fields enabled, batches will prepare nothing to write.</p>}
    {confidenceInvalid && <p className="form-error">Confidence must stay between 10% and 95%.</p>}
    {save.error && save.error.status !== 409 && <p className="form-error">{save.error.message}</p>}
    {notice && <p className="bm-notice" role="status">{notice}</p>}
    <div className="bm-form-actions">
      <button type="button" className="primary" disabled={!dirty || confidenceInvalid || save.isPending} onClick={() => save.mutate(draft)}>{save.isPending ? "Saving…" : "Save policy"}</button>
      <button type="button" className="secondary" disabled={!dirty || save.isPending} onClick={() => { setDraft(normalise(server.policy)); setNotice(null); }}>Undo edits</button>
      <button type="button" className="secondary" disabled={atDefaults || save.isPending} onClick={() => { setDraft(normalise(server.defaults)); setNotice("Defaults loaded; save to apply them."); }}>Reset to defaults</button>
    </div>
  </section>;
}
