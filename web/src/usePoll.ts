import { useCallback, useEffect, useState } from "react";

/** Fetch now and then every `ms`. Returns data, error, and a manual refresh. */
export function usePoll<T>(fn: () => Promise<T>, ms: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(() => {
    fn()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, deps);

  useEffect(() => {
    run();
    if (!ms) return;
    const id = setInterval(run, ms);
    return () => clearInterval(id);
  }, [run, ms]);

  return { data, error, refresh: run };
}
