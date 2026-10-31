#!/usr/bin/env python3
"""Read-only: families that share a partner where one side is missing or the pair repeats — usually a
duplicate created when a proposal could not find the existing family. Reads data/bundle.json; no login."""
import json,os,collections
B=json.load(open(os.path.expanduser('~/gramps-tools/family-site/data/bundle.json')))
P,F,E=B['people'],B['families'],B['events']
nm=lambda i:(P.get(i,{}).get('name',{}).get('display') or i) if i else '—'
by=collections.defaultdict(list)
for fid,f in F.items():
    for k in ('father','mother'):
        if f.get(k): by[(k,f[k])].append(fid)
seen=set()
for (k,pid),fids in sorted(by.items()):
    if len(fids)<2: continue
    partners={F[x].get('mother' if k=='father' else 'father') for x in fids}
    if len(partners)<len(fids) or None in partners:
        key=tuple(sorted(fids))
        if key in seen: continue
        seen.add(key)
        print(f"{nm(pid)} ({pid}) is {k} in:")
        for x in fids:
            f=F[x]; evs=[E[e['ref']]['type']+' '+str((E[e['ref']].get('date') or {}).get('text','')) for e in f.get('events',[]) if e['ref'] in E]
            print(f"   {x}: with {nm(f.get('mother' if k=='father' else 'father'))}; children {len(f.get('children',[]))}; events {evs}")
