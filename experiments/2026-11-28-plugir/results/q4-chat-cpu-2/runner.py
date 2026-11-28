"""CPU diagnostic. JSON lines in/out. Target pixels accessible to the answerer only."""
from pathlib import Path
import sys,json,re
import torch
from PIL import Image
from transformers import Blip2Processor,Blip2ForConditionalGeneration
here=Path(__file__).resolve().parent
project=here.parents[1]
repo=project/'ChatIR-Cornell2026'
sys.path.insert(0,str(repo/'scripts'))
from run_probe_pool_sweep import cpu_retriever,INPUTS,shared
from run_matan_400 import build_grid
torch.set_grad_enabled(False)
torch.set_num_threads(3)
scorer=cpu_retriever()
_,emb=torch.load(INPUTS/'cache/corpus_blip.pth',map_location='cpu',weights_only=True)
space=json.loads((INPUTS/'Protocol/Search_Space_val_50k.json').read_text())
caps=json.loads((INPUTS/'Protocol/visdial_captions.json').read_text())
snapshot=next((repo/'cache/huggingface/hub/models--Salesforce--blip2-flan-t5-xl/snapshots').iterdir())
proc=Blip2Processor.from_pretrained(snapshot,local_files_only=True)
model=Blip2ForConditionalGeneration.from_pretrained(snapshot,torch_dtype=torch.float16,low_cpu_mem_usage=True,local_files_only=True).eval()
def vqa(name,q):
    with Image.open(repo/'data'/name) as pic:
        inputs=proc(images=pic.convert('RGB'),text='Question: '+q+' Answer:',return_tensors='pt').to('cpu',torch.float16)
    tokens=model.generate(**inputs,max_new_tokens=12,do_sample=False)
    return proc.batch_decode(tokens,skip_special_tokens=True)[0].strip()
states={}
def start():
    for i in [742,1071]:
        r=json.loads((project/f'output/mmr-round4-visual/images_only/dialogues/{i}.json').read_text())
        for arm in ['cap','img']:
            states[f'{i}-{arm}']=dict(query=r['queries'][4],name=r['target'],ranks=r['ranks'][:5],turns=[],prior=[(t['question'],t['answer']) for t in r['turns'][:4]])
def observe():
    result={}
    for key,s in states.items():
        n=len(s['ranks'])
        if s['ranks'][-1]<=10 or n>10:continue
        scores=scorer([s['query']])[0]
        order=scores.argsort().flip(0)
        ranks=shared.inverse_ranks(order)
        assert int(ranks[space.index(s['name'])])==s['ranks'][-1]
        pool=order[:500-50*(n-1)]
        ids=shared.history_mmr(pool,ranks,emb,count=10,weight=.85)
        info=dict(query=s['query'],round=n,asked=[x['question'] for x in s['turns']])
        if n==5:info['existing history']=s['prior']
        if key.endswith('cap'):
            info['captions']=[caps[j]['caption'][0] for j in ids]
        else:
            grid=here/f'{key}-{n}.jpg'; build_grid([repo/'data'/space[j] for j in ids],grid)
            info['grid']=str(grid)
        result[key]=info
    return result
def step(proposals):
    for key,options in proposals.items():
        assert len(options)==5 and len({q for q,clause in options})==5 and all('{}' in t for q,t in options)
        s=states[key]; n=len(s['ranks'])
        assert 5<=n<=10 and s['ranks'][-1]>10
        query=s['query']; scores=scorer([query]+[query+', '+q for q,t in options])
        subset=scores[0].argsort()[-(500-50*(n-1)):-10]
        logp=scores[:,subset].log_softmax(1)
        kl=(logp[0].exp()*(logp[0]-logp[1:])).sum(1)
        ix=int(kl.argmin());q,template=options[ix]
        answer=vqa(s['name'],q)
        new=query.rstrip(' .')+'. '+template.format(answer).strip().rstrip('.')+'.'
        rank=int(shared.inverse_ranks(scorer([new])[0].argsort().flip(0))[space.index(s['name'])])
        s['query']=new;s['ranks'].append(rank)
        s['turns'].append(dict(question=q,answer=answer,rank=rank,options=options))
    (here/'results.json').write_text(json.dumps(states,indent=2))
    return {k:dict(q=s['turns'][-1]['question'],a=s['turns'][-1]['answer']) for k,s in states.items() if s['turns']}
start()
print('READY',flush=True)
for line in sys.stdin:
    req=json.loads(line)
    if req=='stop':break
    if req=='batch':req=json.loads((here/'batch.json').read_text())
    out=observe() if req=='observe' else step(req)
    print(json.dumps(out),flush=True)
