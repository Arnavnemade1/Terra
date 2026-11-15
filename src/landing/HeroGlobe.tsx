import { useEffect, useRef, useState } from "react";
import { Globe3D, type GlobeMarker } from "@/components/ui/3d-globe";

// Pacaya-Samiria, the Peruvian Amazon wetland that is Terra's default area on the map.
const PACAYA = { lat: -5.4, lng: -74.65 };
const markers: GlobeMarker[] = [{ ...PACAYA, src: "", label: "Pacaya–Samiria" }];

// Earth 2K color map by Solar System Scope (CC BY 4.0), also used as the bump map.
// initialRotation turns the planet so the marker faces the camera (see latLngToVector3 in 3d-globe.tsx).
const rad = Math.PI / 180;
const config = {
  textureUrl: "/brand/earth.jpg",
  bumpMapUrl: "/brand/earth.jpg",
  showAtmosphere: true,
  atmosphereColor: "#7fb0e0",
  atmosphereIntensity: 0.35,
  atmosphereBlur: 3,
  bumpScale: 1.2,
  autoRotateSpeed: 0,
  initialRotation: { x: PACAYA.lat * rad, y: Math.atan2(Math.cos((PACAYA.lng + 180) * rad), Math.sin((PACAYA.lng + 180) * rad)) },
};

export default function HeroGlobe({ onOpen }: { onOpen: () => void }) {
  // Stop rendering WebGL while the hero is scrolled out of view, so the rest of the page scrolls smoothly.
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const io = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { rootMargin: "100px" });
    io.observe(node);
    return () => io.disconnect();
  }, []);
  return (
    <div ref={ref}>
      <Globe3D markers={markers} config={config} paused={!visible} className="h-[min(1100px,130vw)]" onMarkerClick={onOpen} />
    </div>
  );
}
