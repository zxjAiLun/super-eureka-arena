import { useEffect, useRef, useState } from "react";
import { Chessboard } from "react-chessboard";
import { useStockfishAnalysis } from "./stockfish/useStockfishAnalysis";
import { formatScoreOf, shareForUi } from "./eval";

const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

const TC_LABELS = {
  bullet_1_0: "1+0",
  blitz_3_2: "3+2",
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
  // never move below an early return.
  const liveFen = payload?.current_fen || null;
  const browserEnabled = phase === "live" && Boolean(liveFen);
  const browser = useStockfishAnalysis({
    fen: liveFen,
    enabled: browserEnabled,
    basePath,
  });
  const [boardSize, setBoardSize] = useState(480);
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

  // Hooks must be called unconditionally (before any early return).  Size the
  // board to fit the available height; no-op while there is no board.
  useEffect(() => {
    const el = replayRef.current;
    if (!el) return undefined;
    const compute = () => {
      const w = el.clientWidth;
      const h = el.clientHeight;
      setBoardSize(Math.max(220, Math.min((w - 20) / 2 - 10, h - 170)));
    };
    compute();
    const ro = new ResizeObserver(compute);
    ro.observe(el);
    return () => ro.disconnect();
  }, [phase]);

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
            ` Score: ${payload.candidate_wins}/${payload.candidate_losses}/${payload.draws} (W/L/D).`}
        </p>
        <p>
          <a href={payload.match_url} className="action-link">
            Open completed match replay
          </a>
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
        <div className="board-wrap" data-fen={fen} style={{ width: boardSize }}>
          <Chessboard options={{ position: fen, allowDragging: false }} />
          {/* P4.11 commit 3: the eval bar reflects ONLY the browser
              Stockfish evaluation — never a match engine's self-eval. */}
          {hasBrowserScore && (
            <div className="eval-bar" aria-label="Evaluation">
              <div
                className="eval-bar-white"
                style={{ height: `${shareForUi(browser) * 100}%` }}
              />
            </div>
          )}
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
        {browserEnabled && (
          <div className="analysis-panel">
            {browser.status === "error" ? (
              <>
                <div className="analysis-score">
                  Stockfish unavailable
                  <span className="analysis-engine">
                    Stockfish · browser
                  </span>
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
                  {browser.nps != null && (
                    <>· {(browser.nps / 1e6).toFixed(1)}M </>
                  )}
                  {browser.status === "searching" && <>· searching…</>}
                </div>
                {browser.pv && browser.pv.length > 0 && (
                  <div className="analysis-pv">PV: {browser.pv.join(" ")}</div>
                )}
              </>
            )}
          </div>
        )}
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
