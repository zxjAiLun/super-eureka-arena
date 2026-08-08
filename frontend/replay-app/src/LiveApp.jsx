import { useEffect, useRef, useState } from "react";
import { Chessboard } from "react-chessboard";

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
  const [boardSize, setBoardSize] = useState(480);
  const replayRef = useRef(null);

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

  const fen = payload.opening_fen || START_FEN;
  const inProgress =
    payload.state === "game_running" ||
    payload.state === "pending" ||
    payload.state === "pair_done";

  return (
    <div className="replay" ref={replayRef}>
      <div className="replay-board-col">
        <div className="player-card top">
          <span className="color-dot black" />
          <span className="player-name">{payload.engine_a_label}</span>
        </div>
        <div className="board-wrap" data-fen={fen} style={{ width: boardSize }}>
          <Chessboard options={{ position: fen, allowDragging: false }} />
        </div>
        <div className="player-card bottom">
          <span className="player-name">{payload.engine_b_label}</span>
          <span className="color-dot white" />
        </div>
        {inProgress && (
          <div className="demo-message">Game in progress…</div>
        )}
      </div>
      <div className="replay-side-col">
        <Badges data={payload} />
        <p className="demo-note">
          Position shown is the opening of the current pair. The authoritative
          result appears once the pair passes verification.
        </p>
      </div>
    </div>
  );
}
