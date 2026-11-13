import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { applyTheme, readTheme } from "./features/display/theme";
import "./styles/tokens.css";
import "./styles/global.css";
import "./styles/metrics.css";
import "./styles/experience.css";
import "./styles/darkroom.css";
import "./styles/beta.css";
import "./styles/luminous.css";
import "./styles/home.css";

applyTheme(readTheme());
try { document.documentElement.dataset.density = window.localStorage.getItem("star.density.v1") === "comfortable" ? "comfortable" : "compact"; } catch { document.documentElement.dataset.density = "compact"; }
document.documentElement.dataset.privatePreviews = "hidden";

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
