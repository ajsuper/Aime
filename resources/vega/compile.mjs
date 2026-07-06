// Server-side Vega-Lite compile gate for CreateGraphics.
//
// The web frontend (web_chat.html `ensureVega`) can only render a spec after
// vega-lite compiles it to Vega and vega parses that Vega. We run the *same*
// two steps here, on the same pinned major (see ../../package.json), so a spec
// the model just wrote is rejected server-side for exactly the reasons it would
// fail to render in the browser — and the real error is handed back for a
// same-turn fix instead of surfacing as a silent client "Couldn't render" card.
//
// Pure transform: reads one JSON spec on stdin, writes one JSON line to stdout,
// touches no network and no filesystem. Called by aime.vega_compile, which owns
// the timeout, the size cap, and the graceful fallback when Node/deps are
// missing. Never throws to the process boundary — every outcome is a JSON line.
//
//   stdin:  the Vega-Lite spec (raw JSON text)
//   stdout: {"ok": true}                      spec compiles + parses
//           {"ok": false, "error": "<msg>"}   spec is invalid (the reason)

import { compile } from "vega-lite";
import { parse } from "vega";

function readStdin() {
  return new Promise((resolve) => {
    let buf = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => (buf += chunk));
    process.stdin.on("end", () => resolve(buf));
  });
}

// vega-lite / vega throw Error objects, but also occasionally strings; either
// way we want a single readable line for the model, never a stack trace.
function message(err) {
  const raw = (err && err.message) ? err.message : String(err);
  return raw.replace(/\s+/g, " ").trim().slice(0, 400);
}

async function main() {
  const source = await readStdin();
  let spec;
  try {
    spec = JSON.parse(source);
  } catch (err) {
    // The Python side already gates JSON-validity, so this is belt-and-braces.
    process.stdout.write(JSON.stringify({ ok: false, error: "Invalid JSON: " + message(err) }) + "\n");
    return;
  }

  let compiled;
  try {
    compiled = compile(spec);
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, error: message(err) }) + "\n");
    return;
  }

  try {
    parse(compiled.spec);
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, error: message(err) }) + "\n");
    return;
  }

  process.stdout.write(JSON.stringify({ ok: true }) + "\n");
}

main().catch((err) => {
  // Any unexpected failure is reported as an internal error, not an invalid
  // spec — the Python wrapper treats a non-{ok:false} outcome as "unavailable"
  // and falls back to the loose gate rather than blaming the model's source.
  process.stdout.write(JSON.stringify({ ok: null, error: message(err) }) + "\n");
  process.exit(0);
});
