// Interactive browser Stockfish analysis (P4.11 commit 2).
//
// useStockfishAnalysis({ fen, enabled, basePath }) -> {
//   status,            // idle | ready | searching
//   score_cp, mate,    // White perspective; field names match diagnostics
//   depth, nodes, nps,
//   pv, best_move
// }
//
// Any verified position works; it does NOT depend on the server diagnostics
// artifact.  Navigating to a new ply stops the search, sets the position and
// starts a fresh fixed-depth search.

import { useEffect, useRef, useState } from "react";

import { getStockfishWorker, parseInfoLine } from "./StockfishWorker";

const ANALYSIS_DEPTH = 18;

export function useStockfishAnalysis({ fen, enabled, basePath }) {
  const [status, setStatus] = useState("idle"); // idle | ready | searching
  const [result, setResult] = useState(null);
  const workerRef = useRef(null);
  const resultRef = useRef(null);
  const fenRef = useRef(null);

  // Bring the singleton worker up and subscribe to its stream.
  useEffect(() => {
    if (!enabled) return undefined;
    const worker = getStockfishWorker(basePath);
    workerRef.current = worker;
    const onMessage = (e) => {
      const line = e.data;
      if (typeof line !== "string") return;
      if (line === "uciok" || line === "readyok") {
        setStatus((s) => (s === "idle" ? "ready" : s));
        // A position may have queued while the worker was initializing.
        if (fenRef.current && fenRef.current !== "") {
          worker.postMessage("stop");
          worker.postMessage(`position fen ${fenRef.current}`);
          worker.postMessage(`go depth ${ANALYSIS_DEPTH}`);
        }
      } else if (line.startsWith("info")) {
        const whiteToMove = fenRef.current?.split(" ")[1] !== "b";
        const parsed = parseInfoLine(line, whiteToMove);
        if (parsed.score_cp != null || parsed.mate != null) {
          resultRef.current = { ...parsed };
          setResult(resultRef.current);
        }
      } else if (line.startsWith("bestmove")) {
        const best = line.split(" ")[1];
        setResult((r) => (r ? { ...r, best_move: best } : { best_move: best }));
        setStatus("ready");
      }
    };
    worker.addEventListener("message", onMessage);
    return () => worker.removeEventListener("message", onMessage);
  }, [enabled, basePath]);

  // Re-analyze whenever the position changes.
  useEffect(() => {
    if (!enabled || !fen) return undefined;
    fenRef.current = fen;
    const worker = workerRef.current;
    if (!worker || status === "idle") return undefined;
    setStatus("searching");
    worker.postMessage("stop");
    worker.postMessage(`position fen ${fen}`);
    worker.postMessage(`go depth ${ANALYSIS_DEPTH}`);
    return undefined;
  }, [fen, enabled, status === "ready"]);

  return { status, ...(result || {}) };
}
