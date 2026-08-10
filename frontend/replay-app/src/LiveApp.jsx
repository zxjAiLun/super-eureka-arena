import { useEffect, useRef, useState } from "react";
import { Chessboard } from "react-chessboard";
import { useStockfishAnalysis } from "./stockfish/useStockfishAnalysis";
import { shareForUi } from "./eval";
import AnalysisPanel, {
  DEFAULT_ANALYSIS_DEPTH,
} from "./AnalysisPanel";

const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

// P4.11 commit 4: match-score derived performance delta (Engine A vs B),
// same formula and +/-800 clamp as the server display helper.
function matchEloDelta(wins, draws, losses) {
  const played = wins + draws + losses;
  if (!played) return null;
  const s = (wins + 0.5 * draws) / played;
  if (s >= 1) return 800;
  if (s <= 0) return -800;
  return Math.round(400 * Math.log10(s / (1 - s)));
}

function eloDeltaText(delta) {
  if (delta == null) return "—";
  return delta > 0 ? `+${delta}` : `${delta}`;
}

// Display label: 100% / 0% scores are mathematical +∞ / −∞, so they render
// as the bounds ≥+800 / ≤-800, never as an exact value.
function eloDeltaLabel(wins, draws, losses) {
  const played = wins + draws + losses;
  if (!played) return "—";
  const s = (wins + 0.5 * draws) / played;
  if (s >= 1) return "≥+800";
  if (s <= 0) return "≤-800";
  return eloDeltaText(matchEloDelta(wins, draws, losses));
}

const TC_LABELS = {
  bullet_1_0: "1+0",
  blitz_3_2: "3+2",
  blitz_10_01: "10s+0.1s",
  rapid_5_3: "5+3",
};

function useLive({ basePath, tournamentId }) {
  const [payload, setPayload] = useState(null);
  const [phase, setPhase] = useState("loading"); // loading | idle | live | completed | error

  useEffect(() => {
    let cancelled = false;
    const url = `${basePath}/public-api/v1/live${
      tournamentId ? `?tournament_id=${tournamentId}` : ""
    }`;
    const poll = async () => {
      try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`live endpoint ${res.status}`);
        const data = await res.json();
        if (cancelled) return;
        setPayload(data);
        setPhase(
          data.status === "completed"
            ? "completed"
            : data.status === "live"
              ? "live"
              : "idle"
        );
      } catch (e) {
        if (!cancelled) setPhase("error");
      }
    };
    poll();
    const id = setInterval(poll, 1500);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [basePath, tournamentId]);

  return { phase, payload };
}

function Badges({ data }) {
  const items = [
    data.name && `Match: ${data.name}`,
    data.pair_index != null && `Pair ${data.pair_index + 1}/${data.pairs_total}`,
    data.game_in_pair != null && `Game ${data.game_in_pair}/${data.games_total}`,
    data.time_control && TC_LABELS[data.time_control] || data.time_control,
  ].filter(Boolean);
  return (
    <div className="badges">
      {items.map((b) => (
        <span className="badge" key={b}>
          {b}
        </span>
      ))}
      {data.last_result && (
        <span className="badge badge-result">Last: {data.last_result}</span>
      )}
    </div>
  );
}

export default function LiveApp({ tournamentId, basePath }) {
  const { phase, payload } = useLive({ basePath, tournamentId });
  // P4.11 commit 3: the SAME browser Stockfish core as the Replay page.
  // Only the REAL telemetry current_fen enables it (opening-fen fallback or
  // missing telemetry fails closed).  Called unconditionally — the hook must
  // never move below an early return.  P4.12: OFF by default, no worker
  // until the user starts analysis.
  const [analysisEnabled, setAnalysisEnabled] = useState(false);
  const [analysisDepth, setAnalysisDepth] = useState(DEFAULT_ANALYSIS_DEPTH);
  const liveFen = payload?.current_fen || null;
  const browserEnabled =
    phase === "live" && Boolean(liveFen) && analysisEnabled;
  const browser = useStockfishAnalysis({
    fen: liveFen,
    enabled: browserEnabled,
    basePath,
    depth: analysisDepth,
  });
  const replayRef = useRef(null);
  // Clock countdown: anchored at the moment the last payload arrived; only
  // the side to move keeps ticking between 1.5s polls.
  const [receivedAt, setReceivedAt] = useState(() => Date.now());
  const [renderNow, setRenderNow] = useState(() => Date.now());

  // Hooks must be called unconditionally (before any early return).
  useEffect(() => {
    setReceivedAt(Date.now());
    const id = setInterval(() => setRenderNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [phase]);

  // Re-anchor the countdown whenever a new payload arrives.
  useEffect(() => {
    if (phase === "live") setReceivedAt(Date.now());
  }, [phase, payload]);

  if (phase === "loading") {
    return <div className="demo-message">Connecting to live status…</div>;
  }
  if (phase === "error") {
    return (
      <div className="demo-message demo-error">Live status unavailable.</div>
    );
  }
  if (phase === "idle") {
    return (
      <div className="demo-message">
        No match is currently running. Start one from the admin panel, or{" "}
        <a href={`${basePath}/matches/`} className="action-link">
          browse completed matches
        </a>
        .
      </div>
    );
  }
  if (phase === "completed") {
    return (
      <div className="demo-message">
        <p>
          <strong>{payload.name}</strong> finished.
          {payload.candidate_wins != null &&
            ` Final: ${payload.candidate_wins}-${payload.draws}-${payload.candidate_losses} W-D-L · Δ Elo (A−B) ${eloDeltaLabel(
              payload.candidate_wins,
              payload.draws,
              payload.candidate_losses
            )}.`}
        </p>
        <p>
          {payload.match_url && (
            <a href={payload.match_url} className="action-link">
              Open completed match replay
            </a>
          )}
        </p>
      </div>
    );
  }

  // P4.11: the board shows the REAL position from the engine protocol stream
  // when telemetry is available, falling back to the pair's opening FEN.
  const fen = payload.current_fen || payload.opening_fen || START_FEN;
  const inProgress =
    payload.state === "game_running" || payload.state === "pending";
  const white = payload.white;
  const black = payload.black;
  // Fail closed: without an authoritative game boundary the backend sends no
  // white/black sides — never guess engine A/B into the colors here.
  const sidesKnown = Boolean(white && black);
  const hasTelemetry = Boolean(payload.current_fen);
  const activeIsWhite = payload.side_to_move === "w";
  const hasBrowserScore = browser.score_cp != null || browser.mate != null;

  const clockOf = (side, isActive) => {
    if (!side || side.clock_ms == null) return null;
    const elapsed = Math.max(0, renderNow - receivedAt);
    const remaining = isActive ? Math.max(0, side.clock_ms - elapsed) : side.clock_ms;
    const total = Math.round(remaining / 1000);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  };

  const evalText = (side) => {
    if (!side) return null;
    if (side.mate != null && side.mate !== 0) {
      return side.mate > 0 ? `M${side.mate}` : `-M${Math.abs(side.mate)}`;
    }
    if (side.eval_cp == null) return null;
    const v = side.eval_cp / 100;
    return (v > 0 ? "+" : "") + v.toFixed(2);
  };

  const enginePanel = (side) => {
    if (!side) return null;
    return (
      <div className="live-engine" key={side.label}>
        <div className="live-engine-line">
          <span className="live-engine-eval">{evalText(side) ?? "—"}</span>
          <span className="live-engine-name">{side.label}</span>
          {side.depth != null && <span className="live-engine-meta">d{side.depth}</span>}
          {side.nps != null && (
            <span className="live-engine-meta">
              {(side.nps / 1e6).toFixed(1)}Mn
            </span>
          )}
        </div>
        {side.pv && side.pv.length > 0 && (
          <div className="live-engine-pv">
            PV: {side.pv.map((u) => u).join(" ")}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="replay" ref={replayRef}>
      <div className="replay-board-col">
        <div className="player-card top">
          <span className="color-dot black" />
          <span className="player-name">{black ? black.label : "—"}</span>
          {clockOf(black, hasTelemetry && !activeIsWhite) && (
            <span className="live-clock">{clockOf(black, hasTelemetry && !activeIsWhite)}</span>
          )}
        </div>
        <div className="board-stage">
          {/* P4.11 commit 3: the eval bar reflects ONLY the browser
              Stockfish evaluation — never a match engine's self-eval.  The
              slot is always reserved so the board never moves. */}
          <div
            className={
              "eval-bar" +
              (hasBrowserScore && analysisEnabled ? "" : " eval-bar-empty")
            }
            aria-label="Evaluation"
          >
            {hasBrowserScore && analysisEnabled && (
              <div
                className="eval-bar-white"
                style={{ height: `${shareForUi(browser) * 100}%` }}
              />
            )}
          </div>
          <div className="board-wrap" data-fen={fen}>
            <Chessboard options={{ position: fen, allowDragging: false }} />
          </div>
        </div>
        <div className="player-card bottom">
          <span className="player-name">{white ? white.label : "—"}</span>
          {clockOf(white, hasTelemetry && activeIsWhite) && (
            <span className="live-clock">{clockOf(white, hasTelemetry && activeIsWhite)}</span>
          )}
          <span className="color-dot white" />
        </div>
        {inProgress && (
          <div className="demo-message">Game in progress…</div>
        )}
      </div>
      <div className="replay-side-col">
        <Badges data={payload} />
        {phase === "live" && payload.candidate_wins != null && (
          <div className="live-meta">
            Verified W-D-L {payload.candidate_wins}-{payload.draws}-
            {payload.candidate_losses} · Δ Elo (A−B){" "}
            {eloDeltaLabel(
              payload.candidate_wins,
              payload.draws,
              payload.candidate_losses
            )}
          </div>
        )}
        <AnalysisPanel
          browser={browser}
          fen={liveFen}
          enabled={browserEnabled}
          depth={analysisDepth}
          onDepthChange={setAnalysisDepth}
          onStart={() => setAnalysisEnabled(true)}
          onStop={() => setAnalysisEnabled(false)}
        />
        {hasTelemetry && (
          <>
            <div className="live-meta">
              {payload.side_to_move === "w" ? "White" : "Black"} to move
              {payload.last_move ? ` · last ${payload.last_move}` : ""}
              {payload.ply != null ? ` · ply ${payload.ply}` : ""}
              {payload.telemetry_age_s != null
                ? ` · stream ${payload.telemetry_age_s}s old`
                : ""}
            </div>
            {enginePanel(white)}
            {enginePanel(black)}
          </>
        )}
        {hasTelemetry && !sidesKnown && (
          <p className="demo-note">
            Live position from the engine stream; color assignment unavailable
            until the next authoritative game boundary.
          </p>
        )}
        <p className="demo-note">
          {hasTelemetry
            ? "Live position from the match engine stream. The authoritative result appears once the pair passes verification."
            : "Position shown is the opening of the current pair. The authoritative result appears once the pair passes verification."}
        </p>
      </div>
    </div>
  );
}
