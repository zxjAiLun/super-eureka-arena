// Interactive browser Stockfish analysis (P4.11 commit 2 + repair).
//
// Search lifecycle: a new FEN increments a generation, immediately clears the
// previous result, posts "stop" and an "isready" barrier, and only when
// "readyok" arrives starts "go infinite" for the LATEST requested FEN.  The
// barrier separates stale output from the old position, and info/bestmove are
// accepted only when they belong to the current generation — so a fast
// A -> B navigation can never leak A's late lines into B.  "go infinite"
// keeps deepening the current position until the user navigates again (no
// repeated depth-18 restarts).
//
// Worker failures (load error, runtime error, init timeout) surface a visible
// "error" state instead of failing silently.

import { useCallback, useEffect, useRef, useState } from "react";

import { getStockfishWorker, parseInfoLine } from "./StockfishWorker";

export function useStockfishAnalysis({ fen, enabled, basePath, workerUrl }) {
  const [state, setState] = useState({
    status: "idle", // idle | ready | searching | error
    result: null,
    error: null,
    version: null,
  });
  const workerRef = useRef(null);
  const pendingFenRef = useRef(null);
  const requestedFenRef = useRef(null);
  const servedFenRef = useRef(null);

  const startFen = useCallback((target, worker) => {
    pendingFenRef.current = target;
    worker.postMessage("stop");
    worker.postMessage("isready");
  }, []);

  // Worker bring-up and message stream.
  useEffect(() => {
    if (!enabled) return undefined;
    const worker = getStockfishWorker(basePath, workerUrl);
    if (!worker) {
      setState({ status: "error", result: null, error: "engine unavailable" });
      return undefined;
    }
    workerRef.current = worker;
    const onMessage = (e) => {
      const line = e.data;
      if (typeof line !== "string") return;
      if (line === "uciok") {
        setState((s) => ({ ...s, status: "ready", error: null }));
        // The FEN effect already queued its own stop+isready barrier (and
        // the engine answers it after init), so nothing to start here — a
        // second barrier would yield two readyoks and two "go" commands.
      } else if (line === "readyok") {
        const fen = pendingFenRef.current;
        if (fen && servedFenRef.current !== fen) {
          servedFenRef.current = fen;
          requestedFenRef.current = fen;
          worker.postMessage(`position fen ${fen}`);
          worker.postMessage("go infinite");
        }
      } else if (line.startsWith("info")) {
        // Ownership: only the current generation's lines are accepted.
        if (requestedFenRef.current !== pendingFenRef.current) return;
        const whiteToMove = requestedFenRef.current?.split(" ")[1] !== "b";
        const parsed = parseInfoLine(line, whiteToMove);
        if (parsed.score_cp != null || parsed.mate != null) {
          setState((s) => ({ ...s, result: { ...(s.result || {}), ...parsed } }));
        }
      } else if (line.startsWith("bestmove")) {
        if (requestedFenRef.current !== pendingFenRef.current) return;
        const best = line.split(" ")[1];
        setState((s) => ({
          ...s,
          result: { ...(s.result || {}), best_move: best },
        }));
      } else if (line.startsWith("Stockfish ")) {
        setState((s) => (s.version ? s : { ...s, version: line.trim() }));
      }
    };
    const onError = () => {
      setState((s) => ({ ...s, status: "error", error: "engine unavailable" }));
    };
    const timer = setTimeout(() => {
      setState((s) =>
        s.status === "idle"
          ? { ...s, status: "error", error: "engine unavailable" }
          : s
      );
    }, 10000);
    worker.addEventListener("message", onMessage);
    worker.addEventListener("error", onError);
    return () => {
      clearTimeout(timer);
      worker.removeEventListener("message", onMessage);
      worker.removeEventListener("error", onError);
    };
  }, [enabled, basePath, workerUrl, startFen]);

  // A FEN change starts exactly one search for that position.
  useEffect(() => {
    if (!enabled || !fen) return undefined;
    pendingFenRef.current = fen;
    setState({ status: "searching", result: null, error: null });
    const worker = workerRef.current;
    if (!worker) return undefined; // starts after uciok
    startFen(fen, worker);
    return undefined;
  }, [fen, enabled, startFen]);

  return {
    status: state.status,
    error: state.error,
    version: state.version,
    ...(state.result || {}),
  };
}
