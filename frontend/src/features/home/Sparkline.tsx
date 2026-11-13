const WIDTH = 320;
const HEIGHT = 96;
const PAD = 4;

function pathFor(values: number[], max: number): { line: string; area: string; last: { x: number; y: number } | null } {
  if (!values.length) return { line: "", area: "", last: null };
  const step = values.length > 1 ? (WIDTH - PAD * 2) / (values.length - 1) : 0;
  const points = values.map((value, index) => ({ x: PAD + index * step, y: HEIGHT - PAD - (max > 0 ? (value / max) * (HEIGHT - PAD * 2) : 0) }));
  const line = points.map((point, index) => `${index === 0 ? "M" : "L"}${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(" ");
  const first = points[0]!;
  const end = points[points.length - 1]!;
  const area = `${line} L${end.x.toFixed(1)} ${HEIGHT - PAD} L${first.x.toFixed(1)} ${HEIGHT - PAD} Z`;
  return { line, area, last: end };
}

/**
 * A small area chart for the last day of accepted results. Series share one
 * vertical scale so a quiet senior line stays honest next to the primary one.
 */
export function Sparkline({ primary, senior, live }: { primary: number[]; senior: number[]; live: boolean }) {
  const max = Math.max(1, ...primary, ...senior);
  const main = pathFor(primary, max);
  const second = pathFor(senior, max);
  return <svg className="home-sparkline" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none" aria-hidden="true">
    <defs>
      <linearGradient id="spark-primary" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stopColor="var(--accent)" stopOpacity=".55" />
        <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
      </linearGradient>
      <linearGradient id="spark-senior" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stopColor="var(--tone-wait)" stopOpacity=".4" />
        <stop offset="100%" stopColor="var(--tone-wait)" stopOpacity="0" />
      </linearGradient>
    </defs>
    {[.25, .5, .75].map((fraction) => <line key={fraction} className="home-sparkline-grid" x1={PAD} x2={WIDTH - PAD} y1={HEIGHT - PAD - fraction * (HEIGHT - PAD * 2)} y2={HEIGHT - PAD - fraction * (HEIGHT - PAD * 2)} />)}
    {second.area && <path className="home-sparkline-area senior" d={second.area} fill="url(#spark-senior)" />}
    {second.line && <path className="home-sparkline-line senior" d={second.line} />}
    {main.area && <path className="home-sparkline-area" d={main.area} fill="url(#spark-primary)" />}
    {main.line && <path className="home-sparkline-line" d={main.line} />}
    {main.last && <circle className={`home-sparkline-dot${live ? " live" : ""}`} cx={main.last.x} cy={main.last.y} r="3.2" />}
  </svg>;
}
