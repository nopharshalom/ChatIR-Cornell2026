// Human-in-the-loop interactive retrieval demo. No build step: plain DOM + fetch.
const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"];
// Each image keeps one frame color and tilt, so you can follow it as it moves between rounds.
function hash(str) { let h = 2166136261; for (const ch of str) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; }
const colorOf = (path) => PALETTE[hash(path) % PALETTE.length];
const tiltOf = (path, max = 2.2) => (((hash(path + "~") % 1000) / 1000) * 2 - 1) * max;
function frame(node, path, max) { node.style.setProperty("--c", colorOf(path)); node.style.setProperty("--r", tiltOf(path, max).toFixed(2) + "deg"); }
const img = (path) => "/images/" + path.split("/").map(encodeURIComponent).join("/");

const S = {
  info: null, mode: "free", challenge: null, session: null,
  pending: null,          // the agent's current question payload
  chosen: null,           // {question, source}
  ownMode: false, prevRanks: {}, rating: null, finishPath: null, busy: false,
};

async function api(path, body, method) {
  const r = await fetch(path, { method: method || (body !== undefined ? "POST" : "GET"),
    headers: { "Content-Type": "application/json" }, body: body !== undefined ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return r.json();
}
function toast(msg, ms = 3500) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), ms); }
function busy(on) { S.busy = on; document.querySelectorAll("#answer-form button, #undo").forEach((b) => (b.disabled = on)); }

// ------------------------------------------------------------------ hero collage
// Scattered, tilted dataset images around the start card, framed like the cluster plot.
const SLOTS = [ // [left%, top%, width px, edge?]
  [1, 4, 210, 1], [16, 1, 170, 0], [3, 40, 180, 1], [14, 62, 220, 0], [0, 76, 160, 1],
  [80, 2, 200, 1], [73, 18, 150, 0], [85, 34, 190, 1], [75, 56, 200, 0], [86, 78, 170, 1],
  [24, 84, 150, 0], [62, 86, 150, 0], [39, 88, 120, 0],
];
async function buildCollage() {
  const box = $("#collage"); if (!box) return;
  let paths = [];
  try { paths = await api(`/api/collage?n=${SLOTS.length}`); } catch { return; }
  box.innerHTML = "";
  paths.forEach((p, i) => {
    const [l, t, w, edge] = SLOTS[i];
    const ph = el("div", "ph" + (edge ? " edge" : ""));
    ph.style.cssText = `left:${l}%;top:${t}%;width:${w}px;height:${Math.round(w * 0.75)}px`;
    ph.style.setProperty("--c", PALETTE[i % PALETTE.length]);
    ph.style.setProperty("--r", (((i * 37) % 11) - 5) * 1.1 + "deg");
    if (i % 3 === 0) ph.append(el("span", "tag", `C${i % 10}`));
    const im = el("img"); im.alt = ""; im.src = img(p);
    im.onload = () => ph.classList.add("in"); im.onerror = () => ph.remove();
    ph.append(im); box.append(ph);
  });
}

// ------------------------------------------------------------------ start screen
async function init() {
  try {
    S.info = await api("/api/info");
    const m = S.info.method || {};
    $("#backend").textContent = `${S.info.backend} · ranker: ${m.ranker || "?"} · ${S.info.corpus_size.toLocaleString()} images`;
    $("#backend").title = `fingerprint ${S.info.fingerprint}\n` + Object.entries(m).map(([k, v]) => `${k}: ${v}`).join("\n");
    if (!S.info.challenge) document.querySelector('[data-mode="challenge"]').classList.add("hidden");
  } catch (e) { $("#backend").textContent = "backend offline"; toast("Cannot reach the backend: " + e.message); }
}

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => setMode(t.dataset.mode)));
async function setMode(mode) {
  S.mode = mode;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.mode === mode));
  $(".mode-free").classList.toggle("hidden", mode !== "free");
  $(".mode-challenge").classList.toggle("hidden", mode !== "challenge");
  $("#challenge-hint").classList.toggle("hidden", mode !== "challenge");
  if (mode === "challenge" && !S.challenge) await newChallenge();
  $("#caption").placeholder = mode === "challenge" ? "Describe the target image in one sentence…" : "e.g. a dog playing frisbee in a park";
}
async function newChallenge() {
  try {
    S.challenge = await api("/api/challenge");
    $("#challenge-img").src = img(S.challenge.target);
    const hint = $("#challenge-hint"); hint.innerHTML = "";
    hint.append("VisDial's original caption: ");
    const a = el("a", null, `“${S.challenge.caption}”`); a.href = "#";
    a.onclick = (e) => { e.preventDefault(); $("#caption").value = S.challenge.caption; };
    hint.append(a, el("span", null, " (click to use it, or write your own)"));
  } catch (e) { toast(e.message); }
}
$("#challenge-shuffle").onclick = newChallenge;

$("#start-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const caption = $("#caption").value.trim(); if (!caption) return;
  const btn = e.submitter; btn.disabled = true;
  try {
    const s = await api("/api/sessions", { caption, target: S.mode === "challenge" ? S.challenge?.target : null,
      participant: $("#participant").value.trim() || null });
    S.prevRanks = {};
    $("#start").classList.add("hidden"); $("#dialog").classList.remove("hidden");
    render(s);
    await ask();
  } catch (err) { toast(err.message); } finally { btn.disabled = false; }
});

// ------------------------------------------------------------------ rendering
function render(s) {
  S.session = s;
  $("#round").textContent = `round ${Math.min(s.round, s.max_rounds)} / ${s.max_rounds}`;
  $("#query").textContent = s.query;
  const qh = $("#query-history"); qh.innerHTML = "";
  if (s.query_history.length > 1) s.query_history.slice(0, -1).forEach((q, i) => qh.append(el("li", null, `r${i}: ${q}`)));
  $("#undo").disabled = s.turns.length === 0 || s.done;
  $("#ask-box").classList.toggle("hidden", s.done || s.round > s.max_rounds);

  // target / rank
  const hasT = !!s.target;
  $("#target-box").classList.toggle("hidden", !hasT);
  $("#sim").classList.toggle("hidden", !(hasT && S.info?.simulated_user));
  if (hasT) {
    $("#target-img").src = img(s.target);
    $("#target-rank").textContent = s.target_rank != null ? `#${s.target_rank.toLocaleString()}` : "–";
    spark(s.rank_history);
  }

  // dialogue
  const ol = $("#turns"); ol.innerHTML = "";
  const li0 = el("li"); const cap = el("div", "bubble user caption"); cap.append(el("span", "who", "You · description"), s.caption); li0.append(cap); ol.append(li0);
  s.turns.forEach((t, i) => {
    const li = el("li");
    const q = el("div", "bubble agent"); q.append(el("span", "who", t.source === "user" ? "You asked" : t.source === "alternative" ? "Agent (you picked)" : "Agent"), t.question);
    const a = el("div", "bubble user"); a.append(el("span", "who", t.answered_by === "simulator" ? "AI simulator" : "You"), t.answer);
    li.append(q, a);
    if (t.target_rank != null) {
      const prev = i === 0 ? s.rank_history[0] : s.turns[i - 1].target_rank;
      const d = prev != null ? prev - t.target_rank : 0;
      const meta = el("div", "turn-meta", `target → #${t.target_rank} `);
      if (d) meta.append(el("span", d > 0 ? "delta-good" : "delta-bad", d > 0 ? `▲${d}` : `▼${-d}`));
      li.append(meta);
    }
    ol.append(li);
  });

  // results
  const grid = $("#grid"); grid.innerHTML = "";
  const now = {};
  s.results.forEach((r) => {
    now[r.path] = r.rank;
    const tile = el("div", "tile" + (r.path === s.target ? " is-target" : ""));
    frame(tile, r.path);
    const im = el("img"); im.loading = "lazy"; im.src = img(r.path); im.alt = r.caption || r.path;
    tile.append(im, el("span", "rank", `#${r.rank}`));
    const prev = S.prevRanks[r.path];
    if (Object.keys(S.prevRanks).length) tile.append(el("span", "moved", prev == null ? "new" : prev > r.rank ? `▲${prev - r.rank}` : prev < r.rank ? `▼${r.rank - prev}` : "="));
    tile.append(el("div", "cap", r.caption || r.path.split("/").pop()));
    tile.title = `${r.caption}\nscore ${r.score}`;
    tile.onclick = () => openFinish(r.path);
    grid.append(tile);
  });
  S.prevRanks = now;

  if (s.round > s.max_rounds && !s.done) { $("#question-area").classList.add("hidden"); toast("That was the last round. Pick your image, or start a new search."); }
}

function spark(hist) {
  const svg = $("#spark"); svg.innerHTML = "";
  const v = hist.filter((x) => x != null); if (v.length < 2) return;
  const L = v.map((x) => Math.log10(x)); const mx = Math.max(...L, 1), mn = 0;
  const pts = L.map((y, i) => `${(i / (L.length - 1)) * 118 + 1},${((y - mn) / (mx - mn || 1)) * 28 + 2}`).join(" ");
  svg.innerHTML = `<title>target rank per round (log scale, lower is better)</title><polyline points="${pts}"/>`;
}

function showQuestion(p) {
  S.pending = p;
  $("#question-area").classList.remove("hidden");
  $("#question").textContent = p.question;
  S.chosen = { question: p.question, source: "agent" };
  const ul = $("#alt-list"); ul.innerHTML = "";
  p.candidates.forEach((c) => {
    const b = el("button"); b.type = "button";
    b.append(el("span", null, c.question), el("span", "score", c.score != null ? `${p.criterion === "min_kl" ? "KL" : "score"} ${c.score.toFixed(4)}` : ""));
    if (c.question === p.question) b.classList.add("chosen");
    b.onclick = () => {
      S.chosen = { question: c.question, source: c.question === p.question ? "agent" : "alternative" };
      $("#question").textContent = c.question; setOwn(false);
      ul.querySelectorAll("button").forEach((x) => x.classList.toggle("chosen", x === b));
      $("#answer").focus();
    };
    ul.append(b);
  });
  $("#alts").classList.toggle("hidden", p.candidates.length < 2);
  $("#criterion").textContent = p.criterion === "min_kl"
    ? "PlugIR keeps the question whose embedding changes the candidate distribution least (minimum KL)."
    : p.criterion ? `selection: ${p.criterion}` : "";
  const ctx = $("#context"); ctx.innerHTML = "";
  (p.context || []).forEach((c, k) => { const f = el("figure"); f.dataset.c = `C${k}`; f.style.setProperty("--c", PALETTE[k % PALETTE.length]); const i = el("img"); i.loading = "lazy"; i.src = img(c.path); f.append(i, el("figcaption", null, c.caption)); ctx.append(f); });
  $("#context-wrap").classList.toggle("hidden", !(p.context || []).length);
  $("#answer").value = ""; $("#answer").focus();
}

async function ask() {
  const s = S.session; if (!s || s.done || s.round > s.max_rounds) return;
  $("#question-area").classList.add("hidden"); $("#thinking").classList.remove("hidden"); busy(true);
  const t0 = Date.now(), tick = setInterval(() => ($("#elapsed").textContent = `${Math.round((Date.now() - t0) / 1000)}s`), 1000);
  $("#elapsed").textContent = "0s";
  try { showQuestion(await api(`/api/sessions/${s.session_id}/ask`, {})); }
  catch (e) { toast("Agent failed to ask: " + e.message + " — you can still ask your own question."); setOwn(true); }
  finally { clearInterval(tick); $("#thinking").classList.add("hidden"); busy(false); }
}

// ------------------------------------------------------------------ answering
function setOwn(on) {
  S.ownMode = on;
  $("#own-q-wrap").classList.toggle("hidden", !on);
  $("#own-toggle").textContent = on ? "↩ use the agent's question" : "✎ ask my own question instead";
  $("#question-area").style.opacity = on ? 0.45 : 1;
  if (on) $("#own-q").focus();
}
$("#own-toggle").onclick = () => setOwn(!S.ownMode);
document.querySelectorAll("[data-quick]").forEach((b) => (b.onclick = () => { $("#answer").value = b.dataset.quick; $("#answer").focus(); }));

$("#sim").onclick = async () => {
  const q = S.ownMode ? $("#own-q").value.trim() : S.chosen?.question;
  if (!q) return toast("Write a question first.");
  busy(true);
  try { const r = await api(`/api/sessions/${S.session.session_id}/simulate`, { question: q }); $("#answer").value = r.answer; $("#answer").dataset.sim = r.answer; }
  catch (e) { toast(e.message); } finally { busy(false); }
};

$("#answer-form").addEventListener("submit", async (e) => {
  e.preventDefault(); if (S.busy) return;
  const answer = $("#answer").value.trim();
  const question = S.ownMode ? $("#own-q").value.trim() : S.chosen?.question;
  if (!question) return toast("Write a question, or switch back to the agent's.");
  if (!answer) return $("#answer").focus();
  const answered_by = $("#answer").dataset.sim === answer ? "simulator" : "human";
  delete $("#answer").dataset.sim;
  busy(true); $("#grid").classList.add("loading");
  try {
    const s = await api(`/api/sessions/${S.session.session_id}/turn`, { question, answer, answered_by, source: S.ownMode ? "user" : S.chosen.source });
    $("#own-q").value = ""; setOwn(false);
    render(s);
  } catch (err) { toast(err.message); busy(false); return; }
  finally { $("#grid").classList.remove("loading"); }
  busy(false);
  await ask();
});

$("#undo").onclick = async () => {
  try { const s = await api(`/api/sessions/${S.session.session_id}/undo`, {}); render(s); if (s.pending) showQuestion(s.pending); else await ask(); }
  catch (e) { toast(e.message); }
};
$("#restart").onclick = () => {
  S.session = null; S.challenge = S.mode === "challenge" ? null : S.challenge;
  $("#dialog").classList.add("hidden"); $("#start").classList.remove("hidden"); $("#caption").value = "";
  if (S.mode === "challenge") newChallenge();
  buildCollage();
};

// ------------------------------------------------------------------ finishing
function openFinish(path) {
  S.finishPath = path; S.rating = null;
  document.querySelectorAll("#stars button").forEach((b) => b.classList.remove("on"));
  $("#finish-img").src = img(path); $("#note").value = "";
  const s = S.session;
  if (s.target) {
    const hit = path === s.target;
    $("#finish-title").textContent = hit ? "🎯 Found it!" : "That's not the target";
    $("#finish-msg").textContent = hit ? `Found in ${s.turns.length} question${s.turns.length === 1 ? "" : "s"}.` : "You can keep searching, or finish and record this as a miss.";
    $("#finish-confirm").textContent = hit ? "Finish" : "Finish anyway";
  } else {
    $("#finish-title").textContent = "Is this your image?";
    $("#finish-msg").textContent = `Picked after ${s.turns.length} question${s.turns.length === 1 ? "" : "s"}.`;
    $("#finish-confirm").textContent = "Yes, finish";
  }
  $("#finish").showModal();
}
document.querySelectorAll("#stars button").forEach((b) => (b.onclick = () => {
  S.rating = +b.dataset.r; document.querySelectorAll("#stars button").forEach((x) => x.classList.toggle("on", +x.dataset.r <= S.rating));
}));
$("#finish").addEventListener("close", async () => {
  if ($("#finish").returnValue !== "ok") return;
  try {
    const s = await api(`/api/sessions/${S.session.session_id}/finish`, { found: S.finishPath, rating: S.rating, note: $("#note").value.trim() || null });
    render(s); $("#question-area").classList.add("hidden");
    toast("Thanks! Your dialogue was saved. Start a new search to contribute another.", 5000);
  } catch (e) { toast(e.message); }
});

init();
buildCollage();
