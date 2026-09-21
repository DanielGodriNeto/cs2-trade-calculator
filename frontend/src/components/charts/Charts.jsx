// Standardized chart components. The old dashboard had 14 charts with inconsistent styling and
// no plain-language captions ("the graphs don't make sense"). Here every chart shares one theme
// helper and every one is wrapped with a one-line caption telling the user what it means.
import { Bar, Line, Radar } from "react-chartjs-2";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  RadialLinearScale,
  BarElement,
  PointElement,
  LineElement,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";

ChartJS.register(CategoryScale, LinearScale, RadialLinearScale, BarElement, PointElement, LineElement, Tooltip, Legend, Filler);

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function baseOptions() {
  const grid = cssVar("--border");
  const text = cssVar("--text-2");
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: text, boxWidth: 12, font: { size: 11 } } },
      tooltip: { intersect: false, mode: "index" },
    },
    scales: {
      x: { grid: { color: grid }, ticks: { color: text, font: { size: 11 } } },
      y: { grid: { color: grid }, ticks: { color: text, font: { size: 11 } } },
    },
  };
}

function ChartFrame({ caption, children }) {
  return (
    <div>
      <div className="chart-box">{children}</div>
      {caption && <div className="chart-caption">{caption}</div>}
    </div>
  );
}

// (1) Histogram — distribution of a value (e.g. ROI across finds).
export function Histogram({ labels, values, label, color, caption }) {
  const c = color || cssVar("--accent");
  return (
    <ChartFrame caption={caption}>
      <Bar
        data={{ labels, datasets: [{ label, data: values, backgroundColor: c, borderRadius: 4 }] }}
        options={{ ...baseOptions(), plugins: { ...baseOptions().plugins, legend: { display: false } } }}
      />
    </ChartFrame>
  );
}

// (2) Line — a trend over time / rounds (e.g. cumulative profit, price history).
export function TrendLine({ labels, series, caption }) {
  const palette = [cssVar("--accent"), cssVar("--info"), cssVar("--warn"), cssVar("--loss")];
  const datasets = series.map((s, i) => ({
    label: s.label,
    data: s.values,
    borderColor: s.color || palette[i % palette.length],
    backgroundColor: "transparent",
    tension: 0.25,
    pointRadius: s.values.length > 40 ? 0 : 2,
    borderWidth: 2,
  }));
  return (
    <ChartFrame caption={caption}>
      <Line data={{ labels, datasets }} options={baseOptions()} />
    </ChartFrame>
  );
}

// (4) Radar — multi-axis fingerprint (e.g. each agent's live self-tuned search parameters).
export function RadarChart({ axes, series, caption }) {
  const palette = [cssVar("--accent"), cssVar("--info"), cssVar("--warn"), cssVar("--loss"), "#b26fe0", "#e08a4f"];
  const grid = cssVar("--border");
  const text = cssVar("--text-2");
  const datasets = series.map((s, i) => {
    const c = s.color || palette[i % palette.length];
    return {
      label: s.label,
      data: s.values,
      borderColor: c,
      backgroundColor: "transparent",
      pointBackgroundColor: c,
      borderWidth: 2,
      pointRadius: 2,
    };
  });
  return (
    <ChartFrame caption={caption}>
      <Radar
        data={{ labels: axes, datasets }}
        options={{
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { labels: { color: text, boxWidth: 12, font: { size: 11 } } } },
          scales: {
            r: {
              min: 0, max: 1,
              grid: { color: grid },
              angleLines: { color: grid },
              pointLabels: { color: text, font: { size: 10 } },
              ticks: { display: false },
            },
          },
        }}
      />
    </ChartFrame>
  );
}

// (3) Bars — one value per category (e.g. expected profit per agent).
export function Bars({ labels, values, label, caption }) {
  const c = cssVar("--accent");
  return (
    <ChartFrame caption={caption}>
      <Bar
        data={{ labels, datasets: [{ label, data: values, backgroundColor: c, borderRadius: 4 }] }}
        options={{ ...baseOptions(), plugins: { ...baseOptions().plugins, legend: { display: false } } }}
      />
    </ChartFrame>
  );
}
