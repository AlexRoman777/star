import { PageHeader } from "../components/PageHeader";
import { InferenceSettingsCard } from "../features/inference/InferenceSettingsCard";

/** Settings → Inference: the production sampling, reasoning, and media limits per review stage. */
export function InferencePage() {
  return <div className="inference-page">
    <PageHeader title="Inference" description="Sampling, thinking, and image limits for each production review stage." />
    <InferenceSettingsCard />
  </div>;
}
