/**
 * Barrel for the photo-detail panels, split by concern under ./inspector.
 * Import paths stay stable while each panel lives in a reviewable module.
 */

export { AnalysisPanel } from "./inspector/analysisPanels";
export { CurationPanel, PhotoRatingOverlay } from "./inspector/curationPanels";
export type { HumanRatingSaveOptions } from "./inspector/humanRating";
export { HumanRatingPanel, RatingGuide } from "./inspector/humanRating";
export { AssetFacts, ImmichContextPanel, MetadataPanel, PeopleAndAlbums } from "./inspector/immichPanels";
export { IntelligenceLayers } from "./inspector/intelligenceLayers";
