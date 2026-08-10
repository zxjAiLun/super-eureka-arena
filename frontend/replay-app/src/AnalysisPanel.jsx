// Shared browser-Stockfish control panel (P4.12 commit 1): identical on the
// Replay and Live pages.  Analysis is OFF by default — no worker is created
// until the user starts it — and the depth is a finite `go depth <N>`.

import { formatScoreOf } from "./eval";
import { uciPvToSan } from "./uci";

export const ANALYSIS_DEPTHS = [16, 18, 20, 22];
export const DEFAULT_ANALYSIS_DEPTH = 18;

export default function AnalysisPanel({
  browser,
  fen,
  enabled,
  depth,
  onDepthChange,
  onStart,
  onStop,
}) {
  if (!enabled) {
    return (
      <div className="analysis-panel">
        <div className="analysis-score">Stockfish analysis</div>
        <div className="analysis-line">Runs locally in this browser</div>
        <div className="analysis-controls">
          <button type="button" onClick={onStart}>
            Start analysis
          </button>
        </div>
      </div>
    );
  }
  const pv = browser.pv ? uciPvToSan(fen, browser.pv) : [];
  return (
    <div className="analysis-panel">
      {browser.status === "error" ? (
        <>
          <div className="analysis-score">
            Stockfish unavailable
            <span className="analysis-engine">Stockfish · browser</span>
          </div>
          <div className="analysis-line">engine failed to start</div>
        </>
      ) : (
        <>
          <div className="analysis-score">
            {formatScoreOf(browser) ?? "…"}
            <span className="analysis-engine">
              {browser.version
                ? `Stockfish ${browser.version.split(" ")[1]} · browser`
                : "Stockfish · browser"}
            </span>
          </div>
          <div className="analysis-line">
            {browser.depth != null && <>d{browser.depth} </>}
            {browser.nps != null && <>· {(browser.nps / 1e6).toFixed(1)}M </>}
            {browser.status === "searching" && <>· searching…</>}
          </div>
          {pv.length > 0 && (
            <div className="analysis-pv">PV: {pv.join(" ")}</div>
          )}
        </>
      )}
      <div className="analysis-controls">
        <label className="analysis-depth">
          Depth:{" "}
          <select
            value={depth}
            onChange={(e) => onDepthChange(Number(e.target.value))}
          >
            {ANALYSIS_DEPTHS.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </label>
        <button type="button" onClick={onStop}>
          Stop analysis
        </button>
      </div>
    </div>
  );
}
