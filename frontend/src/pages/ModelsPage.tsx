import { seniorRuntimeLabel } from "../features/machines/machineSummary";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent } from "react";

import { apiFetch, formatBytes } from "../api/client";
import { usePipelineContract } from "../api/pipelineContract";
import type { ModelPackage, ModelVersion, PipelineContract } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { StatusPill } from "../components/StatusPill";

interface ModelCardProps {
  model: ModelVersion;
  packages: ModelPackage[];
  benchmarkPending: boolean;
  packagePending: boolean;
  onBenchmark: (event: FormEvent<HTMLFormElement>, model: ModelVersion) => void;
  onPackage: (event: FormEvent<HTMLFormElement>, model: ModelVersion) => void;
}

function PackageControls({
  model,
  packages,
  pending,
  onPackage,
}: {
  model: ModelVersion;
  packages: ModelPackage[];
  pending: boolean;
  onPackage: (event: FormEvent<HTMLFormElement>, model: ModelVersion) => void;
}) {
  const modelPackages = packages.filter((item) => item.model_version_id === model.id);
  if (modelPackages.length > 0) {
    return modelPackages.map((item) => (
      <p className="input-help" key={item.id}>
        Verified {item.runtime_format} package · {formatBytes(item.size_bytes)} · {item.source_revision}
      </p>
    ));
  }
  if (!model.artifact_checksum || !model.license_id) return null;
  return (
    <details className="model-administration">
      <summary>Verify a seeded model package</summary>
      <form className="benchmark-form" onSubmit={(event) => onPackage(event, model)}>
        <p className="input-help">Seed this exact file under the control-plane model volume, then verify its immutable source, path, size, license, and checksum.</p>
        <label>Official source URL<input name="source_url" type="url" placeholder="https://huggingface.co/org/model/resolve/commit/model.gguf" required /></label>
        <label>Immutable revision<input name="source_revision" minLength={7} required /></label>
        <label>Runtime format<input name="runtime_format" placeholder="gguf" required /></label>
        <label>Size bytes<input name="size_bytes" type="number" min="1" required /></label>
        <label>Model-store path<input name="relative_path" placeholder="org/model/model.gguf" required /></label>
        <button className="secondary" disabled={pending}>Verify seeded package</button>
      </form>
    </details>
  );
}

function ApprovalControls({
  model,
  discovered,
  pending,
  onBenchmark,
}: {
  model: ModelVersion;
  discovered: boolean;
  pending: boolean;
  onBenchmark: (event: FormEvent<HTMLFormElement>, model: ModelVersion) => void;
}) {
  if (model.enabled) {
    return <p className="input-help">Ready for local photo review. It creates proposals only and is never allowed to write or delete media.</p>;
  }
  if (discovered) return null;
  return (
    <details className="model-administration">
      <summary>Enter benchmark results</summary>
      <form className="benchmark-form" onSubmit={(event) => onBenchmark(event, model)}>
        <p className="input-help">Enter measured local results from at least ten representative groups. Approval does not schedule work automatically.</p>
        <label>Dataset<input name="dataset" placeholder="holiday-calibration-v1" required /></label>
        <label>Groups<input name="sample_groups" type="number" min="10" defaultValue="20" required /></label>
        <label>JSON success<input name="structured_success_rate" type="number" min="0" max="1" step="0.01" defaultValue="0.95" required /></label>
        <label>Median ms<input name="median_duration_ms" type="number" min="1" required /></label>
        <label>Peak GiB<input name="peak_memory_gib" type="number" min="0.1" step="0.1" required /></label>
        <button className="secondary" disabled={!model.artifact_checksum || pending}>Approve measured model</button>
      </form>
    </details>
  );
}

function modelCardStatus(model: ModelVersion, discovered: boolean): string {
  if (model.benchmark.mode === "trusted_local_runtime" && model.enabled) return "ready";
  if (model.enabled) return "enabled";
  return discovered ? "discovered" : "disabled";
}

function DiscoveryControls({ model, discovered }: { model: ModelVersion; discovered: boolean }) {
  if (!discovered) return null;
  const workers = (model.availability ?? []).filter((item) => item.available);
  return (
    <>
      <p className="input-help">Discovered on {workers.length || "no current"} worker{workers.length === 1 ? "" : "s"} · {String(model.resource_profile.runtime_format || "local runtime")} · {model.enabled ? "ready for proposal-only review" : "not enabled"}.</p>
    </>
  );
}

const STAGE_LABELS: Record<string, string> = { primary: "Primary review", advanced: "Advanced review", group: "Group tasks" };

function settingRows(model: ModelVersion): Array<[string, string, string]> {
  const vendor = model.build?.vendor_recommended;
  const active = model.active_settings;
  const show = (value: number | null | undefined) => value == null ? "—" : String(value);
  return [
    ["Temperature", show(vendor?.temperature), show(active?.sampling.temperature)],
    ["Top p", show(vendor?.top_p), show(active?.sampling.top_p)],
    ["Top k", show(vendor?.top_k), show(active?.sampling.top_k)],
    ["Presence", show(vendor?.presence_penalty), show(active?.sampling.presence_penalty)],
    ["Thinking", vendor?.reasoning_note || "—", active?.reasoning_effort ?? "—"],
    ["Image edge", vendor?.image_note || "—", active ? `${active.max_media_edge} px` : "—"],
    ["Max tokens", "—", active ? active.max_completion_tokens.toLocaleString() : "—"],
  ];
}

/** Build identity, runtime, and the vendor-recommended vs active settings for one model. */
function ModelBuildPanel({ model }: { model: ModelVersion }) {
  const build = model.build;
  if (!build && !model.stage && !model.active_settings) return null;
  const rows = settingRows(model);
  return <div className="model-build">
    <div className="model-build-facts">
      {build && <span><small>Build</small><strong>{build.ollama_tag ?? build.display_name}</strong></span>}
      <span><small>Quant</small><strong>{build?.quantization ?? model.quantization}</strong></span>
      <span><small>Size</small><strong>{build?.size_gb != null ? `${build.size_gb.toFixed(1)} GB` : "—"}</strong></span>
      <span><small>Runtime</small><strong>{build?.runtime ?? model.runtime}</strong></span>
      <span><small>Stage</small><strong>{model.stage ? STAGE_LABELS[model.stage] ?? model.stage : "Not in production"}</strong></span>
      {build?.huggingface_url && <span><small>Source</small><strong><a href={build.huggingface_url} target="_blank" rel="noreferrer noopener">Hugging Face ↗</a></strong></span>}
    </div>
    {(build?.vendor_recommended || model.active_settings) && <table className="model-settings-table">
      <thead><tr><th scope="col">Setting</th><th scope="col">Vendor recommends</th><th scope="col">Active</th></tr></thead>
      <tbody>{rows.map(([label, vendor, active]) => <tr key={label} className={vendor !== "—" && active !== "—" && vendor !== active ? "differs" : ""}><th scope="row">{label}</th><td>{vendor}</td><td>{active}</td></tr>)}</tbody>
    </table>}
    {build?.notes?.length ? <ul className="model-build-notes">{build.notes.map((note) => <li key={note}>{note}</li>)}</ul> : null}
    {build?.alternatives?.length ? <details className="model-build-alternatives"><summary>{build.alternatives.length} alternative build{build.alternatives.length === 1 ? "" : "s"}</summary><ul>{build.alternatives.map((item) => <li key={item.tag}><strong>{item.label}</strong> <code>{item.tag}</code> · {item.quantization}{item.size_gb != null ? ` · ${item.size_gb.toFixed(1)} GB` : ""} · {item.runtime}{item.note ? ` — ${item.note}` : ""}{item.source_url && <> · <a href={item.source_url} target="_blank" rel="noreferrer noopener">source ↗</a></>}</li>)}</ul></details> : null}
  </div>;
}

function ModelCard(props: ModelCardProps) {
  const { model } = props;
  const discovered = model.resource_profile.discovery === "worker-runtime";
  const status = modelCardStatus(model, discovered);
  return (
    <article className="panel">
      <div className="panel-heading"><div><h2>{model.model_id}</h2><small>{model.version} · {model.runtime}</small></div><StatusPill value={status} /></div>
      <div className="model-facts">
        <span><small>Quantization</small><strong>{model.quantization}</strong></span>
        <span><small>License</small><strong>{model.license_id || "Unrecorded"}</strong></span>
        <span><small>Checksum</small><strong>{model.artifact_checksum ? "Verified record" : "Missing"}</strong></span>
      </div>
      <DiscoveryControls model={model} discovered={discovered} />
      <ModelBuildPanel model={model} />
      <PackageControls model={model} packages={props.packages} pending={props.packagePending} onPackage={props.onPackage} />
      <ApprovalControls model={model} discovered={discovered} pending={props.benchmarkPending} onBenchmark={props.onBenchmark} />
    </article>
  );
}

function productionRole(modelId: string, contract?: PipelineContract): { label: string; description: string } {
  const role = contract?.models.find((model) => model.model_key === modelId)?.role;
  if (role === "primary_rating") {
    return {
      label: "Primary photo review",
      description: "Reviews every photo independently; both machines take from the same primary queue whenever they are free.",
    };
  }
  if (role === "relative_comparison") return {
    label: "Highlight comparison",
    description: "Compares completed strong photos without issuing an individual star rating.",
  };
  return {
    label: "Advanced review",
    description: "Decides the final star for 4★ and 5★ candidates, sensitive photos and manual requests on whichever review machine holds it: the Mac on MLX, the NVIDIA worker on Bonsai.",
  };
}

function ProductionModelCard({ models, contract }: { models: ModelVersion[]; contract?: PipelineContract }) {
  const primary = models[0]!;
  const role = productionRole(primary.model_id, contract);
  const availability = models.flatMap((model) => (model.availability ?? []).map((item) => ({ item, model })));
  const multiRuntime = models.length > 1;
  return (
    <article className="panel production-model-card">
      <header>
        <div><small>{role.label}</small><h2>{primary.model_id}</h2>{multiRuntime && <small>{models.length} builds · {models.map((model) => seniorRuntimeLabel(model.runtime)).join(" · ")}</small>}</div>
        <StatusPill value="ready" />
      </header>
      <div className="production-runtime-list">
        {availability.map(({ item, model }) => (
          <span key={`${model.id}-${item.worker_id}`}>
            <strong>{item.worker_label}</strong>
            <small>{item.loaded ? "loaded" : item.available ? "available" : "missing"} · {item.worker_online ? "online" : "offline"}{multiRuntime ? ` · ${seniorRuntimeLabel(model.runtime)}` : ""}</small>
          </span>
        ))}
        {!availability.length && <span><strong>No current worker</strong><small>Waiting for model discovery</small></span>}
      </div>
      {multiRuntime
        ? models.map((model) => <div className="production-build" key={model.id}><p className="model-slots-eyebrow">{seniorRuntimeLabel(model.runtime)} · {(model.availability ?? []).filter((item) => item.available).map((item) => item.worker_label).join(", ") || "no current worker"}</p><ModelBuildPanel model={model} /></div>)
        : <ModelBuildPanel model={primary} />}
      <details className="model-provenance">
        <summary>Runtime and provenance</summary>
        <div className="model-facts">
          <span><small>Workers</small><strong>{availability.filter(({ item }) => item.available).length}</strong></span>
          <span><small>License</small><strong>{primary.license_id || "Not reported"}</strong></span>
          <span><small>Artifacts</small><strong>{models.every((model) => model.artifact_checksum) ? "Verified" : "Worker managed"}</strong></span>
        </div>
        {availability.map(({ item, model }) => <code key={`${model.id}-${item.worker_id}`}>{item.worker_label} · {model.runtime} · {item.runtime_fingerprint}</code>)}
      </details>
    </article>
  );
}

function groupProductionModels(models: ModelVersion[], productionKeys: ReadonlySet<string>): ModelVersion[][] {
  const groups = new Map<string, ModelVersion[]>();
  for (const model of models.filter(
    (item) => item.enabled && productionKeys.has(item.model_id),
  )) {
    const group = groups.get(model.model_id) ?? [];
    group.push(model);
    groups.set(model.model_id, group);
  }
  return [...groups.values()];
}

function ProductionRoute({ contract }: { contract?: PipelineContract }) {
  return <section className="panel model-candidates production-route">
    <div className="panel-heading"><div><h2>Production route</h2></div><StatusPill value="private" /></div>
    <div className="model-candidate-grid">
      {(contract?.stages ?? []).map((stage, index) => (
        <article key={stage.key}>
          <small>{String(index + 1).padStart(2, "0")} · {stage.required ? "required" : "conditional"}</small>
          <strong>{stage.label}</strong>
          <span>{stage.model_role.replaceAll("_", " ")}</span>
        </article>
      ))}
    </div>
  </section>;
}

function ProductionModels({ models, contract }: { models: ModelVersion[]; contract?: PipelineContract }) {
  const productionKeys = new Set(contract?.models.map((model) => model.model_key) ?? []);
  const groups = groupProductionModels(models, productionKeys);
  return <section className="model-grid production-model-grid" aria-label="Production models">
    {groups.map((group) => <ProductionModelCard models={group} contract={contract} key={group[0]!.model_id} />)}
    {!models.length && <div className="panel empty-state large"><strong>Waiting for a model worker</strong><span>Configured local models appear here automatically after the worker connects.</span></div>}
  </section>;
}

interface ExperimentModelsProps extends Omit<ModelCardProps, "model"> {
  models: ModelVersion[];
  productionKeys: ReadonlySet<string>;
}

function ExperimentModels(props: ExperimentModelsProps) {
  const experiments = props.models.filter((model) => !props.productionKeys.has(model.model_id));
  if (!experiments.length) return null;
  return <details className="panel model-experiments">
    <summary><span><small>Optional</small><strong>Experiments and inactive models</strong></span><span className="status-pill">{experiments.length}</span></summary>
    <div className="model-grid">
      {experiments.map((model) => (
        <ModelCard
          key={model.id}
          model={model}
          packages={props.packages}
          benchmarkPending={props.benchmarkPending}
          packagePending={props.packagePending}
          onBenchmark={props.onBenchmark}
          onPackage={props.onPackage}
        />
      ))}
    </div>
  </details>;
}

function ModelRegistration({ pending, error, onSubmit }: { pending: boolean; error: Error | null; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) {
  return <details className="panel administration-disclosure">
    <summary><span><small>Advanced fallback</small><strong>Register an undiscovered model</strong></span><span className="status-pill">normally automatic</span></summary>
    <form className="model-form" onSubmit={onSubmit}>
      <label>Model ID<input name="model_id" placeholder="qwen/qwen3.6-27b" required /></label>
      <label>Version<input name="version" placeholder="2026-07" required /></label>
      <label>Runtime<select name="runtime"><option>ollama</option><option>openai-compatible</option><option>mlx</option><option>llama.cpp-prism</option><option>llama.cpp</option><option>transformers</option><option>vllm</option></select></label>
      <label>Quantization<input name="quantization" defaultValue="Q4_K_M" required /></label>
      <label>Machine profile<select name="machine"><option>nvidia-8gb</option><option>mac-48gb</option></select></label>
      <label>License<input name="license_id" placeholder="Apache-2.0" /></label>
      <label className="checksum-field">Verified checksum<input name="artifact_checksum" placeholder="sha256:…" /></label>
      <button className="secondary" disabled={pending}>Register disabled model</button>
      {error && <p className="form-error">{error.message}</p>}
    </form>
  </details>;
}

function ModelErrors({ errors }: { errors: Array<Error | null> }) {
  return <>{errors.map((error, index) => error && <p className="form-error" key={index}>{error.message}</p>)}</>;
}

export function ModelsPage() {
  const queryClient = useQueryClient();
  const contract = usePipelineContract();
  const models = useQuery({
    queryKey: ["models"],
    queryFn: () => apiFetch<ModelVersion[]>("/models"),
  });
  const packages = useQuery({
    queryKey: ["model-packages"],
    queryFn: () => apiFetch<ModelPackage[]>("/models/packages"),
  });
  const register = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      apiFetch<ModelVersion>("/models", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ["models"] }),
  });
  const benchmark = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      apiFetch<ModelVersion>("/models/" + id + "/benchmark", {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ["models"] }),
  });
  const registerPackage = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      apiFetch<ModelPackage>(`/models/${id}/packages`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ["model-packages"] }),
  });

  function submitRegistration(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const machine = String(data.get("machine"));
    const capability = machine === "mac-48gb" ? "medium-vlm" : "small-vlm";
    register.mutate(
      {
        model_id: data.get("model_id"),
        version: data.get("version"),
        runtime: data.get("runtime"),
        quantization: data.get("quantization"),
        license_id: data.get("license_id") || null,
        artifact_checksum: data.get("artifact_checksum") || null,
        capabilities: {
          structured_json: true,
          images: true,
          single_image_review: true,
          multi_image: true,
        },
        resource_profile: { machine, capability },
      },
      { onSuccess: () => form.reset() },
    );
  }

  function submitBenchmark(event: FormEvent<HTMLFormElement>, model: ModelVersion) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    benchmark.mutate({
      id: model.id,
      body: {
        enabled: true,
        benchmark: {
          dataset: data.get("dataset"),
          host_profile: model.resource_profile.machine,
          sample_groups: Number(data.get("sample_groups")),
          structured_success_rate: Number(data.get("structured_success_rate")),
          median_duration_ms: Number(data.get("median_duration_ms")),
          peak_memory_gib: Number(data.get("peak_memory_gib")),
          approved_at: new Date().toISOString(),
        },
      },
    });
  }

  function submitPackage(event: FormEvent<HTMLFormElement>, model: ModelVersion) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    registerPackage.mutate(
      {
        id: model.id,
        body: {
          source_url: data.get("source_url"),
          source_revision: data.get("source_revision"),
          artifact_checksum: model.artifact_checksum,
          license_id: model.license_id,
          runtime_format: data.get("runtime_format"),
          size_bytes: Number(data.get("size_bytes")),
          relative_path: data.get("relative_path"),
          is_pinned: true,
        },
      },
      { onSuccess: () => form.reset() },
    );
  }

  const productionKeys = new Set(contract.data?.models.map((model) => model.model_key) ?? []);

  return (
    <>
      <PageHeader title="Models" />
      <ProductionRoute contract={contract.data} />
      <ProductionModels models={models.data ?? []} contract={contract.data} />
      <ExperimentModels models={models.data ?? []} productionKeys={productionKeys} packages={packages.data ?? []} benchmarkPending={benchmark.isPending} packagePending={registerPackage.isPending} onBenchmark={submitBenchmark} onPackage={submitPackage} />
      <ModelRegistration pending={register.isPending} error={register.error} onSubmit={submitRegistration} />
      <ModelErrors errors={[benchmark.error, registerPackage.error]} />
    </>
  );
}
