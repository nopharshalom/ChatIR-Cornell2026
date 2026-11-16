"""Build the standalone paired first-question review from saved dialogues."""
from base64 import b64encode
from html import escape
import json
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
ASSESSMENTS = {
    35: ("Ambiguous answer", "Both questions ask about location. “Middle” has no clear reference in the image, and the injected caption only says tree branch. The rank gain alone gives little evidence of a useful new detail."),
    158: ("Vanilla asks about a distinguishing object", "Vanilla gets “narrowboat,” consistent with the long boat in the canal. The swap asks for the bridge type and gets the generic answer “bridge.”"),
    343: ("More specific answer", "The injected caption mentions a tray. Both questions ask about the surface, but “wooden tray” identifies the visible serving tray more precisely than “wood,” which the vanilla rewrite turns into a wooden table."),
    466: ("More specific question, uncertain answer", "The swap asks about the trick instead of the time of day. A still image supports that a stunt is underway, but the exact “kickflip” is hard to verify. The injected caption does not name the trick."),
    518: ("Caption detail becomes a question", "The injected caption names the dog’s color. The question elicits “black,” which is visible and distinguishes the target. Vanilla asks about the stuffed object already described."),
    526: ("Useful detail absent from the injected caption", "The swap asks shirt color and gets “blue,” consistent with the image. The injected caption mentions tennis but no color, so the question change could come from the broader prompt perturbation."),
    570: ("Useful caption detail goes unused", "The injected caption mentions brick. Vanilla asks about the building material and gets it, while the swap asks about the clock type and gets “clock tower.”"),
    814: ("Background question, uncertain answer", "The caption describes two men outside a house. The swap asks about the background. The photo shows a building and plants, but “garden” is an interpretation. Vanilla asks what the men are holding."),
    1655: ("Generic question after the swap", "The replacement caption repeats that the men are playing frisbee. The swap asks who they are and gets no new detail. Vanilla gets “tackling,” a more distinctive description, though intent is hard to infer from the frame."),
    1772: ("Caption detail becomes a question", "The target caption says the mouse pad is orange, which is visible. The swap asks about that color and preserves it in the rewrite. Vanilla adds the visible but less distinctive keyboard."),
    1874: ("Helpful answer despite noisy caption", "The swap gets “on the street,” consistent with the scene. The injected caption calls the large vehicle a train, while the original query calls it a bus. The new query keeps the original vehicle description."),
    2029: ("Context changes the question", "The injected caption mentions feeding an elephant, which vanilla already asked about. The swap asks about the background and adds “by a river.” Water is visible, though its exact type is uncertain."),
}


def load(arm):
    return {r["qid"]: r for r in (
        json.loads(p.read_text()) for p in (ROOT / "results" / arm / "dialogues").glob("*.json")
    )}


def first_hit(record):
    return next((i for i, rank in enumerate(record["ranks"]) if rank <= 10), None)


def column(label, record):
    turn = record["turns"][0]
    candidates = "".join(f"<li>{escape(q)}</li>" for q in turn["candidate_questions"])
    captions = "".join(
        f"<li{' class=swap' if i == turn.get('injected_slot') else ''}>{escape(c)}</li>"
        for i, c in enumerate(turn["selected_captions"])
    )
    hit = first_hit(record)
    return f"""<div class="arm"><h3>{label}</h3>
    <p><strong>Rank {record['ranks'][0]} → {record['ranks'][1]}</strong> · First hit {hit if hit is not None else 'outside 10 rounds'}</p>
    <p><b>Asked</b> {escape(turn['question'])}</p><p><b>Answer</b> {escape(turn['answer'])}</p>
    <p><b>Rewrite</b> {escape(record['queries'][1])}</p>
    <details open><summary>Candidate questions</summary><ol>{candidates}</ol></details>
    <details><summary>Captions in the prompt</summary><ol>{captions}</ol></details></div>"""


def build():
    vanilla, target = load("vanilla"), load("target_caption")
    cases = {i for i in vanilla if (first_hit(vanilla[i]) == 1) != (first_hit(target[i]) == 1)}
    assert cases == ASSESSMENTS.keys()
    sections = []
    with ZipFile(ROOT / "agentir_full_dialogue_bundle_v2.zip") as archive:
        captions = {row["id"]: row["caption"][0] for row in json.loads(
            archive.read("Protocol/visdial_captions.json"))}
        for i in sorted(cases, key=lambda i: (first_hit(target[i]) != 1, i)):
            v, t = vanilla[i], target[i]
            assessment, detail = ASSESSMENTS[i]
            pic = b64encode(archive.read("targets/" + v["target"])).decode()
            call = "Target-caption hit" if first_hit(t) == 1 else "Vanilla hit"
            sections.append(f"""<section id="case-{i}"><h2>Dialogue {i} · {call}</h2>
                <div class="image-row"><img src="data:image/jpeg;base64,{pic}" alt="Target image for dialogue {i}">
                <div><p><b>Original query</b> {escape(v['queries'][0])}</p>
                <p><b>Stored target caption</b> {escape(captions[v['target']])}</p>
                <p><b>{escape(assessment)}.</b> {escape(detail)}</p></div></div>
                <div class="pair">{column('Vanilla PlugIR',v)}{column('Target caption guaranteed',t)}</div>
                <label for="note-{i}">Your notes</label><textarea id="note-{i}" data-case="{i}" rows="3"></textarea></section>""")
    nav = " ".join(f'<a href="#case-{i}">{i}</a>' for i in sorted(cases))
    html = """<!doctype html><html lang="en"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>PlugIR paired dialogue review</title>
    <style>
    *{box-sizing:border-box}body{margin:0;background:#f0f4f7;color:#142438;font:16px/1.5 system-ui,sans-serif}
    main{max-width:1120px;margin:auto;padding:24px}section,.arm{border:1px solid #d5dfe7;border-radius:12px;padding:20px;background:white}
    section{margin:22px 0}h2,h3{margin:0 0 12px}p{margin:8px 0}a{color:#165b79}
    nav a{display:inline-block;margin:4px}summary,label{font-weight:650}details{margin:12px 0}
    .image-row,.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:14px 0}
    img{max-width:100%;max-height:280px;object-fit:contain;justify-self:center}.swap{background:#fff1b8}
    textarea{display:block;width:100%;margin-top:6px;padding:10px;border:1px solid #8399ad;border-radius:5px;font:inherit}
    button{font:inherit;padding:6px 12px;margin-top:12px}@media(max-width:680px){.image-row,.pair{grid-template-columns:1fr}}
    </style><main><h1>Where target-caption exposure changed the first hit</h1>
    <p>Same 200 dialogues, Qwen3.5-9B. Among the 62 initial misses, vanilla found 13 after one question and target-caption exposure found 19. The 12 cases below account for the difference: 9 hits unique to the target-caption run and 3 unique to vanilla. The target was in the initial candidate pool in all 12.</p>
    <p>Ranks are before and after the first answer. The injected caption is highlighted in the prompt list. Image-based assessments flag details that are hard to verify. Notes save in this browser where local storage is available. Export a copy to share or keep.</p>
    <button id="export">Download your notes</button>
    """
    html += f"<nav aria-label='Jump to dialogue'>{nav}</nav>" + "".join(sections)
    html += """<script>
    const notes = [...document.querySelectorAll('textarea')];
    for (const field of notes) {
      const key = 'agentir-caption-review-' + field.dataset.case;
      try { field.value = localStorage.getItem(key) || ''; } catch {}
      field.addEventListener('input', () => { try { localStorage.setItem(key,field.value); } catch {} });
    }
    document.querySelector('#export').onclick = () => {
      const content = notes.filter(f => f.value.trim()).map(f => 'Dialogue ' + f.dataset.case + '\\n' + f.value).join('\\n\\n');
      const url = URL.createObjectURL(new Blob([content],{type:'text/plain'}));
      const link = document.createElement('a');
      link.href=url;link.download='plugir-dialogue-notes.txt';link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    </script></main></html>"""
    (ROOT / "Dialogue_Review.html").write_text(html)


if __name__ == "__main__":
    build()
