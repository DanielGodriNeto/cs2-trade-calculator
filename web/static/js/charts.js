import { money, pct, float } from "./fmt.js";

export const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

export function uplotTheme() {
  const grid = cssVar("--grid"), tick = cssVar("--text-dim"), accent = cssVar("--accent");
  return {
    legend: { show: false },
    cursor: { x: true, y: false, points: { show: false }, drag: { x: false, y: false } },
    axes: [
      { stroke: tick, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, font: "11px 'JetBrains Mono', monospace" },
      { stroke: tick, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, font: "11px 'JetBrains Mono', monospace" },
    ],
    _accent: accent,
  };
}

export function caption(el, text) {
  el.querySelector(":scope > .chart-caption")?.remove();
  const p = document.createElement("p");
  p.className = "chart-caption";
  p.textContent = text;
  el.appendChild(p);
}

export function crosshairReadout(panelEl, rows) {
  let box = panelEl.querySelector(":scope > .readout");
  if (!box) { box = document.createElement("div"); box.className = "readout"; panelEl.appendChild(box); }
  box.innerHTML = rows.map((r) =>
    `<div class="readout-row ${r.tone || ""}"><span class="readout-label">${r.label}</span><span class="readout-val">${r.value}</span></div>`
  ).join("");
}

export function sparkline(el, values, { stroke } = {}) {
  const w = el.clientWidth || 80, h = el.clientHeight || 20;
  const u = new uPlot({
    width: w, height: h, cursor: { show: false }, legend: { show: false },
    scales: { x: { time: false } },
    axes: [{ show: false }, { show: false }],
    series: [{}, { stroke: stroke || cssVar("--text-dim"), width: 1, points: { show: false } }],
  }, [values.map((_, i) => i), values], el);
  return { destroy: () => u.destroy() };
}

export function heatmap(canvas, opts) {
  const { rows, cols, values, colorFor, fmt, onHover, onClick } = opts;
  const LEGEND_H = 20;
  const ctx = canvas.getContext("2d");
  const gridCol = cssVar("--grid") || "#1F242C";

  const cellW = () => canvas.width / cols.length;
  const cellH = () => (canvas.height - LEGEND_H) / rows.length;

  function hitTest(px, py) {
    if (py < 0 || py >= canvas.height - LEGEND_H || px < 0 || px >= canvas.width) return null;
    const c = Math.floor(px / cellW()), r = Math.floor(py / cellH());
    if (r < 0 || r >= rows.length || c < 0 || c >= cols.length) return null;
    return { r, c };
  }

  let flat = values.flat().filter((v) => Number.isFinite(v));
  const min = Math.min(...flat), max = Math.max(...flat);

  function draw(hoverRC) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const w = cellW(), h = cellH();
    for (let r = 0; r < rows.length; r++) {
      for (let c = 0; c < cols.length; c++) {
        ctx.fillStyle = colorFor(values[r][c], min, max);
        ctx.fillRect(c * w, r * h, w - 1, h - 1);               // -1 leaves the 1px grid gap
      }
    }
    if (hoverRC) {
      ctx.strokeStyle = cssVar("--accent");
      ctx.lineWidth = 1;
      ctx.strokeRect(hoverRC.c * w + 0.5, hoverRC.r * h + 0.5, w - 2, h - 2);
    }
    // legend strip: min→max gradient
    const g = ctx.createLinearGradient(0, 0, canvas.width, 0);
    g.addColorStop(0, colorFor(min, min, max));
    g.addColorStop(1, colorFor(max, min, max));
    ctx.fillStyle = g;
    ctx.fillRect(0, canvas.height - LEGEND_H + 4, canvas.width, LEGEND_H - 8);
  }

  function move(e) {
    const rect = canvas.getBoundingClientRect();
    const rc = hitTest(e.clientX - rect.left, e.clientY - rect.top);
    draw(rc);
    if (rc) onHover(rc.r, rc.c);
  }
  function click(e) {
    const rect = canvas.getBoundingClientRect();
    const rc = hitTest(e.clientX - rect.left, e.clientY - rect.top);
    if (rc) onClick(rc.r, rc.c);
  }
  canvas.addEventListener("mousemove", move);
  canvas.addEventListener("click", click);
  draw(null);

  return {
    destroy() { canvas.removeEventListener("mousemove", move); canvas.removeEventListener("click", click); },
    hitTest,
  };
}
