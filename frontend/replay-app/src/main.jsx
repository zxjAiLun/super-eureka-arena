import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import LiveApp from "./LiveApp.jsx";
import HumanPlayApp from "./HumanPlayApp.jsx";
import "./styles.css";

const rootEl = document.getElementById("replay-root");
const humanPlayEl = document.getElementById("human-play-root");

if (humanPlayEl) {
  createRoot(humanPlayEl).render(
    <HumanPlayApp
      basePath={humanPlayEl.dataset.basePath}
      csrfToken={humanPlayEl.dataset.csrfToken}
      pollSeconds={parseFloat(humanPlayEl.dataset.pollSeconds || "0.4", 10)}
    />
  );
} else if (rootEl) {
  const basePath = rootEl.dataset.basePath;
  const tournamentId = rootEl.dataset.tournamentId;

  if (rootEl.dataset.mode === "live") {
    createRoot(rootEl).render(
      <LiveApp tournamentId={tournamentId || null} basePath={basePath} />
    );
  } else {
    const props = {
      gameId: rootEl.dataset.gameId,
      tournamentId,
      basePath,
      pairIndex: parseInt(rootEl.dataset.pairIndex || "0", 10),
    };
    if (!props.gameId || !props.tournamentId) {
      rootEl.innerHTML =
        '<p class="demo-message demo-error">Replay app missing game configuration.</p>';
    } else {
      createRoot(rootEl).render(<App {...props} />);
    }
  }
}
