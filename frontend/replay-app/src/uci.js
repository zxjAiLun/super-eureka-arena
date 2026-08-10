import { Chess } from "chess.js";

// Convert a UCI principal variation (["e2e4", "e7e5", ...]) to SAN, replayed
// from the given FEN via chess.js.  Converts AS MUCH as possible: a
// truncated or illegal tail never hides the valid prefix (P4.12 commit 1).
export function uciPvToSan(fen, pv) {
  if (!fen || !pv || pv.length === 0) return [];
  const board = new Chess(fen);
  const out = [];
  for (const uci of pv) {
    try {
      // Object form: a bare UCI string is not SAN and chess.js would throw.
      const move = board.move(
        {
          from: uci.slice(0, 2),
          to: uci.slice(2, 4),
          promotion: uci[4] || undefined,
        },
        { strict: true }
      );
      out.push(move.san);
    } catch (e) {
      break;
    }
  }
  return out;
}
