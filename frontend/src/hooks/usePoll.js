import { useQuery } from "@tanstack/react-query";

// One shared polling primitive built on TanStack Query — replaces the old app.js's scattered
// setInterval calls and its manual "don't overlap ticks" guard (Query dedupes and won't fire a
// new request while one is in flight). Pass a stable key and a fetcher; `ms` controls the poll
// cadence (0 = fetch once, no polling). Returns { data, error, isLoading }.
export function usePoll(key, fetcher, ms = 3000) {
  return useQuery({
    queryKey: Array.isArray(key) ? key : [key],
    queryFn: fetcher,
    refetchInterval: ms || false,
    refetchOnWindowFocus: false,
    staleTime: ms ? ms / 2 : 0,
    retry: 1,
  });
}
