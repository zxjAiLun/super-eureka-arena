import { useEffect, useMemo, useRef, useState } from "react";
import { Chessboard } from "react-chessboard";
import { Chess } from "chess.js";

const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

// Tournaments start from a non-initial FEN when an opening book is used, and
// chess.js loadPgn() honors that [FEN] header.  Navigation must therefore
// replay from the same start position, not from the standard array.
function pgnStartFen(pgn) {
  const m = pgn.match(/^\[FEN "([^"]+)"\]/m);
  return m ? m[1] : null;
}

// Friendly time-control labels for the badge (API returns the config key).
const TC_LABELS = {
  bullet_1_0: "1+0",
  blitz_3_2: "3+2",
  rapid_5_3: "5+3",
};

// P4.9: Arena swing classification (winning-share drop of the mover).
const INACCURACY = 0.1;
const MISTAKE = 0.2;
const BLUNDER = 0.35;

function formatScoreOf(p) {
  if (!p) return null;
  if (p.mate != null) return p.mate > 0 ? `M${p.mate}` : `-M${Math.abs(p.mate)}`;
  if (p.score_cp == null) return null;
  const v = p.score_cp / 100;
  return (v > 0 ? "+" : "") + v.toFixed(2);
}

function whiteShareOf(p) {
  if (p.mate != null) return p.mate > 0 ? 0.98 : 0.02;
  if (p.score_cp == null) return 0.5;
  return Math.min(0.98, Math.max(0.02, 1 / (1 + Math.exp(-p.score_cp / 250))));
}

function moveMark(loss) {
  if (loss >= BLUNDER) return "??";
  if (loss >= MISTAKE) return "?";
  if (loss >= INACCURACY) return "?!";
  return "";
}

function useReplay({ gameId, tournamentId, basePath }) {
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState(null);
  const [moves, setMoves] = useState([]);
  const [meta, setMeta] = useState(null);
  const [pgn, setPgn] = useState("");
  const [startFen, setStartFen] = useState(START_FEN);
  const [analysis, setAnalysis] = useState(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const matchRes = await fetch(
          `${basePath}/public-api/v1/matches/${tournamentId}`
        );
        if (!matchRes.ok) {
          throw new Error("failed to load game data");
        }
        const match = await matchRes.json();
        const game = match.games.find((g) => g.id === gameId);
        if (!game) {
          throw new Error("game not found in match");
        }
        const pgnRes = await fetch(
          `${basePath}/public-api/v1/games/${gameId}/pgn`
        );
        if (!pgnRes.ok) {
          throw new Error("failed to load game data");
        }
        const pgnText = await pgnRes.text();
        const chess = new Chess();
        chess.loadPgn(pgnText);
        const ms = chess.history({ verbose: true });
        if (cancelled) return;
        setMoves(ms);
        setPgn(pgnText);
        setStartFen(pgnStartFen(pgnText) || START_FEN);
        setMeta({ game, timeControl: match.time_control, matchName: match.name });
        // Analysis is optional: only fetch when the match detail says the game
        // has an artifact, so unanalyzed games never trigger a 404.
        if (game.analyzed) {
          const analysisRes = await fetch(
            `${basePath}/public-api/v1/games/${gameId}/analysis`
          );
          if (analysisRes.ok && !cancelled) {
            setAnalysis(await analysisRes.json());
          }
        }
        setStatus("ready");
      } catch (e) {
        if (!cancelled) {
          setStatus("error");
          setError(e.message);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [gameId, tournamentId, basePath]);

  return { status, error, moves, meta, pgn, startFen, analysis };
}

export default function App({ gameId, tournamentId, basePath, pairIndex }) {
  const { status, error, moves, meta, pgn, startFen, analysis } = useReplay({
    gameId,
    tournamentId,
    basePath,
  });
  const [ply, setPly] = useState(0);
  const [playing, setPlaying] = useState(false);
  const replayRef = useRef(null);
  const [boardSize, setBoardSize] = useState(480);

  useEffect(() => {
    setPly(0);
    setPlaying(false);
  }, [status]);

  // Size the board to fit the available height (and the left half of the
  // width) so the replay never needs the page to scroll.
  useEffect(() => {
    if (status !== "ready") return undefined;
    const el = replayRef.current;
    if (!el) return undefined;
    const compute = () => {
      const w = el.clientWidth;
      const h = el.clientHeight;
      const size = Math.max(220, Math.min((w - 20) / 2 - 10, h - 170));
      setBoardSize(size);
    };
    compute();
    const ro = new ResizeObserver(compute);
    ro.observe(el);
    return () => ro.disconnect();
  }, [status]);

  const fen = useMemo(() => {
    if (moves.length === 0) return startFen;
    const c = new Chess(startFen);
    for (let i = 0; i < ply; i++) {
      // Apply by source/target squares (verbose object), not by SAN:
      // re-parsing SAN can hit disambiguation errors on real tournament
      // PGNs ("Invalid move: Bxc6").
      const m = moves[i];
      c.move({ from: m.from, to: m.to, promotion: m.promotion });
    }
    return c.fen();
  }, [moves, ply, startFen]);

  // Autoplay: advance one ply on a timer; stop at the last move.
  useEffect(() => {
    if (!playing || status !== "ready") return undefined;
    const id = setInterval(() => {
      setPly((p) => (p >= moves.length ? (setPlaying(false), p) : p + 1));
    }, 600);
    return () => clearInterval(id);
  }, [playing, status, moves.length]);

  const step = (delta) =>
    setPly((p) => Math.min(moves.length, Math.max(0, p + delta)));

  const togglePlay = () => {
    if (playing) {
      setPlaying(false);
    } else {
      setPly(0);
      setPlaying(true);
    }
  };

  useEffect(() => {
    if (status !== "ready") return undefined;
    const handler = (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowDown") {
        setPly((p) => Math.min(moves.length, p + 1));
      } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        setPly((p) => Math.max(0, p - 1));
      } else if (e.key === "Home") {
        setPly(0);
      } else if (e.key === "End") {
        setPly(moves.length);
      } else if (e.key === " ") {
        e.preventDefault();
        togglePlay();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [status, moves.length, playing]);

  const onWheel = (e) => {
    // Wheel steps moves only outside the move list, so the move list can
    // still be scrolled normally; never preventDefault, so the page scroll
    // is never blocked.
    if (e.target && e.target.closest && e.target.closest(".moves-list")) {
      return;
    }
    step(e.deltaY > 0 ? 1 : -1);
  };

  // Keep the active move visible while navigating a long game.
  useEffect(() => {
    if (status !== "ready") return undefined;
    const active = document.querySelector(".move.active");
    if (active) {
      active.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    return undefined;
  }, [ply, status]);

  // P4.9b/c: per-move winning-share swing from the mover's perspective.
  const swings = useMemo(() => {
    if (!analysis?.positions?.length) return [];
    const positions = analysis.positions;
    const out = [];
    for (let i = 1; i < positions.length; i++) {
      const before = whiteShareOf(positions[i - 1]);
      const after = whiteShareOf(positions[i]);
      // Odd ply = White's move, even ply = Black's move.
      const loss = i % 2 === 1 ? before - after : after - before;
      out.push({ ply: i, loss });
    }
    return out;
  }, [analysis]);

  const errorPlies = useMemo(
    () => swings.filter((s) => s.loss >= INACCURACY).map((s) => s.ply),
    [swings]
  );
  const biggest = useMemo(
    () =>
      swings.reduce(
        (best, s) => (s.loss > best.loss ? s : best),
        { ply: 0, loss: 0 }
      ),
    [swings]
  );
  const biggestText = biggest.ply
    ? `${moves[biggest.ply - 1]?.san ?? ""} ${formatScoreOf(
        analysis?.positions?.[biggest.ply - 1]
      )} → ${formatScoreOf(analysis?.positions?.[biggest.ply])}`
    : "";

  const jumpToBiggest = () => setPly(biggest.ply);
  const nextError = () => {
    const next = errorPlies.find((p) => p > ply);
    setPly(next ?? errorPlies[0] ?? ply);
  };
  const prevError = () => {
    const prev = [...errorPlies].reverse().find((p) => p < ply);
    setPly(prev ?? errorPlies[errorPlies.length - 1] ?? ply);
  };

  if (status === "loading") {
    return <div className="demo-message">Loading game…</div>;
  }
  if (status === "error") {
    return (
      <div className="demo-message demo-error">
        Failed to load replay: {error}
      </div>
    );
  }

  const { game, timeControl, matchName } = meta;
  const rows = [];
  for (let i = 0; i < moves.length; i += 2) {
    rows.push({ n: i / 2 + 1, white: moves[i], black: moves[i + 1] });
  }

  // Analysis is aligned by ply: positions[ply] covers the current position.
  const pos = analysis?.positions?.[ply] ?? null;

  const pvSans = (p) => {
    if (!p?.pv?.length) return [];
    const c = new Chess(p.fen);
    const out = [];
    for (const uci of p.pv) {
      try {
        const m = c.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] });
        out.push(m.san);
      } catch {
        break;
      }
    }
    return out;
  };
  const scoreText = formatScoreOf(pos);
  const pvText = pos ? pvSans(pos) : [];

  return (
    <div className="replay" ref={replayRef} onWheel={onWheel}>
      <div className="replay-board-col">
        {/* White at the bottom: react-chessboard's default white orientation
            places White at the bottom, so Black card sits above the board. */}
        <div className="player-card top">
          <span className="color-dot black" />
          <span className="player-name">{game.black_engine}</span>
        </div>
        <div className="board-wrap" data-fen={fen} style={{ width: boardSize }}>
          <Chessboard options={{ position: fen, allowDragging: false }} />
          {pos && (
            <div className="eval-bar" aria-label="Evaluation">
              <div
                className="eval-bar-white"
                style={{ height: `${whiteShareOf(pos) * 100}%` }}
              />
            </div>
          )}
        </div>
        <div className="player-card bottom">
          <span className="player-name">{game.white_engine}</span>
          <span className="color-dot white" />
        </div>
        <div className="controls">
          <button
            type="button"
            onClick={() => setPly(0)}
            disabled={ply === 0}
          >
            first
          </button>
          <button
            type="button"
            aria-label="Previous move"
            onClick={() => setPly((p) => Math.max(0, p - 1))}
            disabled={ply === 0}
          >
            ←
          </button>
          <span className="ply-indicator">
            {ply}/{moves.length}
          </span>
          <button
            type="button"
            aria-label="Next move"
            onClick={() => setPly((p) => Math.min(moves.length, p + 1))}
            disabled={ply === moves.length}
          >
            →
          </button>
          <button
            type="button"
            onClick={() => setPly(moves.length)}
            disabled={ply === moves.length}
          >
            last
          </button>
          <button
            type="button"
            aria-label="Play or pause autoplay"
            onClick={togglePlay}
            className={playing ? "active" : ""}
          >
            {playing ? "⏸" : "▶"}
          </button>
        </div>
        <div className="demo-note">←/→ ↑/↓ step · Home/End jump · wheel step · Space play</div>
      </div>

      <div className="replay-side-col">
        <div className="badges">
          <span className="badge">Game {game.game_number}</span>
          <span className="badge">Pair {pairIndex + 1}</span>
          <span className="badge">{TC_LABELS[timeControl] || timeControl}</span>
          <span className="badge badge-result">
            {game.result || "?"}
            {game.termination ? ` · ${game.termination}` : ""}
          </span>
        </div>

        {pos && (
          <div className="analysis-panel">
            <div className="analysis-score">
              {scoreText ?? "—"}
              <span className="analysis-engine">{analysis.engine_name}</span>
            </div>
            {pos.best_move && (
              <div className="analysis-line">
                Best: <strong>{pvText[0] ?? pos.best_move}</strong>
              </div>
            )}
            {pvText.length > 0 && (
              <div className="analysis-pv">PV: {pvText.join(" ")}</div>
            )}
            <div className="analysis-actions">
              <button
                type="button"
                onClick={jumpToBiggest}
                disabled={!biggest.ply}
                title="Jump to the biggest winning-share swing"
              >
                Biggest swing{biggest.ply ? `: ${biggestText}` : ""}
              </button>
              <button
                type="button"
                onClick={prevError}
                disabled={errorPlies.length === 0}
              >
                ‹ Error
              </button>
              <button
                type="button"
                onClick={nextError}
                disabled={errorPlies.length === 0}
              >
                Error ›
              </button>
            </div>
          </div>
        )}

        <div className="moves-list">
          {rows.map((r) => (
            <div className="move-row" key={r.n}>
              <span className="move-n">{r.n}.</span>
              <button
                type="button"
                className={"move" + (ply === r.n * 2 - 1 ? " active" : "")}
                onClick={() => setPly(r.n * 2 - 1)}
                title={moveMark(swings[r.n * 2 - 2]?.loss)
                  ? "Arena classification · evaluation swing"
                  : ""}
              >
                {r.white.san}
                {moveMark(swings[r.n * 2 - 2]?.loss) && (
                  <span className="move-mark">
                    {moveMark(swings[r.n * 2 - 2].loss)}
                  </span>
                )}
              </button>
              {r.black && (
                <button
                  type="button"
                  className={"move" + (ply === r.n * 2 ? " active" : "")}
                  onClick={() => setPly(r.n * 2)}
                  title={moveMark(swings[r.n * 2 - 1]?.loss)
                    ? "Arena classification · evaluation swing"
                    : ""}
                >
                  {r.black.san}
                  {moveMark(swings[r.n * 2 - 1]?.loss) && (
                    <span className="move-mark">
                      {moveMark(swings[r.n * 2 - 1].loss)}
                    </span>
                  )}
                </button>
              )}
            </div>
          ))}
        </div>

        <div className="actions">
          <a
            href={`${basePath}/public-api/v1/games/${gameId}/pgn`}
            className="action-link"
          >
            Download PGN
          </a>
        </div>

        <div className="demo-note">{matchName}</div>
      </div>
    </div>
  );
}
