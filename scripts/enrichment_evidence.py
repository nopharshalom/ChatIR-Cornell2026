"""Identify an unresolved attribute and measure whether candidate evidence supports asking it."""
from collections import Counter
from itertools import combinations
import hashlib
import json
from pathlib import Path
import re

import torch
from torch.nn.functional import normalize

# The templates never expose a candidate, an answer choice, or an assumed attribute value.
FORMS = {
    'color': ('What is the main color of the {e}?', 'The main color of the {e} is {v}.'),
    'material': ('What material is the {e} made of?', 'The {e} is made of {v}.'),
    'surface': ('What surface is the {e} on?', 'The {e} is on {v}.'),
    'holding': ('What, if anything, is the {e} holding?', 'The {e} is holding {v}.'),
    'clothing': ('What is the {e} wearing on their upper body?', 'The {e} is wearing {v} on their upper body.'),
    'setting': ('What kind of place is the {e} in?', 'The {e} is in {v}.'),
    'activity': ('What is the {e} doing?', 'The {e} is {v}.'),
}
SCOUT = """Choose at most one unresolved, visually observable distinction about the user's own photo.
Ground it in a short, unambiguous noun phrase quoted from their initial description or an actual
answer. Do not introduce an object from just a candidate caption or a prior question.
Check the full dialogue for the same distinction, including synonyms and previously unanswered
questions. Skip if it has been asked, answered, is already implied, is subjective, or would
need context outside this photo. Use the candidates to suggest likely alternatives. A missing
caption detail can still be worth checking. Prefer a distinction between plausible photos
that would change how you describe/search for the picture. Use one of the supplied templates.
Return NONE or JSON {"entity":"noun phrase without leading article","family":"template key",
"values":["label A","label B", ...]}. Give 2 to 6 mutually exclusive concrete answers at
the same level of detail, suitable to insert verbatim in the supplied declarative sentence.
Do not offer synonyms as separate labels. For holding, 'nothing' is possible only if hands are
visible. Never use vague other/unknown as a label. If uncertain about the referent use NONE."""
SCREEN = """You see only the user's initial description and completed dialogue. Is the proposed
question natural and neutrally phrased about a uniquely identifiable subject in their own
photo, applicable to that subject, directly visible, still unanswered and not previously
asked even with different wording? Reject false premises, underspecified multiple people or
objects, implied answers, and activities that require guessing intent. The labels must
represent distinct, non-overlapping plausible answers. Return exactly PASS or SKIP."""
LABEL = """Classify the answer to the supplied question using ONLY the provided evidence.
Use exactly one listed label, or unknown. Use unknown if evidence is missing, ambiguous,
unreadable, contradictory, several labels apply, the subject cannot be identified, or the
answer falls outside the list. Never infer the answer from stereotypes or from what is typical.
An unmentioned object is not evidence of absence. Use 'nothing' only if explicitly supported."""
BAD = re.compile(r"\b(grid|tile|candidate|collage|thumbnail|user|target|photo|image|picture|someone|something|it|they|he|she)\b", re.I)
WORD = re.compile(r"[a-z0-9]+(?:['-][a-z0-9]+)*")
EMPTY = {'unknown','unclear','other','unsure','none of the above','n/a','not visible','cannot tell'}


def norm(s):
    return ' '.join(WORD.findall(s.casefold().replace('’', "'")))


def json_value(raw):
    try:
        return json.loads(re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', raw.strip(), flags=re.I))
    except (TypeError, ValueError):
        return None


def parse_slot(raw, history):
    obj = json_value(raw)
    if not isinstance(obj, dict):
        return None
    entity, family, values = (obj.get(k) for k in ('entity','family','values'))
    if not isinstance(entity,str) or not isinstance(family,str) or family not in FORMS:
        return None
    entity = re.sub(r'^(?:the|a|an)\s+', '', norm(entity))
    user_words = [history[0], *(line.partition('?')[2] for line in history[1:])]
    if (not 1 <= len(entity.split()) <= 7 or BAD.search(entity)
            or not any(' '+entity+' ' in ' '+norm(t)+' ' for t in user_words)):
        return None
    if not isinstance(values,list) or not 2 <= len(values) <= 6 or not all(isinstance(v,str) for v in values):
        return None
    values = [norm(v) for v in values]
    if (len(set(values)) != len(values) or set(values) & EMPTY
            or any(not 1 <= len(v.split()) <= 5 or BAD.search(v) for v in values)):
        return None
    return dict(entity=entity, family=family, values=values, question=FORMS[family][0].format(e=entity))


def parse_label(raw, values):
    label = norm(raw.strip(' "'))
    return label if label in values else None


def replicated(labels):
    counts = Counter(x for x in labels if x)
    return sum(counts.values()) >= 6 and sum(n >= 2 for n in counts.values()) >= 2


def transfer_score(labels, shifts):
    # For each contrast, adding answer A should favor A over B, and adding B should
    # favor B over A. Use within-image score changes to remove baseline relevance.
    groups = {v:[i for i,x in enumerate(labels) if x == v]
              for v,n in Counter(labels).items() if v and n >= 2}
    rates = []
    for a,b in combinations(groups,2):
        for good,bad in ((a,b),(b,a)):
            s = shifts[good]
            pairs = [(s[i],s[j]) for i in groups[good] for j in groups[bad]]
            rates.append(sum((x > y) + 0.5*(x == y) for x,y in pairs)/len(pairs))
    return sum(rates)/len(rates) if rates else None


QUESTION_CHECK = """Check a proposed question for a searcher looking only at their own photo.
Use their initial description and real answers to establish the referent. PASS only if the
question is standalone, neutrally phrased, visually answerable, has a clear subject, and asks
for something that has not already been discussed. SKIP a reference to candidate images or
an assumed object, value, intention, or identity. Return only PASS or SKIP."""


def standalone(question):
    return (question.count('?') == 1 and question.endswith('?') and '\n' not in question
            and re.match(r'^(what|which|how|where|who|is|are|does|do|can|has|have)\b', question, re.I)
            and not re.search(r'\b(grid|tile|panel|candidate|collage|thumbnail)s?\b|'
                              r'\bwhich (?:of (?:the|these) )?(?:image|photo|picture)s?\b|'
                              r'\b(image|photo|picture)\s*#?\s*\d',question,re.I))


def annotated(captions, obs):
    slot = obs['slot']
    pattern = FORMS[slot['family']][1]
    return [cap+' '+pattern.format(e=slot['entity'],v=value) if value else cap
            for cap,value in zip(captions,obs['labels'])]


class Audit:
    def __init__(self, pipe, cache, image_root, revision, write):
        self.pipe, self.cache, self.root = pipe, cache, image_root
        self.revision, self.write = revision, write

    def ask(self, instruction, payload, tokens, image=None):
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        name = self.pipe.search_space[image] if image is not None else None
        key = json.dumps([self.revision, instruction, content, tokens, name])
        path = self.cache/(hashlib.sha256(key.encode()).hexdigest()+'.json')
        if path.exists():
            return json.loads(path.read_text())['response']
        worker = self.pipe.client
        # MacQwen normally applies the dialogue seed to the first chat request. Keep it for
        # the original proposal generator. These additional requests use greedy decoding.
        pending, worker.next_seed = worker.next_seed, None
        try:
            raw = worker.complete([dict(role='system',content=instruction),
                                   dict(role='user',content=content)], tokens, temperature=0,
                                  image=self.root/name if name else None)
        finally:
            worker.next_seed = pending
        self.write(path, dict(request=json.loads(key), response=raw))
        return raw

    def inspect(self, source, ids, captions, query, history):
        if len(ids) != 10 or len(set(ids)) != 10 or len(captions) != 10:
            raise ValueError('Expected ten distinct aligned representatives')
        raw = self.ask(SCOUT, dict(dialogue=history, captions=captions, templates=FORMS), 180)
        slot = parse_slot(raw, history)
        report = dict(slot=slot, scout=raw, evidence_ids=list(ids), reason='no_grounded_slot')
        if not slot:
            return report
        if self.ask(SCREEN, dict(dialogue=history, slot=slot), 10).strip().upper().rstrip('.') != 'PASS':
            report['reason'] = 'ambiguous_or_previously_resolved'
            return report
        task = dict(question=slot['question'], allowed_labels=slot['values'])
        keys = [str(i) for i in range(1,len(ids)+1)]
        raw = self.ask(LABEL + ' Return a JSON object keyed by the supplied caption numbers. '
                       'Read each caption independently, using only what it explicitly says.',
                       dict(task=task,captions=dict(zip(keys,captions))), 260)
        by_id = json_value(raw)
        if not isinstance(by_id,dict) or set(by_id) != set(keys):
            # Never align a malformed array by position. Recover by reading one caption at a time.
            by_id = {}
        cap = []
        for key,caption in zip(keys,captions):
            value = by_id.get(key)
            if not isinstance(value,str):
                value = self.ask(LABEL+' Return one label for this caption only.',
                                 dict(task=task,caption=caption),14)
            cap.append(parse_label(value,slot['values']))
        labels = cap.copy()
        report['caption_labels'] = cap
        if source == 'pixels':
            # Supplement only omissions. Retain explicit caption facts in both conditions.
            for j,i in enumerate(ids):
                if labels[j] is None:
                    raw = self.ask(LABEL+' You see a single photo. Return a single label.',task,14,image=i)
                    labels[j] = parse_label(raw,slot['values'])
        report['labels'] = labels
        report['filled'] = sum(c is None and v is not None for c,v in zip(cap,labels))
        if not replicated(labels):
            report['reason'] = 'insufficient_supported_contrast'
            return report
        report['reason'] = 'supported'
        # Check whether our *actual retriever* responds to this difference in the right direction.
        photos = self.pipe.img_embs[ids]
        def scores(text):
            q = normalize(self.pipe.get_text_features(text),dim=-1)
            return (q @ photos.T).flatten()
        prior = scores(query)
        shifts = {}
        for v,n in Counter(labels).items():
            if v and n >= 2:
                statement = FORMS[slot['family']][1].format(e=slot['entity'],v=v)
                shifts[v] = (scores(query+' '+statement)-prior).detach().cpu().tolist()
        report['transfer'] = transfer_score(labels,shifts)
        return report
