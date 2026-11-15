import { lazy, Suspense, useRef, type ReactNode } from "react";
import { motion, useReducedMotion, useScroll, useSpring, useTransform } from "motion/react";
import { Badge } from "@/components/arc/badge/badge";
import { Button } from "@/components/arc/button/button";
import { ExpandableCard } from "@/components/arc/expandable-card/expandable-card";
import { Card } from "@/components/arc/card/card";
import { InViewTitle } from "@/components/arc/in-view-title/in-view-title";
import { SiteHeader } from "@/components/arc/blocks/site-header/site-header";
import { Bento } from "./Bento";
import { Datasets } from "./Datasets";
import { HowItWorks } from "./HowItWorks";
import { ProblemArt, AnswerArt, GetArt } from "./FeatureArt";
import { MarginGraphic } from "./MarginGraphic";
import { FaqSection, type FaqItem } from "@/components/arc/blocks/faq-section/faq-section";
import { CtaSection } from "@/components/arc/blocks/cta-section/cta-section";
import { SiteFooter } from "@/components/arc/blocks/site-footer/site-footer";

const HeroGlobe = lazy(() => import("./HeroGlobe"));
const MAP = "/app.html";
const go = (href: string) => () => { window.location.href = href; };

const Mark = ({ className }: { className?: string }) => (
  <img className={className} src="/favicon.svg" alt="" width={24} height={24} />
);

const faq: FaqItem[] = [
  { question: "What is NISAR?", answer: "NISAR is a radar satellite built by NASA and ISRO. It sends microwave pulses to the ground and measures what comes back, day or night and through cloud, and it passes over most of the planet every 12 days. Terra reads its original L-band GCOV products." },
  { question: "Is this real data?", answer: "Yes. Every analysis reads original NISAR GCOV files from the Alaska Satellite Facility, converts them to calibrated backscatter, and compares two dates. The scene in the how-it-works section is the one exception: it is synthetic, and labelled so." },
  { question: "How does Terra decide something changed?", answer: "It compares calibrated radar returns between compatible dates and chooses thresholds from the measured change distribution, accounting for speckle and pixel averaging. The report gives the chosen method and its limitations. Possible causes are hypotheses for independent observations to check." },
  { question: "Which polarizations does it use?", answer: "Every diagonal polarization the product has, such as HH and HV, so the report can say which one shows the change. Different surfaces respond to different polarizations." },
  { question: "Can I trust the written explanation?", answer: "The writer is a language model. Every number and date in its answer is checked against the evidence from the analysis, and each answer is labelled with the model that wrote it. Treat the causes it proposes as hypotheses to check, not findings." },
  { question: "Are the 3D tiles used to measure anything?", answer: "No. Google Photorealistic 3D Tiles are display only. While they are on screen the pointer readout is switched off, and all science comes from NISAR, Sentinel-2 and Terra's own terrain." },
];

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="section">
      <InViewTitle text={title} as="h2" variant="blur" className="section-title" />
      {children}
    </section>
  );
}

function Hero() {
  const ref = useRef<HTMLElement>(null);
  const reduced = useReducedMotion();
  const { scrollYProgress } = useScroll({ target: ref, offset: ["start start", "end start"] });
  const p = useSpring(scrollYProgress, { stiffness: 90, damping: 24, mass: 0.4 });
  const y = useTransform(p, [0, 1], [0, -120]);
  const textY = useTransform(p, [0, 1], [0, 60]);
  const textOpacity = useTransform(p, [0, 0.7], [1, 0]);
  const moving = reduced ? {} : { y };
  return (
    <section ref={ref} className="hero" aria-labelledby="hero-title">
      <motion.div className="hero-copy" style={reduced ? undefined : { y: textY, opacity: textOpacity }}>
        <h1 id="hero-title" className="wordmark">Terra</h1>
        <p className="tagline">See what changed on Earth</p>
        <p className="hero-sub">Terra reads NASA-ISRO NISAR radar to show where the ground, water and vegetation changed between two dates, and how sure we are.</p>
        <div className="hero-actions">
          <Button size="lg" onClick={go(MAP)}>Open the satellite map</Button>
          <Button size="lg" variant="secondary" onClick={go("#what")}>What is this?</Button>
        </div>
      </motion.div>
      <motion.div className="hero-globe" style={moving} role="img" aria-label="A globe of the Earth with a marker at Pacaya–Samiria in the Peruvian Amazon">
        <Suspense fallback={null}><HeroGlobe onOpen={go(MAP)} /></Suspense>
      </motion.div>
    </section>
  );
}

export function Landing() {
  return (
    <>
      <SiteHeader
        variant="simple"
        brand={{ name: "Terra", href: "/", mark: <Mark /> }}
        items={[
          { value: "what", label: "What is it?", href: "#what" },
          { value: "data", label: "Data", href: "#data" },
          { value: "how", label: "How it works", href: "#how" },
          { value: "scale", label: "Reading change", href: "#scale" },
          { value: "faq", label: "FAQ", href: "#faq" },
        ]}
        secondaryAction={null}
        primaryAction={{ label: "Open the map", href: MAP }}
      />

      <main>
        <Hero />

        <Section id="what" title="What is Terra?">
          <div className="explain">
            <p className="lede">
              Imagine photographing a river valley every few weeks, in any weather, in the dark, and laying the photos on top of each other. Whatever moved, flooded, grew or fell shows up as a difference.
            </p>
            <p className="lede">
              That is radar. Terra is a map of the Earth that does the laying-on-top for you. You pick a place and two dates, and it shows <strong>where the surface changed</strong>, how much, and what is likely behind it.
            </p>
          </div>
          <div className="cards feature-cards">
            <Card title="The problem" media={<ProblemArt />} status="Why it is hard" description="Radar images are grainy and hard to read, and two dates taken from different angles cannot be compared at all." />
            <Card title="The answer" media={<AnswerArt />} status="What Terra does" description="Pair only passes with the same geometry, subtract them in decibels, and flag the patches whose change stands out from that scene's own speckle." />
            <Card title="What you get" media={<GetArt />} status="What you see" description="A change map on the globe, up to six outlined hotspots, and a written answer whose numbers are checked against the evidence." />
          </div>
        </Section>

        <Section id="numbers" title="Built on real radar">
          <Bento />
        </Section>

        <Section id="data" title="The data behind it">
          <p className="lede">Terra combines NISAR radar with optical imagery, land cover, weather and live hazard feeds. Here is each one, what it is used for, and how they connect.</p>
          <Datasets />
        </Section>

        <HowItWorks />

        <Section id="scale" title="Reading the change">
          <p className="lede">
            Radar brightness is measured in decibels. Smooth water returns almost nothing, vegetation and soil return some, and buildings and rough rock return a lot. A drop of several decibels in one place usually means the surface itself changed. Terra reports the <strong>water-like</strong> and <strong>very bright</strong> areas separately so the two are never mixed up.
          </p>
          <MarginGraphic />
          <div className="statuses">
            <Badge tone="info" dot size="lg">Blue: got darker, more water-like</Badge>
            <Badge tone="warning" dot size="lg">Amber: got brighter</Badge>
            <Badge tone="neutral" dot size="lg">Grey: below the scene's threshold, not flagged</Badge>
          </div>
          <ExpandableCard title="Show the rule">
            <code className="formula">change (dB) = later backscatter − earlier backscatter, flagged when |change| ≥ T, where T comes from the scene's change histogram (Kittler–Illingworth), else the 0.1% speckle false-alarm level, else 3 dB</code>
          </ExpandableCard>
        </Section>

        <section id="faq" className="section">
          <FaqSection variant="accordion" title="Questions" description="What Terra does, and what it does not." items={faq} contact={null} />
        </section>

        <CtaSection
          variant="centered"
          title="Find out what changed"
          description="Open the satellite map, pick a place and two dates, and read the change."
          primaryAction={{ label: "Open the satellite map", href: MAP }}
          secondaryAction={null}
          faces={[]}
          note="Built for NASA Space Apps 2026"
        />
      </main>

      <SiteFooter
        variant="minimal"
        brand={{ name: "Terra", href: "/", mark: <Mark /> }}
        tagline="See what changed on Earth. Earth texture: Solar System Scope, CC BY 4.0. Radar: NASA-ISRO NISAR via the Alaska Satellite Facility."
        links={[{ label: "Satellite map", href: MAP }, { label: "How it works", href: "#how" }, { label: "FAQ", href: "#faq" }]}
        legal={[]}
        socials={[]}
        newsletter={null}
        status={null}
      />
    </>
  );
}
