"""Select one grounded question at a shared dialogue state without access to the target."""
from collections import Counter
import hashlib
import json
import re

from torch.nn.functional import normalize
import enrichment_evidence as ev

ENTITIES = """Extract up to six concrete subjects a person can point to in this photograph.
Copy short noun phrases verbatim from the initial description or a user's actual answer.
Use distinguishing descriptions when there are multiple similar people or objects. Return
a JSON list of strings. Do not name attributes, invent objects, or take a noun from a question."""
SCOUT = """Nominate up to three different observable, unresolved contrasts among plausible photos.
Refer to a subject ONLY by its numeric ID. Consider the ordinary proposed questions and the
captions, including details missing from them. A question must be understandable from one photo.
Use exactly a supplied family and 2 to 6 disjoint concrete labels suitable to insert in its
statement. Skip uncertain identity, guessed intent and invisible facts. Return a JSON list of
{"id":1,"family":"material","values":["wood","metal"]} objects. Never re-ask a prior question."""
HISTORY = """A searcher sees only their own photograph. Read the initial description and all
previous questions and answers. Try to answer the proposed question using that text alone,
including implicit facts and synonyms. Check whether this was previously asked, even if the
answer was vague. Return JSON {"status":"KNOWN|ASKED|UNSAFE|OPEN","evidence":"short reason"}.
Use OPEN only for an unasked, unanswered, directly visible distinction about an unambiguous
subject in the user's photograph, with distinct non-overlapping answer choices. Never guess."""
HISTORY_ANSWER = """Answer the question from the searcher's initial description and past Q&A
alone. Use only facts explicitly stated or clearly entailed for this particular subject. If the
answer is unavailable, reply exactly UNKNOWN. Otherwise give the short answer. You see no
candidate photos or candidate descriptions."""
LABELS = """Answer each numbered question independently from the one evidence item provided.
Use one exact allowed label for each question, or unknown if absent, uncertain, obscured,
ambiguous, or outside the choices. Do not use stereotypes. Missing information never establishes
absence. Return a JSON object mapping question IDs to labels."""
PICK = """Choose the most useful next question for someone who sees only their own target photo.
Use the dialogue and ten possible photos' descriptions to choose a distinction among plausible
targets. The entries may include supported details. Use question 0 (the usual question) when
another question is not better. Return only the integer ID. Never write a new question."""
FAMILY = {
    'color': r'colou?r|hue|shade',
    'material': r'material|made (?:of|out of|from)|composed of',
    'surface': r'surface|terrain|standing on|sitting on|resting on|ground',
    'holding': r'hold(?:s|ing)?|carrying|in (?:his|her|their) hands?',
    'clothing': r'wear(?:s|ing)?|clothing|dressed|outfit|upper body',
    'setting': r'where|setting|location|kind of place|located',
    'activity': r'doing|activity|action|occupation',
}
ATTRS = {
    'color': 'red blue green brown gray grey black white pink purple orange yellow silver tan beige gold golden turquoise multicolored'.split(),
    'material': 'stone brick wood wooden steel metal metallic plastic concrete glass leather fabric marble ceramic iron cement'.split(),
}
STOP = set('the a an and of in with on at to by near next other their upper large small middle front back side'.split())
ALIASES = {'lorry':'truck','lorries':'truck','automobile':'car','automobiles':'car','grey':'gray',
           'wooden':'wood','metallic':'metal','computers':'computer','men':'man','women':'woman',
           'children':'child','buses':'bus','sandy':'sand','stonemasonry':'stone'}
BASIC = ('captions_annotations','pixels_annotations','captions_retrieval','pixels_retrieval','blind_alternative')


def tokens(text):
    out = []
    for word in ev.norm(text).split():
        word = ALIASES.get(word,word)
        if len(word)>3 and word.endswith('s') and not word.endswith(('ss','us')):
            word = word[:-1]
        out.append(word)
    return out


def memory_veto(slot, history):
    entity, family = slot['entity'], slot['family']
    anchors = set(tokens(entity)) - STOP - set(ATTRS['color']) - set(ATTRS['material'])
    if not anchors:
        return 'ambiguous subject'
    values = {tuple(tokens(v)) for v in slot['values']+ATTRS.get(family,[])}
    if len({tuple(tokens(v)) for v in slot['values']}) != len(slot['values']):
        return 'synonymous labels'
    qkey = ev.norm(slot['question'])
    for line in history[1:]:
        q, _, answer = line.partition('?')
        if ev.norm(q)==qkey:
            return 'same previous question'
        if anchors.intersection(tokens(q)) and re.search(r'\b(?:'+FAMILY[family]+r')\b',q,re.I):
            return 'previous question about this subject and attribute'
        if anchors.intersection(tokens(q)) and tuple(tokens(answer)) in values:
            return 'previous answer gave this value'
    # Require a local grammatical link. A red umbrella *beside* a truck does not make the truck red.
    for text in [history[0],*(x.partition('?')[2] for x in history[1:])]:
        words = tokens(text)
        for j,word in enumerate(words):
            if word not in anchors:
                continue
            for value in values:
                n = len(value)
                if n and j >= n and tuple(words[j-n:j])==value:
                    return 'attribute explicitly modifies subject'
                for verb in (('is',),('are',),('is','made','of'),('is','made','from'),
                             ('is','on'),('is','wearing'),('is','holding'),('is','in')):
                    after = words[j+1:]
                    if tuple(after[:len(verb)])==verb and tuple(after[len(verb):len(verb)+n])==value:
                        return 'explicit subject predicate'
    return None


def make_menu(audit, history, caps, proposals):
    statements = [history[0],*(line.partition('?')[2] for line in history[1:])]
    raw = ev.json_value(audit.ask(ENTITIES,statements,100))
    entities = []
    for name in raw if isinstance(raw,list) else []:
        if isinstance(name,str):
            name = re.sub(r'^(an?|the)\s+','',ev.norm(name))
            if (name and name not in entities and len(name.split())<=7 and not ev.BAD.search(name)
                    and any(' '+name+' ' in ' '+ev.norm(s)+' ' for s in statements)):
                entities.append(name)
    if not entities:
        return [], ['no grounded subjects']
    payload = dict(subjects=dict(enumerate(entities,1)),dialogue=history,captions=caps,
                   proposals=proposals,templates=ev.FORMS)
    raw = ev.json_value(audit.ask(SCOUT,payload,280))
    slots, reasons, seen = [], [], set()
    for item in raw[:3] if isinstance(raw,list) else []:
        if not isinstance(item,dict) or type(item.get('id')) is not int or not 1<=item['id']<=len(entities):
            reasons.append('invalid subject ID')
            continue
        slot = ev.parse_slot(json.dumps({**item,'entity':entities[item['id']-1]}),history)
        if not slot or slot['question'] in seen:
            reasons.append('invalid or duplicate nomination')
            continue
        seen.add(slot['question'])
        reason = memory_veto(slot,history)
        if not reason:
            answer = audit.ask(HISTORY_ANSWER,dict(dialogue=history,question=slot['question']),24)
            if ev.norm(answer) != 'unknown':
                reason = 'answerable from dialogue alone: '+answer
        if not reason:
            check = ev.json_value(audit.ask(HISTORY,dict(history=history,slot=slot),100))
            if not isinstance(check,dict) or check.get('status')!='OPEN':
                reason = 'dialogue-only check: '+str(check)
        if reason:
            reasons.append(reason)
        else:
            slots.append(slot)
        if len(slots)==2:
            break
    return slots, reasons or ([] if slots else ['no valid nomination'])


def label_one(audit, slots, captions=None, image=None):
    tasks = {str(k):dict(question=s['question'],allowed=s['values']) for k,s in enumerate(slots,1)}
    payload = dict(questions=tasks)
    if image is None:
        payload['caption'] = captions
    instruction = LABELS + (' Use only explicitly stated caption evidence.' if image is None else ' Inspect this single photo.')
    raw = ev.json_value(audit.ask(instruction,payload,65,image=image))
    def valid(value,slot):
        return isinstance(value,str) and (ev.norm(value)=='unknown' or ev.parse_label(value,slot['values']) is not None)
    if (not isinstance(raw,dict) or set(raw)!=set(tasks)
            or not all(valid(raw[str(k)],s) for k,s in enumerate(slots,1))):
        # Avoid treating a protocol failure as evidence that captions omit a detail.
        labels = []
        for slot in slots:
            q = dict(question=slot['question'],allowed=slot['values'])
            if image is None:
                q['caption'] = captions
            one = audit.ask(ev.LABEL+' Return one exact label or unknown.',q,18,image=image)
            if not valid(one,slot):
                raise ValueError('Invalid classification on both structured and single-label attempts')
            labels.append(ev.parse_label(one,slot['values']))
        return labels
    return [ev.parse_label(raw[str(k)],s['values']) for k,s in enumerate(slots,1)]


def transfer(pipe, ids, query, slot, labels):
    if not ev.replicated(labels):
        return None
    photos = pipe.img_embs[ids]
    def scores(text):
        return (normalize(pipe.get_text_features(text),dim=-1) @ photos.T).flatten()
    old = scores(query)
    shifts = {}
    for value,n in Counter(labels).items():
        if value and n>=2:
            fact = ev.FORMS[slot['family']][1].format(e=slot['entity'],v=value)
            shifts[value] = (scores(query+' '+fact)-old).detach().cpu().tolist()
    return ev.transfer_score(labels,shifts)


def choices(audit, history, query, ids, caps, baseline, proposals, blind_key):
    slots, skips = make_menu(audit,history,caps,proposals)
    diagnostic = dict(slots=slots,vetoes=skips)
    if not slots:
        return None, diagnostic
    assert len(caps)==len(ids)==len(set(ids))==10
    base, filled = [], []
    for image,cap in zip(ids,caps):
        try:
            text = label_one(audit,slots,captions=cap)
        except ValueError as exc:
            diagnostic['protocol_error']=str(exc)
            return None,diagnostic
        labels = text.copy()
        missing = [j for j,v in enumerate(text) if v is None]
        if missing:
            try:
                answers = label_one(audit,[slots[j] for j in missing],image=image)
            except ValueError as exc:
                diagnostic['protocol_error']=str(exc)
                return None,diagnostic
            for j,v in zip(missing,answers):
                labels[j] = v
        base.append(text)
        filled.append(labels)
    def columns(rows):
        return [list(v) for v in zip(*rows)]
    cap_cols, pix_cols = columns(base), columns(filled)
    evidence_added = [sum(a is None and b is not None for a,b in zip(c,p))
                      for c,p in zip(cap_cols,pix_cols)]
    qualified = [ev.replicated(p) and n>=2 for p,n in zip(pix_cols,evidence_added)]
    diagnostic.update(caption_labels=cap_cols,pixel_labels=pix_cols,filled=evidence_added,
                      pixel_supported=qualified)
    if not any(qualified):
        return None,diagnostic
    qs = [baseline,*(s['question'] for s in slots)]
    alternatives = [i for i,q in enumerate(qs[1:],1) if ev.norm(q)!=ev.norm(baseline)]
    if not alternatives:
        return None,diagnostic
    blind_index = int.from_bytes(hashlib.sha256(blind_key.encode()).digest()[:8],'big')
    picks = {'blind_alternative':alternatives[blind_index%len(alternatives)]}
    for source,cols in (('captions',cap_cols),('pixels',pix_cols)):
        # All branches get the same menu, template, ordinary question, and opportunity to switch.
        annotated = list(caps)
        for slot,col in zip(slots,cols):
            annotated = ev.annotated(annotated,dict(slot=slot,labels=col))
        raw = audit.ask(PICK,dict(dialogue=history,candidate_descriptions=annotated,
                                  questions=dict(enumerate(qs))),12)
        idx = raw.strip().rstrip('.')
        picks[source+'_annotations'] = int(idx) if idx in map(str,range(len(qs))) else 0
        scores = [transfer(audit.pipe,ids,query,s,c) for s,c in zip(slots,cols)]
        eligible = [j for j,score in enumerate(scores,1) if score is not None and score>=2/3]
        picks[source+'_retrieval'] = max(eligible,key=lambda j:scores[j-1]) if eligible else 0
        diagnostic[source+'_transfer'] = scores
    return dict(questions=qs,picks=picks),diagnostic
