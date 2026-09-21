import { useEffect, useRef, useState } from "react";
import { api } from "../api/client.js";
import { SkinImage } from "./ui.jsx";

// Debounced skin-name autocomplete backed by /api/skins. Calls onPick(skin) with the full
// skin object (name, image, wears, tradeable, ...). Used by the Calculator's builder.
export default function SkinPicker({ onPick, placeholder = "Start typing a skin name…" }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState([]);
  const [open, setOpen] = useState(false);
  const timer = useRef();

  useEffect(() => {
    clearTimeout(timer.current);
    if (q.trim().length < 2) {
      setResults([]);
      return;
    }
    timer.current = setTimeout(async () => {
      try {
        const r = await api.skins(q.trim());
        setResults(r);
        setOpen(true);
      } catch {
        setResults([]);
      }
    }, 220);
    return () => clearTimeout(timer.current);
  }, [q]);

  return (
    <div style={{ position: "relative" }}>
      <input
        type="text"
        value={q}
        placeholder={placeholder}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => results.length && setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
      />
      {open && results.length > 0 && (
        <div style={{
          position: "absolute", top: "100%", left: 0, right: 0, zIndex: 30,
          background: "var(--surface)", border: "1px solid var(--border-strong)",
          borderRadius: "var(--radius-sm)", maxHeight: 280, overflowY: "auto", boxShadow: "var(--shadow)",
        }}>
          {results.map((s) => (
            <div
              key={s.name}
              onMouseDown={() => { onPick(s); setQ(""); setResults([]); setOpen(false); }}
              style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", cursor: "pointer", opacity: s.tradeable ? 1 : 0.5 }}
              onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-2)")}
              onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
            >
              <SkinImage url={s.image} name={s.name} />
              <span style={{ flex: 1 }}>{s.name}</span>
              {!s.tradeable && <span style={{ fontSize: "0.72rem", color: "var(--loss)" }}>can't trade up</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
