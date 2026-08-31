// FLOAT instrument (spec §5.3) — shared component embedded by OPPS (Task 9) and CALC (Task 10).
// Float (X, 0..1) vs price (Y) as a step curve across the five wear-bracket midpoints, with the
// skin's float-cap zone greyed, an average-float marker + optimizer ghost verticals, and
// CSFloat-resolved listings as points. Not a route — nothing imports it yet.
import { WEAR_BANDS, wearOfFloat, money, float as fmtFloat } from "../fmt.js";
import { uplotTheme, crosshairReadout, caption } from "../charts.js";

const CAPTION =
  "Where the output's value jumps as its float changes. Your input mix's average float is the blue line.";

// Pure. xs = midpoints of whichever of the 5 wear keys are present, ys = their prices,
// both sorted by xs ascending. Exported for the selftest and reused by callers.
export function stepSeriesFromWears(pricesByWear = {}) {
  const pts = [];
  for (const b of WEAR_BANDS) {
    const p = pricesByWear?.[b.short];
    if (p == null || Number.isNaN(Number(p))) continue;
    pts.push([(b.lo + b.hi) / 2, Number(p)]);
  }
  pts.sort((a, b) => a[0] - b[0]);
  return { xs: pts.map((p) => p[0]), ys: pts.map((p) => p[1]) };
}

// Linear-interpolate price at float f between the step knots; flat-extrapolate past the ends.
function priceAt(xs, ys, f) {
  if (!xs || !xs.length) return null;
  if (f <= xs[0]) return ys[0];
  if (f >= xs[xs.length - 1]) return ys[ys.length - 1];
  for (let i = 1; i < xs.length; i++) {
    if (f <= xs[i]) {
      const t = (f - xs[i - 1]) / ((xs[i] - xs[i - 1]) || 1);
      return ys[i - 1] + t * (ys[i] - ys[i - 1]);
    }
  }
  return ys[ys.length - 1];
}

export function mountFloatInstrument(el, opts = {}) {
  el.innerHTML = "";
  const box = document.createElement("div");
  box.className = "chart-box";
  el.appendChild(box);

  const theme = uplotTheme();
  const accent = theme._accent || "#5C7CFF";
  let cur = { ...opts };

  const dataFrom = (o) => {
    const { xs, ys } = stepSeriesFromWears(o.pricesByWear || {});
    return [xs, ys];
  };

  // spec §5.3 — cap zone + verticals + resolved points, all in the draw hook via u.ctx.
  // Coords here are canvas/device pixels: u.bbox and valToPos(_, _, true) both return them.
  const drawOverlay = (u) => {
    const o = cur;
    const { ctx } = u;
    const { left: bx, top: by, width: bw, height: bh } = u.bbox;
    const xAt = (v) => u.valToPos(v, "x", true);
    ctx.save();

    // out-of-range float cap: grey the < min and > max x-ranges
    const cap = o.floatCap || {};
    ctx.fillStyle = "rgba(90,100,114,0.20)"; // --text-faint, translucent
    if (Number.isFinite(cap.min) && cap.min > 0) {
      const px = Math.min(bx + bw, Math.max(bx, xAt(cap.min)));
      ctx.fillRect(bx, by, px - bx, bh);
    }
    if (Number.isFinite(cap.max) && cap.max < 1) {
      const px = Math.min(bx + bw, Math.max(bx, xAt(cap.max)));
      ctx.fillRect(px, by, bx + bw - px, bh);
    }

    // optimizer-proposed wear-mixes: faint dashed accent verticals
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.globalAlpha = 0.35;
    for (const g of o.ghosts || []) {
      if (!Number.isFinite(g)) continue;
      const px = xAt(g);
      ctx.beginPath(); ctx.moveTo(px, by); ctx.lineTo(px, by + bh); ctx.stroke();
    }

    // current input-mix average float: one solid accent vertical
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    if (Number.isFinite(o.marker)) {
      const px = xAt(o.marker);
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(px, by); ctx.lineTo(px, by + bh); ctx.stroke();
    }

    // CSFloat-resolved real listings
    ctx.fillStyle = accent;
    for (const p of o.resolvedPoints || []) {
      if (!Number.isFinite(p?.float) || !Number.isFinite(p?.price)) continue;
      ctx.beginPath();
      ctx.arc(xAt(p.float), u.valToPos(p.price, "y", true), 3, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.restore();
  };

  // cursor.left is CSS px within the plot area; posToVal wants CSS px.
  const onCursor = (u) => {
    const lx = u.cursor.left;
    if (lx == null || lx < 0) return;
    const f = Math.max(0, Math.min(1, u.posToVal(lx, "x")));
    const p = priceAt(u.data[0], u.data[1], f);
    crosshairReadout(el, [
      { label: "float", value: fmtFloat(f) },
      { label: "wear", value: wearOfFloat(f).name },
      { label: "~price", value: p == null ? "—" : money(p) },
    ]);
  };

  const u = new uPlot(
    {
      ...theme,
      width: box.clientWidth || 600,
      height: box.clientHeight || 260,
      scales: { x: { time: false, range: [0, 1] } },
      series: [
        {},
        {
          stroke: accent,
          width: 2,
          points: { show: true, size: 6 },
          paths: uPlot.paths.stepped({ align: 1 }),
        },
      ],
      hooks: { draw: [drawOverlay], setCursor: [onCursor] },
    },
    dataFrom(cur),
    box,
  );

  caption(el, cur.caption || CAPTION);

  return {
    update(newOpts = {}) {
      cur = { ...newOpts };
      u.setData(dataFrom(cur)); // redraw re-runs drawOverlay, which re-reads `cur` for markers
    },
    destroy() {
      try { u.destroy(); } catch { /* already gone */ }
      el.innerHTML = "";
    },
  };
}
