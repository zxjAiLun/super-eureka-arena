import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Chessboard } from "react-chessboard";
import { Chess } from "chess.js";

const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const TOKEN_KEY = "chessarena.humanPlay.token";
const GAME_KEY = "chessarena.humanPlay.gameId";

const TERMINAL_STATUSES = new Set([
  "FINISHED",
  "RESIGNED",
  "EXPIRED",
  "INTERRUPTED",
  "ENGINE_FAILED",
]);

const RESULT_TEXT = {
  "1-0": "1–0 · White wins",
  "0-1": "0–1 · Black wins",
  "1/2-1/2": "½–½ · Draw",
};

const TERMINATION_TEXT = {
  checkmate: "Checkmate",
  stalemate: "Stalemate",
  insufficient_material: "Insufficient material",
  threefold_repetition: "Threefold repetition",
  fifty_move_rule: "Fifty-move rule",
  resign: "Resigned",
  ttl_expired: "Game expired",
  idle_expired: "Game expired (idle)",
  engine_error: "Engine error",
};

function terminationLabel(t) {
  if (!t) return "";
  return TERMINATION_TEXT[t] || t;
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
class ApiError extends Error {
  constructor(status, body) {
    super(body?.detail || `request failed (${status})`);
    this.status = status;
    this.body = body;
  }
}

async function api(path, { method = "GET", csrf, gameToken, body } = {}) {
  const headers = {};
  if (csrf) headers["X-CSRF-Token"] = csrf;
  if (gameToken) headers["X-Game-Token"] = gameToken;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let parsed = null;
    try {
      parsed = await res.json();
    } catch (e) {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, parsed);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Hook: the whole human-play lifecycle
// ---------------------------------------------------------------------------
function useHumanPlay({ basePath, csrf, pollSeconds }) {
  const [phase, setPhase] = useState("lobby"); // lobby | playing | error
  const [opponents, setOpponents] = useState([]);
  const [opponentsError, setOpponentsError] = useState(null);
  const [game, setGame] = useState(null);
  const [gameToken, setGameToken] = useState(null);
  const [error, setError] = useState(null);
  const [fatal, setFatal] = useState(null);
  const pollTimer = useRef(null);
  const gameRef = useRef(null);

  // Lobby: load the opponent list once.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api(`${basePath}/public-api/v1/human-play/opponents`);
        if (!cancelled) setOpponents(list);
      } catch (e) {
        if (!cancelled) setOpponentsError(e.message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [basePath]);

  const stopPolling = useCallback(() => {
    if (pollTimer.current != null) {
      clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  // Poll while a move is pending and the game is active.
  useEffect(() => {
    gameRef.current = game;
    if (!game || !gameToken) return;
    if (game.status !== "ACTIVE" || !game.engine_pending) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await api(
          `${basePath}/public-api/v1/human-play/games/${game.id}`,
          { gameToken }
        );
        if (cancelled) return;
        setGame(next);
        if (next.status === "ACTIVE" && next.engine_pending) {
          pollTimer.current = setTimeout(tick, pollSeconds * 1000);
        }
      } catch (e) {
        if (cancelled) return;
        // 401 => token invalid / game purged: drop to lobby.
        if (e.status === 401 || e.status === 404) {
          setFatal(null);
          setGame(null);
          setGameToken(null);
          setPhase("lobby");
          localStorage.removeItem(TOKEN_KEY);
          localStorage.removeItem(GAME_KEY);
          return;
        }
        pollTimer.current = setTimeout(tick, Math.max(pollSeconds, 1.5) * 1000);
      }
    };
    pollTimer.current = setTimeout(tick, pollSeconds * 1000);
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [game, gameToken, basePath, pollSeconds, stopPolling]);

  // Session restore from localStorage.
  useEffect(() => {
    const savedToken = localStorage.getItem(TOKEN_KEY);
    const savedId = localStorage.getItem(GAME_KEY);
    if (!savedToken || !savedId) return;
    let cancelled = false;
    (async () => {
      try {
        const g = await api(
          `${basePath}/public-api/v1/human-play/games/${savedId}`,
          { gameToken: savedToken }
        );
        if (cancelled) return;
        setGame(g);
        setGameToken(savedToken);
        setPhase("playing");
      } catch (e) {
        if (cancelled) return;
        localStorage.removeItem(TOKEN_KEY);
        localStorage.removeItem(GAME_KEY);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [basePath]);

  const createGame = useCallback(
    async (opponent, humanColor) => {
      setError(null);
      try {
        const g = await api(`${basePath}/public-api/v1/human-play/games`, {
          method: "POST",
          csrf,
          body: { opponent, human_color: humanColor },
        });
        const token = g.game_token;
        delete g.game_token;
        localStorage.setItem(TOKEN_KEY, token);
        localStorage.setItem(GAME_KEY, g.id);
        setGame(g);
        setGameToken(token);
        setPhase("playing");
      } catch (e) {
        setError(e.message);
      }
    },
    [basePath, csrf]
  );

  const submitMove = useCallback(
    async (uci) => {
      if (!game || !gameToken) return null;
      try {
        const next = await api(
          `${basePath}/public-api/v1/human-play/games/${game.id}/moves`,
          {
            method: "POST",
            csrf,
            gameToken,
            body: { uci, expected_revision: game.revision },
          }
        );
        setGame(next);
        return next;
      } catch (e) {
        if (e.status === 409) {
          // Stale revision / not our turn / engine pending: re-sync.
          try {
            const fresh = await api(
              `${basePath}/public-api/v1/human-play/games/${game.id}`,
              { gameToken }
            );
            setGame(fresh);
          } catch (e2) {
            setError(e.message);
          }
        } else if (e.status === 410) {
          setGame((g) => (g ? { ...g, status: "EXPIRED" } : g));
        } else {
          setError(e.message);
        }
        return null;
      }
    },
    [game, gameToken, basePath, csrf]
  );

  const resign = useCallback(async () => {
    if (!game || !gameToken) return;
    try {
      const next = await api(
        `${basePath}/public-api/v1/human-play/games/${game.id}/resign`,
        { method: "POST", csrf, gameToken }
      );
      setGame(next);
    } catch (e) {
      setError(e.message);
    }
  }, [game, gameToken, basePath, csrf]);

  const newGame = useCallback(() => {
    stopPolling();
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(GAME_KEY);
    setGame(null);
    setGameToken(null);
    setPhase("lobby");
  }, [stopPolling]);

  return {
    phase,
    opponents,
    opponentsError,
    game,
    gameToken,
    error,
    createGame,
    submitMove,
    resign,
    newGame,
    setGame,
  };
}

// ---------------------------------------------------------------------------
// Lobby
// ---------------------------------------------------------------------------
function Lobby({ opponents, opponentsError, onCreate, busy, error }) {
  const [opponent, setOpponent] = useState("");
  const [color, setColor] = useState("white");
  const selected = opponents.find((o) => o.id === opponent) || opponents[0];

  useEffect(() => {
    if (!opponent && opponents.length) setOpponent(opponents[0].id);
  }, [opponents, opponent]);

  if (opponentsError) {
    return (
      <div className="hp-lobby">
        <div className="demo-message demo-error">{opponentsError}</div>
      </div>
    );
  }
  if (!opponents.length) {
    return (
      <div className="hp-lobby">
        <div className="demo-message">
          No opponents are currently available.
        </div>
      </div>
    );
  }
  return (
    <div className="hp-lobby">
      <h2 className="hp-lobby-title">Play against an engine</h2>
      <p className="hp-lobby-note">
        The engine runs on the Arena server between its scheduled matches —
        replies usually arrive within a couple of seconds.
      </p>
      <label className="hp-field">
        <span className="hp-field-label">Opponent</span>
        <select
          value={opponent}
          onChange={(e) => setOpponent(e.target.value)}
          disabled={busy}
        >
          {opponents.map((o) => (
            <option key={o.id} value={o.id}>
              {o.display_name}
            </option>
          ))}
        </select>
      </label>
      <label className="hp-field">
        <span className="hp-field-label">Your color</span>
        <select
          value={color}
          onChange={(e) => setColor(e.target.value)}
          disabled={busy}
        >
          <option value="white">White (you move first)</option>
          <option value="black">Black</option>
        </select>
      </label>
      {selected?.strength_label ? (
        <p className="hp-lobby-strength">
          Target strength: about {selected.strength_label} Elo.
        </p>
      ) : null}
      {error ? <div className="demo-message demo-error">{error}</div> : null}
      <button
        className="hp-start"
        onClick={() => onCreate(opponent, color)}
        disabled={busy || !opponent}
      >
        Start game
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Promotion picker
// ---------------------------------------------------------------------------
const PROMOTION_PIECES = ["q", "r", "b", "n"];

function PromotionPicker({ color, onPick, onCancel }) {
  return (
    <div className="hp-promo-backdrop" onClick={onCancel}>
      <div className="hp-promo" onClick={(e) => e.stopPropagation()}>
        <div className="hp-promo-title">Promote to</div>
        <div className="hp-promo-row">
          {PROMOTION_PIECES.map((p) => (
            <button key={p} onClick={() => onPick(p)} title={p.toUpperCase()}>
              <span className={`hp-promo-piece ${color} ${p}`} />
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main app
// ---------------------------------------------------------------------------
export default function HumanPlayApp({ basePath, csrfToken, pollSeconds }) {
  const hp = useHumanPlay({ basePath, csrf: csrfToken, pollSeconds });
  const { game, phase } = hp;
  const [promotion, setPromotion] = useState(null); // {from, to}
  const [reviewPly, setReviewPly] = useState(null);
  const [busy, setBusy] = useState(false);
  const movesListRef = useRef(null);

  const myTurn =
    game != null &&
    game.status === "ACTIVE" &&
    !game.engine_pending &&
    game.side_to_move === game.human_color;

  // Chess.js view of the authoritative game state.
  const gameChess = useMemo(() => {
    if (!game) return null;
    const c = new Chess();
    for (const m of game.moves) {
      try {
        c.move({ from: m.uci.slice(0, 2), to: m.uci.slice(2, 4),
                 promotion: m.uci.length > 4 ? m.uci[4] : undefined });
      } catch (e) {
        break;
      }
    }
    return c;
  }, [game]);

  // Enter review mode when the game ends.
  useEffect(() => {
    if (game && TERMINAL_STATUSES.has(game.status)) {
      setReviewPly(game.moves.length);
    } else {
      setReviewPly(null);
    }
  }, [game?.status, game?.moves.length]);

  // Terminal-position fallback: a FINISHED game whose last recorded FEN is
  // already terminal (authoritative status from the server).
  useEffect(() => {
    if (game && game.moves.length === 0 && game.status !== "ACTIVE") {
      setReviewPly(0);
    }
  }, [game]);

  const inReview = reviewPly != null;
  const terminal = game != null && TERMINAL_STATUSES.has(game.status);

  // The FEN to render: review ply or the live position.
  const displayFen = useMemo(() => {
    if (!game) return START_FEN;
    if (!inReview) return game.fen;
    if (reviewPly === game.moves.length) return game.fen;
    if (reviewPly === 0) return START_FEN;
    return game.moves[reviewPly - 1].fen_after;
  }, [game, inReview, reviewPly]);

  // Auto-scroll the move list to the latest move.
  useEffect(() => {
    const el = movesListRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [game?.moves.length]);

  const onPieceDrop = useCallback(
    ({ sourceSquare, targetSquare, piece }) => {
      if (!myTurn || inReview || !gameChess) return false;
      const moving = gameChess.get(sourceSquare);
      if (!moving) return false;
      const isPromotion =
        moving.type === "p" &&
        ((moving.color === "w" && targetSquare[1] === "8") ||
         (moving.color === "b" && targetSquare[1] === "1"));
      const doSubmit = (promotion) => {
        const uci = sourceSquare + targetSquare + (promotion || "");
        setBusy(true);
        hp.submitMove(uci).finally(() => setBusy(false));
      };
      if (isPromotion) {
        setPromotion({ from: sourceSquare, to: targetSquare });
        return true;
      }
      try {
        const probe = new Chess(game.fen);
        probe.move({
          from: sourceSquare,
          to: targetSquare,
          promotion: undefined,
        });
      } catch (e) {
        return false;
      }
      doSubmit();
      return true;
    },
    [myTurn, inReview, gameChess, game, hp]
  );

  const boardOrientation = game ? game.human_color : "white";

  if (phase === "lobby") {
    return (
      <div className="hp-root">
        <Lobby
          opponents={hp.opponents}
          opponentsError={hp.opponentsError}
          onCreate={(opponent, color) => {
            setBusy(true);
            hp.createGame(opponent, color).finally(() => setBusy(false));
          }}
          busy={busy}
          error={hp.error}
        />
      </div>
    );
  }

  if (!game) {
    return <div className="demo-message">Loading game…</div>;
  }

  const opponentTop = game.human_color === "white";
  const topName = opponentTop ? game.opponent_name : "You";
  const bottomName = opponentTop ? "You" : game.opponent_name;

  const statusText = () => {
    if (game.status === "ACTIVE") {
      if (game.engine_pending) return "Engine is thinking…";
      if (myTurn) return game.in_check ? "Your move — check!" : "Your move";
      return "Engine to move";
    }
    const r = game.result ? RESULT_TEXT[game.result] || game.result : "";
    const t = terminationLabel(game.termination);
    return [r, t].filter(Boolean).join(" · ") || game.status;
  };

  const moveRows = [];
  for (let i = 0; i < game.moves.length; i += 2) {
    const num = i / 2 + 1;
    const w = game.moves[i];
    const b = game.moves[i + 1];
    moveRows.push(
      <div className="move-row" key={num}>
        <span className="move-num">{num}.</span>
        <button
          className={"move-san" + (reviewPly === i + 1 ? " active" : "")}
          onClick={() => setReviewPly(i + 1)}
        >
          {w?.san ?? ""}
        </button>
        {b ? (
          <button
            className={"move-san" + (reviewPly === i + 2 ? " active" : "")}
            onClick={() => setReviewPly(i + 2)}
          >
            {b.san}
          </button>
        ) : (
          <span className="move-san" />
        )}
      </div>
    );
  }

  return (
    <div className="hp-root">
      <div className="replay hp-main">
        <div className="replay-board-col">
          <div className="player-card top">
            <span className={"color-dot " + (opponentTop ? "black" : "white")} />
            <span className="player-name">{topName}</span>
          </div>
          <div className="board-stage">
            <div className="board-wrap" data-fen={displayFen}>
              <Chessboard
                options={{
                  position: displayFen,
                  boardOrientation,
                  allowDragging: myTurn && !inReview && !busy,
                  onPieceDrop,
                  id: "human-play",
                }}
              />
              {promotion ? (
                <PromotionPicker
                  color={game.human_color === "white" ? "w" : "b"}
                  onPick={(p) => {
                    const { from, to } = promotion;
                    setPromotion(null);
                    setBusy(true);
                    hp.submitMove(from + to + p).finally(() => setBusy(false));
                  }}
                  onCancel={() => setPromotion(null)}
                />
              ) : null}
            </div>
          </div>
          <div className="player-card bottom">
            <span className="player-name">{bottomName}</span>
            <span className={"color-dot " + (opponentTop ? "white" : "black")} />
          </div>
          <div className="hp-status" data-state={
            game.status === "ACTIVE"
              ? myTurn
                ? "your-move"
                : "engine"
              : "terminal"
          }>
            {statusText()}
          </div>
        </div>

        <div className="replay-side-col hp-side">
          <div className="badges">
            <span className="badge">Human vs Engine</span>
            <span className="badge">{game.opponent_name}</span>
            {terminal ? (
              <span className="badge badge-result">
                {game.result || game.status}
              </span>
            ) : null}
          </div>

          {terminal ? (
            <div className="hp-review-controls">
              <div className="controls">
                <button onClick={() => setReviewPly(0)} disabled={reviewPly === 0}>
                  ⏮
                </button>
                <button
                  onClick={() => setReviewPly(Math.max(0, (reviewPly ?? 0) - 1))}
                  disabled={reviewPly === 0}
                >
                  ◀
                </button>
                <button
                  onClick={() =>
                    setReviewPly(
                      Math.min(game.moves.length, (reviewPly ?? 0) + 1)
                    )
                  }
                  disabled={reviewPly === game.moves.length}
                >
                  ▶
                </button>
                <button
                  onClick={() => setReviewPly(game.moves.length)}
                  disabled={reviewPly === game.moves.length}
                >
                  ⏭
                </button>
              </div>
              <div className="hp-review-hint">Review the finished game</div>
            </div>
          ) : (
            <div className="hp-actions">
              <button
                className="hp-resign"
                onClick={() => {
                  if (window.confirm("Resign this game?")) {
                    hp.resign();
                  }
                }}
                disabled={!myTurn && !game.engine_pending}
              >
                Resign
              </button>
            </div>
          )}

          {hp.error ? (
            <div className="demo-message demo-error">{hp.error}</div>
          ) : null}

          <div className="moves-list" ref={movesListRef}>
            {moveRows.length ? (
              moveRows
            ) : (
              <div className="hp-moves-empty">No moves yet.</div>
            )}
          </div>

          {terminal ? (
            <div className="hp-actions">
              <a
                className="hp-link-button"
                href={`${basePath}/public-api/v1/human-play/games/${game.id}/pgn`}
                onClick={(e) => {
                  e.preventDefault();
                  const url = `${basePath}/public-api/v1/human-play/games/${game.id}/pgn`;
                  fetch(url, {
                    headers: { "X-Game-Token": hp.gameToken },
                  })
                    .then((r) => {
                      if (!r.ok) throw new Error(`pgn ${r.status}`);
                      return r.text();
                    })
                    .then((text) => {
                      const blob = new Blob([text], {
                        type: "application/x-chess-pgn",
                      });
                      const a = document.createElement("a");
                      a.href = URL.createObjectURL(blob);
                      a.download = `human-game-${game.id}.pgn`;
                      a.click();
                      URL.revokeObjectURL(a.href);
                    })
                    .catch((err) => hp.setError?.(err.message));
                }}
              >
                Download PGN
              </a>
              <button className="hp-start" onClick={hp.newGame}>
                New game
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
