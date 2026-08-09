import { useEffect, useMemo, useRef, useState } from "react";
import { Chessboard } from "react-chessboard";
import { Chess } from "chess.js";
import { useStockfishAnalysis } from "./stockfish/useStockfishAnalysis";

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
  blitz_10_01: "10s+0.1s",
  rapid_5_3: "5+3",
};

// P4.9: Arena swing classification (winning-share drop of the mover).
const INACCURACY = 0.1;
const MISTAKE = 0.2;
const BLUNDER = 0.35;

function formatScoreOf(p) {
  if (!p) return null;
  if (p.mate != null) {
    if (p.mate === 0) return null; // invalid mate score is not an evaluation
    return p.mate > 0 ? `M${p.mate}` : `-M${Math.abs(p.mate)}`;
  }
  if (p.score_cp == null) return null;
  const v = p.score_cp / 100;
  return (v > 0 ? "+" : "") + v.toFixed(2);
}

// Strict winning share for swing classification: null when the evaluation is
// unknown — never treated as an exactly equal position.
function shareOf(p) {
  if (!p) return null;
  if (p.mate != null) {
    if (p.mate === 0) return null;
    return p.mate > 0 ? 0.98 : 0.02;
  }
  if (p.score_cp == null) return null;
  return Math.min(0.98, Math.max(0.02, 1 / (1 + Math.exp(-p.score_cp / 250))));
}

// UI-safe share: unknown evaluations render as a neutral bar.
function shareForUi(p) {
  const s = shareOf(p);
  return s == null ? 0.5 : s;
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
  const [diagnostics, setDiagnostics] = useState(null);

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
          const diagRes = await fetch(
            `${basePath}/public-api/v1/games/${gameId}/analysis`
          );
          if (diagRes.ok && !cancelled) {
            setDiagnostics(await diagRes.json());
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

  return { status, error, moves, meta, pgn, startFen, diagnostics };
}

export default function App({ gameId, tournamentId, basePath, pairIndex }) {
  const { status, error, moves, meta, pgn, startFen, diagnostics } = useReplay({
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

  // P4.11 commit 2: interactive browser Stockfish — analyzes the current
  // position in the browser (no server artifact required).  The server
  // diagnostics remain only a whole-game source for ?!/??/biggest-swing.
  const browser = useStockfishAnalysis({
    fen,
    enabled: status === "ready",
    basePath,
  });

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
      // Resume from the current ply; only restart from the start when the
      // game is already at the final position.
      if (ply >= moves.length) setPly(0);
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
  }, [status, moves.length, playing, ply]);

  const onWheel = (e) => {
    // Wheel steps moves only outside the move list, so the move list can
    // still be scrolled normally; never preventDefault, so the page scroll
    // is never blocked.
    if (e.target && e.target.closest && e.target.closest(".moves-list")) {
      return;
    }
    step(e.deltaY > 0 ? 1 : -1);
  };

  // Keep the active move visible while navigating a long game: center it in
  // the move list (manual scroll; scrollIntoView misbehaves in this overflow
  // container), clamped at the edges.  A ResizeObserver re-centers whenever
  // the analysis panels change the list height.
  useEffect(() => {
    if (status !== "ready") return undefined;
    const list = document.querySelector(".moves-list");
    const active = document.querySelector(".move.active");
    const centerActive = () => {
      if (!list || !active) return;
      const max = list.scrollHeight - list.clientHeight;
      const lr = list.getBoundingClientRect();
      const ar = active.getBoundingClientRect();
      const target =
        list.scrollTop + (ar.top - lr.top) - list.clientHeight / 2;
      list.scrollTop = Math.max(0, Math.min(max, target));
    };
    centerActive();
    if (!list) return undefined;
    const ro = new ResizeObserver(centerActive);
    ro.observe(list);
    return () => ro.disconnect();
  }, [ply, status]);

  // FEN-aware move numbering: the start position may be mid-game (Black to
  // move, fullmove > 1), so move numbers come from the FEN header, not from
  // counting rows from 1.White.
  const startFields = startFen.split(" ");
  const startSide = startFields[1] || "w";
  const startFullmove = parseInt(startFields[5] || "1", 10) || 1;
  // Moves already played before the FEN start, per color.
  const w0 = startSide === "w" ? startFullmove - 1 : startFullmove;
  const b0 = startFullmove - 1;
  const moveNumber = (i) => {
    // i: 1-based ply; returns the fullmove number of that move.
    const m = moves[i - 1];
    if (!m) return 0;
    let same = 0;
    for (let k = 0; k < i - 1; k++) {
      if (moves[k].color === m.color) same += 1;
    }
    const base = m.color === "w" ? w0 : b0;
    return base + same + 1;
  };
  const moveLabel = (i) => {
    const m = moves[i - 1];
    if (!m) return "";
    const n = moveNumber(i);
    return m.color === "w" ? `${n}.${m.san}` : `${n}...${m.san}`;
  };

  // P4.9b/c: per-move winning-share swing from the mover's perspective.
  // The mover comes from the actual move (moves[i-1].color), never from ply
  // parity, so FEN starts with Black to move work.  Moves with an unknown
  // evaluation on either side are marked `known: false` and never participate
  // in classification or the biggest-swing search.
  const swings = useMemo(() => {
    if (!diagnostics?.positions?.length) return [];
    const positions = diagnostics.positions;
    const out = [];
    for (let i = 1; i < positions.length; i++) {
      const before = shareOf(positions[i - 1]);
      const after = shareOf(positions[i]);
      const mover = moves[i - 1];
      if (before == null || after == null || !mover) {
        out.push({ ply: i, loss: 0, known: false });
        continue;
      }
      const loss =
        mover.color === "w" ? before - after : after - before;
      out.push({ ply: i, loss, known: true });
    }
    return out;
  }, [diagnostics, moves]);

  const errorPlies = useMemo(
    () =>
      swings
        .filter((s) => s.known && s.loss >= INACCURACY)
        .map((s) => s.ply),
    [swings]
  );
  const biggest = useMemo(
    () =>
      swings
        .filter((s) => s.known)
        .reduce(
          (best, s) => (s.loss > best.loss ? s : best),
          { ply: 0, loss: 0, known: false }
        ),
    [swings]
  );
  const biggestText = biggest.ply
    ? `${moveLabel(biggest.ply)} ${formatScoreOf(
        diagnostics?.positions?.[biggest.ply - 1]
      )} → ${formatScoreOf(diagnostics?.positions?.[biggest.ply])}`
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
  // Classification mark for the move ending at swing index `idx`; moves with
  // an unknown evaluation never get a mark.
  const markFor = (idx) => {
    const s = swings[idx];
    return s && s.known ? moveMark(s.loss) : "";
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
  // Rows are grouped by real fullmove number (FEN-aware); each row holds the
  // white and/or black move of that fullmove with their 1-based plies.
  const rows = [];
  const rowByN = new Map();
  for (let i = 1; i <= moves.length; i++) {
    const m = moves[i - 1];
    const n = moveNumber(i);
    let row = rowByN.get(n);
    if (!row) {
      row = { n, white: null, black: null };
      rowByN.set(n, row);
      rows.push(row);
    }
    if (m.color === "w") row.white = { move: m, ply: i };
    else row.black = { move: m, ply: i };
  }

  // Analysis is aligned by ply: positions[ply] covers the current position.
  const pos = diagnostics?.positions?.[ply] ?? null;

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
  // The eval bar prefers the live browser Stockfish result.
  const hasBrowserScore = browser.score_cp != null || browser.mate != null;
  const barPos = hasBrowserScore ? browser : pos;
  const browserScoreText = formatScoreOf(browser);
  const browserPv = browser.pv ? pvSans({ fen, pv: browser.pv }) : [];

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
          {barPos && (
            <div className="eval-bar" aria-label="Evaluation">
              <div
                className="eval-bar-white"
                style={{ height: `${shareForUi(barPos) * 100}%` }}
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

        {browser.status === "error" && (
          <div className="analysis-panel">
            <div className="analysis-score">
              Stockfish unavailable
              <span className="analysis-engine">Stockfish · browser</span>
            </div>
            <div className="analysis-line">engine failed to start</div>
          </div>
        )}

        {(hasBrowserScore || browser.status === "searching") && (
          <div className="analysis-panel">
            <div className="analysis-score">
              {browserScoreText ?? "…"}
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
            {browserPv.length > 0 && (
              <div className="analysis-pv">PV: {browserPv.join(" ")}</div>
            )}
          </div>
        )}

        {pos && (
          <div className="diagnostics-panel">
            <div className="diagnostics-title">
              Game diagnostics
              {(scoreText || pos.best_move) && (
                <span className="diagnostics-engine">
                  {scoreText ? ` · ${scoreText}` : ""}
                  {pos.best_move ? ` · best ${pos.best_move}` : ""}
                </span>
              )}
            </div>
            {pvText.length > 0 && (
              <div className="diagnostics-pv">PV: {pvText.join(" ")}</div>
            )}
            <div className="diagnostics-actions">
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
              {r.white ? (
                <button
                  type="button"
                  className={"move" + (ply === r.white.ply ? " active" : "")}
                  onClick={() => setPly(r.white.ply)}
                  title={markFor(r.white.ply - 1)
                    ? "Arena classification · evaluation swing"
                    : ""}
                >
                  {r.white.move.san}
                  {markFor(r.white.ply - 1) && (
                    <span className="move-mark">
                      {markFor(r.white.ply - 1)}
                    </span>
                  )}
                </button>
              ) : (
                <span className="move-ellipsis">…</span>
              )}
              {r.black && (
                <button
                  type="button"
                  className={"move" + (ply === r.black.ply ? " active" : "")}
                  onClick={() => setPly(r.black.ply)}
                  title={markFor(r.black.ply - 1)
                    ? "Arena classification · evaluation swing"
                    : ""}
                >
                  {r.black.move.san}
                  {markFor(r.black.ply - 1) && (
                    <span className="move-mark">
                      {markFor(r.black.ply - 1)}
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
