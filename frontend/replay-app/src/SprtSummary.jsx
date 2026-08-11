// Shared SPRT evidence block (P4.12 follow-up): rendered on BOTH the running
// and the completed Live page from the whitelisted payload.sprt fields.

export default function SprtSummary({ sprt }) {
  if (!sprt) return null;
  return (
    <>
      <div className="analysis-line">
        SPRT · {sprt.decision} · LLR {Number(sprt.llr).toFixed(3)} · bounds{" "}
        {sprt.lower_bound} / {sprt.upper_bound}
      </div>
      <div className="analysis-pv">
        H0 {sprt.elo0} Elo · H1 {sprt.elo1} Elo · Ptnml [
        {(sprt.ptnml || []).join(", ")}]
      </div>
    </>
  );
}
