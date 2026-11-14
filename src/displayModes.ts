import * as C from 'cesium';

/**
 * Display filters applied after the scene renders. They change how the image is
 * shown, never the data: a FLIR palette here is a luminance remap, not thermal data.
 */
export type DisplayMode = 'normal' | 'nvg' | 'flir' | 'crt' | 'mono';
export const DISPLAY_MODES: { id: DisplayMode; label: string; hint: string }[] = [
  { id: 'normal', label: 'NORMAL', hint: 'True-colour imagery' },
  { id: 'nvg', label: 'NVG', hint: 'Image-intensifier look' },
  { id: 'flir', label: 'FLIR', hint: 'White-hot luminance palette' },
  { id: 'crt', label: 'CRT', hint: 'Phosphor monitor' },
  { id: 'mono', label: 'MONO', hint: 'High-contrast monochrome' },
];

const COMMON = /* glsl */ `
uniform sampler2D colorTexture;
uniform vec2 colorTextureDimensions;
uniform float u_time;
in vec2 v_textureCoordinates;
float hash12(vec2 p) { vec3 p3 = fract(vec3(p.xyx) * 0.1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }
float luma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }
float vignette(vec2 uv, float strength) { vec2 d = uv - 0.5; return clamp(1.0 - dot(d, d) * strength, 0.0, 1.0); }
`;

const SHADERS: Record<Exclude<DisplayMode, 'normal'>, string> = {
  nvg: COMMON + /* glsl */ `
void main() {
  vec2 uv = v_textureCoordinates;
  vec2 texel = 1.0 / colorTextureDimensions;
  float l = luma(texture(colorTexture, uv).rgb);
  // soft halo around bright sources, as an intensifier tube blooms
  float halo = 0.0;
  for (int i = -2; i <= 2; i++) for (int j = -2; j <= 2; j++) {
    float s = luma(texture(colorTexture, uv + vec2(float(i), float(j)) * texel * 3.0).rgb);
    halo += smoothstep(0.55, 1.0, s);
  }
  halo /= 25.0;
  float gain = pow(clamp(l * 1.35, 0.0, 1.0), 0.95) * 0.9 + halo * 0.4;
  float grain = (hash12(uv * colorTextureDimensions + u_time * 61.0) - 0.5) * 0.09;
  float line = 0.94 + 0.06 * sin(uv.y * colorTextureDimensions.y * 1.4);
  vec3 phosphor = vec3(0.30, 1.0, 0.36);
  vec3 c = phosphor * clamp(gain + grain, 0.0, 1.2) * line * vignette(uv, 1.9);
  out_FragColor = vec4(c, 1.0);
}`,
  flir: COMMON + /* glsl */ `
vec3 ironbow(float t) {
  return clamp(vec3(
    smoothstep(0.05, 0.55, t) + 0.2 * smoothstep(0.85, 1.0, t),
    smoothstep(0.45, 0.95, t),
    smoothstep(0.0, 0.25, t) * (1.0 - smoothstep(0.35, 0.6, t)) + smoothstep(0.88, 1.0, t)), 0.0, 1.0);
}
void main() {
  vec2 uv = v_textureCoordinates;
  vec3 src = texture(colorTexture, uv).rgb;
  // white-hot: brighter, warmer surfaces read hotter; vegetation and water read cool
  float l = luma(src) * 0.85 + (src.r - src.b) * 0.25;
  float t = clamp(pow(clamp(l, 0.0, 1.0), 0.85) * 1.15, 0.0, 1.0);
  float grain = (hash12(uv * colorTextureDimensions + u_time * 37.0) - 0.5) * 0.04;
  vec3 c = mix(vec3(t), ironbow(t), 0.18) + grain;
  out_FragColor = vec4(c * vignette(uv, 1.2), 1.0);
}`,
  crt: COMMON + /* glsl */ `
vec2 curve(vec2 uv) { uv = uv * 2.0 - 1.0; uv *= 1.0 + dot(uv.yx, uv.yx) * vec2(0.035, 0.045); return uv * 0.5 + 0.5; }
void main() {
  vec2 uv = curve(v_textureCoordinates);
  if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) { out_FragColor = vec4(0.0, 0.0, 0.0, 1.0); return; }
  vec2 off = vec2(1.4 / colorTextureDimensions.x, 0.0);
  vec3 c = vec3(texture(colorTexture, uv + off).r, texture(colorTexture, uv).g, texture(colorTexture, uv - off).b);
  float l = luma(c);
  vec3 amber = vec3(1.0, 0.66, 0.16) * pow(l, 0.9) * 1.35;
  float scan = 0.82 + 0.18 * sin(uv.y * colorTextureDimensions.y * 3.14159);
  float mask = 0.93 + 0.07 * sin(uv.x * colorTextureDimensions.x * 2.094);
  float flicker = 0.985 + 0.015 * sin(u_time * 9.0);
  out_FragColor = vec4(amber * scan * mask * flicker * vignette(uv, 1.6), 1.0);
}`,
  mono: COMMON + /* glsl */ `
void main() {
  vec2 uv = v_textureCoordinates;
  float l = luma(texture(colorTexture, uv).rgb);
  l = smoothstep(0.04, 0.82, l);
  float grain = (hash12(uv * colorTextureDimensions + u_time * 13.0) - 0.5) * 0.035;
  out_FragColor = vec4(vec3(l + grain) * vignette(uv, 1.1), 1.0);
}`,
};

export class DisplayModes {
  private stages = new Map<DisplayMode, C.PostProcessStage>();
  private timer: ReturnType<typeof setInterval> | undefined;
  private start = performance.now();
  constructor(private viewer: C.Viewer) {
    for (const [id, fragmentShader] of Object.entries(SHADERS) as [Exclude<DisplayMode, 'normal'>, string][]) {
      const stage = new C.PostProcessStage({ name: `terra-${id}`, fragmentShader, uniforms: { u_time: () => (performance.now() - this.start) / 1000 } });
      stage.enabled = false;
      viewer.scene.postProcessStages.add(stage);
      this.stages.set(id, stage);
    }
  }
  set(mode: DisplayMode) {
    this.stages.forEach((stage, id) => { stage.enabled = id === mode; });
    clearInterval(this.timer); this.timer = undefined;
    // Grain and flicker need fresh frames; the renderer otherwise sleeps when idle.
    if (mode !== 'normal') this.timer = setInterval(() => { if (!this.viewer.isDestroyed()) this.viewer.scene.requestRender(); }, 66);
    this.viewer.scene.requestRender();
  }
  destroy() { clearInterval(this.timer); }
}
