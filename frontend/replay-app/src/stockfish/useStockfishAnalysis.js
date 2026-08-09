// Interactive browser Stockfish analysis (P4.11 commit 2 + closure repair).
//
// Search lifecycle: every FEN request carries a fresh GENERATION.  The
// request object is { gen, fen }; "pending" is the latest request and
// "requested" is the position actually sent to the engine.  A new request
// immediately clears the previous result, posts "stop" and an "isready"
// barrier, and only when "readyok" arrives starts "go infinite" for the
// pending FEN — but only if its generation was not yet served.  Because
// matching is by generation (not by FEN string), a fast A -> B -> A
// navigation re-serves the second A as a brand-new request, and stale
// output from an earlier generation is always dropped.
//
// Failures surface a visible "error" state: a worker that never answers
// "uci" (init timeout), a barrier that never gets its "readyok" (stalled
// engine), a load failure and runtime worker errors.  Timeouts track
// protocol progress directly (uciReadyRef, barrier timer), never UI status.

import { useEffect, useRef, useState } from "react";

import { getStockfishWorker, parseInfoLine } from "./StockfishWorker";

const INIT_TIMEOUT_MS = 10000;
const BARRIER_TIMEOUT_MS = 10000;

export function useStockfishAnalysis({ fen, enabled, basePath, workerUrl }) {
  const [state, setState] = useState({
    status: "idle", // idle | ready | searching | error
    result: null,
    error: null,
    version: null,
  });
  const workerRef = useRef(null);
  const genRef = useRef(0);
  const pendingRef = useRef(null); // { gen, fen } — latest request
  const requestedRef = useRef(null); // { gen, fen } — position actually sent
  const uciReadyRef = useRef(false);
  const initTimerRef = useRef(null);
  const barrierTimerRef = useRef(null);

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
        uciReadyRef.current = true;
        if (initTimerRef.current) {
          clearTimeout(initTimerRef.current);
          initTimerRef.current = null;
        }
        setState((s) => ({ ...s, status: "ready", error: null }));
      } else if (line === "readyok") {
        if (barrierTimerRef.current) {
          clearTimeout(barrierTimerRef.current);
          barrierTimerRef.current = null;
        }
        const pending = pendingRef.current;
        if (
          pending &&
          (!requestedRef.current ||
            requestedRef.current.gen !== pending.gen)
        ) {
          // This generation was never served (possibly a very late answer
          // after a timeout): serve it and resume a visible search.
          requestedRef.current = pending;
          setState((s) => ({ ...s, status: "searching", error: null }));
          worker.postMessage(`position fen ${pending.fen}`);
          worker.postMessage("go infinite");
        }
      } else if (line.startsWith("info")) {
        // Ownership: only the current generation's lines are accepted.
        const pending = pendingRef.current;
        const requested = requestedRef.current;
        if (!pending || !requested || requested.gen !== pending.gen) return;
        const whiteToMove = requested.fen.split(" ")[1] !== "b";
        const parsed = parseInfoLine(line, whiteToMove);
        if (parsed.score_cp != null || parsed.mate != null) {
          setState((s) => ({ ...s, result: { ...(s.result || {}), ...parsed } }));
        }
      } else if (line.startsWith("bestmove")) {
        const pending = pendingRef.current;
        const requested = requestedRef.current;
        if (!pending || !requested || requested.gen !== pending.gen) return;
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
    // Init timeout: a worker that loads but never answers "uci" must not
    // leave the page stuck on "searching" forever.  Tracked via uciReadyRef,
    // not the UI status (which the FEN effect sets to "searching" at once).
    initTimerRef.current = setTimeout(() => {
      if (!uciReadyRef.current) {
        setState((s) => ({ ...s, status: "error", error: "engine unavailable" }));
      }
    }, INIT_TIMEOUT_MS);
    worker.addEventListener("message", onMessage);
    worker.addEventListener("error", onError);
    return () => {
      if (initTimerRef.current) clearTimeout(initTimerRef.current);
      if (barrierTimerRef.current) clearTimeout(barrierTimerRef.current);
      worker.removeEventListener("message", onMessage);
      worker.removeEventListener("error", onError);
    };
  }, [enabled, basePath, workerUrl]);

  // A FEN change starts exactly one search for that position.  Every change
  // (even back to a previously served FEN) is a new generation, so the
  // readyok barrier re-serves it.
  useEffect(() => {
    if (!enabled || !fen) return undefined;
    genRef.current += 1;
    pendingRef.current = { gen: genRef.current, fen };
    setState((s) => ({
      ...s, // keep the captured engine version across navigations
      status: "searching",
      result: null,
      error: null,
    }));
    const worker = workerRef.current;
    if (!worker) return undefined; // barrier is queued once the worker exists
    worker.postMessage("stop");
    worker.postMessage("isready");
    if (barrierTimerRef.current) clearTimeout(barrierTimerRef.current);
    barrierTimerRef.current = setTimeout(() => {
      // The engine never answered the isready barrier: it is stalled.
      setState((s) => ({ ...s, status: "error", error: "engine unavailable" }));
    }, BARRIER_TIMEOUT_MS);
    return undefined;
  }, [fen, enabled]);

  return {
    status: state.status,
    error: state.error,
    version: state.version,
    ...(state.result || {}),
  };
}
