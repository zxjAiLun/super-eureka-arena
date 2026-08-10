// Interactive browser Stockfish analysis (P4.11 commit 2 + closure repair).
//
// Search lifecycle — the stop/bestmove barrier:
//
//   uciok -> isready -> readyok      init: isready is used ONLY at startup,
//   -> position fen <pending>          as the liveness barrier before the
//   -> go infinite                     first search.
//
//   [search A running]
//   FEN change -> pending = {gen B, fen}
//             -> stop                 UCI "stop" does NOT drain the old
//             -> [all info dropped]     search: the engine can print
//   bestmove  <- (old search ended)     readyok/info out of order.  The only
//             -> position fen B         reliable end marker for "go infinite"
//             -> go infinite            is the old search's BESTMOVE — info
//                                      arriving before it belongs to the
//                                      previous FEN and is discarded.
//
// Every FEN request carries a fresh GENERATION; "pending" is the latest
// request and "requested" is the position actually sent.  Rapid navigation
// (A -> B -> C -> A) only replaces `pending`; the in-flight stop ends with
// one bestmove which restarts the LATEST pending generation.  Stale output
// from an earlier generation can never land in the current panel.
//
// Failures surface a visible "error" state: no "uci" within 10s (init), no
// "readyok" after the init barrier, no "bestmove" within 10s of a stop
// (stalled engine).  Late answers self-heal by restarting the latest
// pending generation.
//
// Session lifecycle: disabling (enabled -> false, e.g. a Live match flipping
// to COMPLETED) or unmounting TERMINATES the worker — a running "go
// infinite" must never outlive the session that started it.  All refs are
// reset, so a later re-enable starts a clean engine from scratch.

import { useCallback, useEffect, useRef, useState } from "react";

import {
  disposeStockfishWorker,
  getStockfishWorker,
  parseInfoLine,
} from "./StockfishWorker";

const INIT_TIMEOUT_MS = 10000;
const INIT_BARRIER_TIMEOUT_MS = 10000;
const STOP_TIMEOUT_MS = 10000;

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
  const searchingRef = useRef(false); // a search is running (go sent)
  const stoppingRef = useRef(false); // stop sent, waiting for bestmove
  const initTimerRef = useRef(null);
  const barrierTimerRef = useRef(null);
  const stopTimerRef = useRef(null);

  const serve = useCallback((pending) => {
    const worker = workerRef.current;
    if (!worker) return;
    requestedRef.current = pending;
    searchingRef.current = true;
    worker.postMessage(`position fen ${pending.fen}`);
    worker.postMessage("go infinite");
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
        uciReadyRef.current = true;
        if (initTimerRef.current) {
          clearTimeout(initTimerRef.current);
          initTimerRef.current = null;
        }
        // Init barrier: isready is only ever sent here.
        worker.postMessage("isready");
        if (barrierTimerRef.current) clearTimeout(barrierTimerRef.current);
        barrierTimerRef.current = setTimeout(() => {
          setState((s) => ({ ...s, status: "error", error: "engine unavailable" }));
        }, INIT_BARRIER_TIMEOUT_MS);
        setState((s) => ({ ...s, status: "ready", error: null }));
      } else if (line === "readyok") {
        if (barrierTimerRef.current) {
          clearTimeout(barrierTimerRef.current);
          barrierTimerRef.current = null;
        }
        // Init barrier passed: start the first pending FEN (if any).  A
        // readyok is NEVER a search barrier, so a late one after a timeout
        // only self-heals when no search is running.
        const pending = pendingRef.current;
        if (pending && !searchingRef.current && !stoppingRef.current) {
          setState((s) => ({ ...s, status: "searching", error: null }));
          serve(pending);
        }
      } else if (line.startsWith("info")) {
        // While stopping, ALL output belongs to the old search — drop it.
        if (stoppingRef.current) return;
        const pending = pendingRef.current;
        const requested = requestedRef.current;
        if (!pending || !requested || requested.gen !== pending.gen) return;
        const whiteToMove = requested.fen.split(" ")[1] !== "b";
        const parsed = parseInfoLine(line, whiteToMove);
        if (parsed.score_cp != null || parsed.mate != null) {
          setState((s) => ({ ...s, result: { ...(s.result || {}), ...parsed } }));
        }
      } else if (line.startsWith("bestmove")) {
        // The old search has REALLY ended: with "go infinite" a bestmove
        // only follows our stop, and it is the last line the engine emits
        // for that search.  This is the ownership barrier.
        if (stoppingRef.current) {
          stoppingRef.current = false;
          if (stopTimerRef.current) {
            clearTimeout(stopTimerRef.current);
            stopTimerRef.current = null;
          }
        }
        searchingRef.current = false;
        const pending = pendingRef.current;
        if (pending && (!requestedRef.current || requestedRef.current.gen !== pending.gen)) {
          // Serve the latest pending generation — this also self-heals a
          // stop-timeout when a very late bestmove finally arrives.
          setState((s) => ({ ...s, status: "searching", error: null }));
          serve(pending);
        }
      } else if (line.startsWith("Stockfish ")) {
        setState((s) => (s.version ? s : { ...s, version: line.trim() }));
      }
    };
    const onError = () => {
      setState((s) => ({ ...s, status: "error", error: "engine unavailable" }));
    };
    // Init timeout: a worker that loads but never answers "uci" must not
    // leave the page stuck on "searching".  Tracked via uciReadyRef, not
    // the UI status (which the FEN effect sets to "searching" at once).
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
      if (stopTimerRef.current) clearTimeout(stopTimerRef.current);
      worker.removeEventListener("message", onMessage);
      worker.removeEventListener("error", onError);
      // Disabling (e.g. a Live match flipping to COMPLETED) or unmount ends
      // the whole analysis session: terminate the singleton worker instead
      // of letting "go infinite" keep burning a CPU core with no listener.
      // No stop/bestmove dance — terminate() is the session boundary, and
      // all refs are reset so a later re-enable starts a clean engine.
      disposeStockfishWorker(worker);
      workerRef.current = null;
      uciReadyRef.current = false;
      searchingRef.current = false;
      stoppingRef.current = false;
      requestedRef.current = null;
      pendingRef.current = null;
    };
  }, [enabled, basePath, workerUrl, serve]);

  // A FEN change starts exactly one search for that position: bump the
  // generation, clear the visible result, and either stop the active search
  // (its bestmove picks up the latest pending) or serve directly when idle.
  // Rapid navigation only replaces `pending` — one stop, one bestmove, one
  // restart.
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
    if (!worker || !uciReadyRef.current) return undefined; // served at init readyok
    if (stoppingRef.current) return undefined; // in-flight stop will restart
    if (searchingRef.current) {
      stoppingRef.current = true;
      worker.postMessage("stop");
      if (stopTimerRef.current) clearTimeout(stopTimerRef.current);
      stopTimerRef.current = setTimeout(() => {
        if (stoppingRef.current) {
          setState((s) => ({ ...s, status: "error", error: "engine unavailable" }));
        }
      }, STOP_TIMEOUT_MS);
    } else {
      serve(pendingRef.current);
    }
    return undefined;
  }, [fen, enabled, serve]);

  return {
    status: state.status,
    error: state.error,
    version: state.version,
    ...(state.result || {}),
  };
}
