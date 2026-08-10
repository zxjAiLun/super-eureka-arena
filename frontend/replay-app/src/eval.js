// Shared evaluation formatting for the browser Stockfish (Replay + Live).

// Strict winning share for swing classification: null when the evaluation is
// unknown — never treated as an exactly equal position.
export function shareOf(p) {
  if (!p) return null;
  if (p.mate != null) {
    if (p.mate === 0) return null;
    return p.mate > 0 ? 0.98 : 0.02;
  }
  if (p.score_cp == null) return null;
  return Math.min(0.98, Math.max(0.02, 1 / (1 + Math.exp(-p.score_cp / 250))));
}

// UI-safe share: unknown evaluations render as a neutral bar.
export function shareForUi(p) {
  const s = shareOf(p);
  return s == null ? 0.5 : s;
}

// White-perspective score text ("+0.42", "-M3"); null when unknown.
export function formatScoreOf(p) {
  if (!p) return null;
  if (p.mate != null) {
    if (p.mate === 0) return null; // invalid mate score is not an evaluation
    return p.mate > 0 ? `M${p.mate}` : `-M${Math.abs(p.mate)}`;
  }
  if (p.score_cp == null) return null;
  const v = p.score_cp / 100;
  return (v > 0 ? "+" : "") + v.toFixed(2);
}
