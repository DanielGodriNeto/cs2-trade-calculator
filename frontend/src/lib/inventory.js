// Client-side cache of the last-loaded Steam inventory, so the Opportunities and Calculator
// views can be inventory-aware ("you own X/10 inputs") without re-fetching. Written by the
// Inventory view after a successful /api/inventory load. Kept in localStorage so it survives
// reloads; the server also caches server-side (data/inventory_cache.json) for the API layer.

const KEY = "cs2_inventory";

export function saveInventory(payload) {
  try {
    localStorage.setItem(KEY, JSON.stringify({ savedAt: Date.now(), ...payload }));
  } catch {
    /* quota / private mode — non-fatal, inventory-awareness just won't persist */
  }
}

export function loadInventory() {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function clearInventory() {
  localStorage.removeItem(KEY);
}

// Map of "skin name|wear" -> count owned, from a loaded inventory payload's items[].
export function ownedCounts(inv) {
  const out = {};
  if (!inv || !Array.isArray(inv.items)) return out;
  for (const it of inv.items) {
    const key = `${it.name || it.market_hash_name}|${it.wear || ""}`;
    out[key] = (out[key] || 0) + (it.count || 1);
  }
  return out;
}
