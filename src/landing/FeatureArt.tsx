import { useSyncExternalStore } from "react";
import { Dithering } from "@paper-design/shaders-react";

/** Dithered gradients for the three feature cards, the same Paper Shaders look the Avian hero uses. Decorative and static. */
const noopSubscribe = () => () => {};
const hasWebGL2 = () => {
  try { return !!document.createElement("canvas").getContext("webgl2"); } catch { return false; }
};

type Shape = "warp" | "sphere" | "ripple";
function Dither({ shape, front, back = "#162226", scale = 1 }: { shape: Shape; front: string; back?: string; scale?: number }) {
  // Without WebGL2 the card's own muted background shows instead of a blank canvas.
  const supported = useSyncExternalStore(noopSubscribe, hasWebGL2, () => false);
  if (!supported) return null;
  return (
    <div className="fart" aria-hidden="true">
      <Dithering width="100%" height="100%" colorBack={back} colorFront={front} shape={shape} type="4x4" size={2.5} scale={scale} speed={0} />
    </div>
  );
}

export const ProblemArt = () => <Dither shape="warp" front="#7fb0e0" scale={0.8} />;
export const AnswerArt = () => <Dither shape="sphere" front="#e8f0e6" back="#10191c" scale={0.9} />;
export const GetArt = () => <Dither shape="ripple" front="#c0d9bd" />;
