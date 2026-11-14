import { Carousel } from "@/components/arc/carousel/carousel";
import { ExpandableCard } from "@/components/arc/expandable-card/expandable-card";

type Dataset = { name: string; source: string; role: string; used: string; feeds: string; status?: "context" };

const datasets: Dataset[] = [
  { name: "NISAR GCOV radar", source: "NASA-ISRO NISAR, L-band, original GCOV products from the Alaska Satellite Facility", role: "The change itself", used: "Terra reads small windows of the original files over HTTP byte ranges, converts them to calibrated backscatter in decibels, and subtracts the earlier date from the later one.", feeds: "Change map, hotspots, histograms" },
  { name: "ASF and CMR catalogs", source: "Alaska Satellite Facility and NASA Common Metadata Repository", role: "Which passes exist", used: "Finds every NISAR acquisition over your area, then keeps only pairs from the same track, frame, direction and release, so the two images can be compared fairly.", feeds: "Date picker, pair checks" },
  { name: "Sentinel-2 true colour", source: "ESA Copernicus, optical", role: "What it looks like", used: "A normal photograph of the same place, so you can see whether a bright or dark patch is a field, a river or a burn scar.", feeds: "Optical layer, context", status: "context" },
  { name: "ESA WorldCover", source: "ESA, 10 m land cover", role: "What kind of ground", used: "Reports the share of forest, crops, water and built-up land inside your area, so a change can be read against the right surface.", feeds: "Land cover context", status: "context" },
  { name: "JRC Global Surface Water", source: "European Commission Joint Research Centre", role: "Was it water before?", used: "Splits newly water-like area into water that was usually there and water that is new.", feeds: "Water context", status: "context" },
  { name: "Live hazards", source: "NASA FIRMS, NASA EONET, USGS and GDACS", role: "What is happening now", used: "Fires, earthquakes, volcanoes and other events, drawn on the globe so a change can be placed next to a known cause.", feeds: "Hazard layers, timeline" },
  { name: "Weather", source: "Open-Meteo", role: "Rain before the pass", used: "Recent rainfall and temperature for your area, since wet soil and frozen ground both change radar brightness.", feeds: "Conditions panel", status: "context" },
];

const chain = [
  { head: "Pick", items: ["A place on the globe", "Two dates", "A question in plain words"] },
  { head: "Read", items: ["NISAR radar for both dates", "Only pairs that can be compared fairly", "Every polarization the product has"] },
  { head: "Compare", items: ["Backscatter in decibels", "Later minus earlier", "Water-like and very bright area", "Up to six hotspots"] },
  { head: "Explain", items: ["Context from optical, land cover and weather", "A written answer", "Every number and date checked against the evidence"] },
];

export function Datasets() {
  return (
    <div className="ds-split">
      <div className="ds-left">
        <Carousel label="How the data fits together" slideSize="min(86cqw, 360px)" slideLabel={(i, n) => `${chain[i].head}, step ${i + 1} of ${n}`}>
          {chain.map((c, i) => (
            <div key={c.head} className="chain-col">
              <span className="chain-step">{i + 1}</span>
              <h3>{c.head}</h3>
              <ul>{c.items.map((it) => <li key={it}>{it}</li>)}</ul>
            </div>
          ))}
        </Carousel>
      </div>
      <div className="ds-right">
        {datasets.map((d, i) => (
          <ExpandableCard key={d.name} title={d.name} description={d.role} defaultExpanded={i === 0}>
            <div className="ds-details">
              <p className="ds-source">{d.source}</p>
              <p>{d.used}</p>
              <p className="ds-feeds"><span>Feeds</span> {d.feeds}</p>
              {d.status && <p className="ds-status">Context only. It never changes the radar result.</p>}
            </div>
          </ExpandableCard>
        ))}
      </div>
    </div>
  );
}
