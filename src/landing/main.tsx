import { createRoot } from "react-dom/client";
import Lenis from "lenis";
import "@fontsource-variable/archivo/wdth.css";
import "@fontsource-variable/newsreader/opsz.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/800.css";
import "@/components/arc/foundation.css";
import "./tailwind.css";
import "./landing.css";
import { Landing } from "./Landing";

createRoot(document.getElementById("root")!).render(<Landing />);

// Smooth scrolling. Skipped for reduced motion, where native scrolling is kept.
if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const lenis = new Lenis({ anchors: true, lerp: 0.1 });
  const raf = (time: number) => { lenis.raf(time); requestAnimationFrame(raf); };
  requestAnimationFrame(raf);
}
