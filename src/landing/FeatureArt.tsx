import { useSyncExternalStore } from "react";
import { Dithering } from "@paper-design/shaders-react";

/** Dithered gradients for the three feature cards, the same Paper Shaders look the Avian hero uses. Decorative and static. */
const noopSubscribe = () => () => {};
const hasWebGL2 = () => {
  try { return !!document.createElement("canvas").getContext("webgl2"); } catch { return false; }
};

type Shape = "warp" | "sphere" | "ripple";
function Dither({ shape, front, back = "#1b1c18", scale = 1 }: { shape: Shape; front: string; back?: string; scale?: number }) {
  // Without WebGL2 the card's own muted background shows instead of a blank canvas.
  const supported = useSyncExternalStore(noopSubscribe, hasWebGL2, () => false);
  if (!supported) return null;
  return (
    <div className="fart" aria-hidden="true">
      <Dithering width="100%" height="100%" colorBack={back} colorFront={front} shape={shape} type="4x4" size={2.5} scale={scale} speed={0} />
    </div>
  );
}

export const ProblemArt = () => <Dither shape="warp" front="#ff5a1f" scale={0.8} />;
export const AnswerArt = () => <Dither shape="sphere" front="#ece9df" back="#151613" scale={0.9} />;
export const GetArt = () => <Dither shape="ripple" front="#8fbf6a" />;
