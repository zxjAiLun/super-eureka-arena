// Shared browser Stockfish engine (P4.11 commit 2).
//
// stockfish.js compiled to WebAssembly, run in a classic web worker.  The
// worker script and its .wasm are shipped under /static/replay-app/stockfish
// (copied verbatim by Vite from public/).  One singleton worker serves both
// the Replay and the Live pages; searches are per-position (stop + position
// fen + go) and cheap enough for interactive use.

let worker = null;

export function getStockfishWorker(basePath) {
  if (!worker) {
    worker = new Worker(
      `${basePath}/static/replay-app/stockfish/stockfish.wasm.js`
    );
    // No setoption calls: this stockfish.js build pins Threads=1 and
    // Hash=16 (max 16) — an out-of-range setoption can hang the engine.
    worker.postMessage("uci");
  }
  return worker;
}

// White-perspective score from a UCI "info ..." line.  Field names mirror the
// server diagnostics positions (score_cp / best_move) so the shared
// formatScoreOf / shareOf helpers work unchanged.
export function parseInfoLine(line, whiteToMove) {
  const tokens = line.split(" ");
  let score_cp = null;
  let mate = null;
  let depth = null;
  let nodes = null;
  let nps = null;
  let pv = [];
  for (let i = 0; i < tokens.length; i++) {
    const tok = tokens[i];
    if (tok === "score" && i + 2 < tokens.length) {
      if (tokens[i + 1] === "cp") {
        score_cp = parseInt(tokens[i + 2], 10);
      } else if (tokens[i + 1] === "mate") {
        mate = parseInt(tokens[i + 2], 10);
      }
    } else if (tok === "depth" && i + 1 < tokens.length) {
      depth = parseInt(tokens[i + 1], 10);
    } else if (tok === "nodes" && i + 1 < tokens.length) {
      nodes = parseInt(tokens[i + 1], 10);
    } else if (tok === "nps" && i + 1 < tokens.length) {
      nps = parseInt(tokens[i + 1], 10);
    } else if (tok === "pv") {
      pv = tokens.slice(i + 1);
    }
  }
  if (score_cp != null && !whiteToMove) score_cp = -score_cp;
  if (mate != null && !whiteToMove) mate = -mate;
  return { score_cp, mate, depth, nodes, nps, pv };
}
