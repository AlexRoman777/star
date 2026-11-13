import { useState } from "react";

import { readFacesLayerPreference, writeFacesLayerPreference } from "./faceBoxes";

/** Remembered per browser; off until the owner turns it on once. */
export function useFacesLayerPreference(): [boolean, (value: boolean) => void] {
  const [enabled, setEnabled] = useState(() => readFacesLayerPreference(typeof window === "undefined" ? null : window.localStorage));
  const update = (value: boolean) => {
    setEnabled(value);
    writeFacesLayerPreference(typeof window === "undefined" ? null : window.localStorage, value);
  };
  return [enabled, update];
}

