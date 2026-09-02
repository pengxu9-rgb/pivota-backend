#!/usr/bin/env python3
"""Cross-cohort comparison for the Revenue Recovery GEO spikes.

Every cohort is (brand, model, temperature, query-basis). Raw Tier-B rates are
only comparable within a shared basis; the portable metric across bases is the
branded/unbranded RATIO. This script keeps that distinction explicit.
"""
import json, math, collections, os, sys

def load(path, brand=None, model=None):
    if not os.path.exists(path): return []
    rows=[json.loads(l) for l in open(path) if l.strip()]
    seen={}
    for r in rows: seen[(r.get('brand'), r['query_id'], r['model'], r['run'])]=r
    v=list(seen.values())
    if brand: v=[r for r in v if r['brand']==brand]
    if model: v=[r for r in v if r['model']==model]
    return v

def rate(rows, t):
    s=[r for r in rows if r['tier']==t]
    return sum(1 for r in s if r['mentioned']=='Y'), len(s)

def z2(k1,n1,k2,n2):
    if not n1 or not n2: return 0.0
    p=(k1+k2)/(n1+n2); se=math.sqrt(p*(1-p)*(1/n1+1/n2))
    return ((k1/n1)-(k2/n2))/se if se else 0.0

H=os.path.expanduser('~/dev')
JOY=f'{H}/judydoll_joocyee_geo_2026-08-23/per_response_rows.jsonl'
C=[  # label, rows, basis, model, temp
 ('Judydoll',      load(JOY,'Judydoll','gemini'), 'joy-makeup',  '2.5-flash', 0),
 ('Joocyee',       load(JOY,'Joocyee','gemini'),  'joy-makeup',  '2.5-flash', 0),
 ('Flower Knows',  load(f'{H}/flower_knows_geo_temp0/per_response_rows.jsonl'), 'joy-makeup','2.5-flash',0),
 ('Flower Knows',  load(f'{H}/flower_knows_geo_g3/per_response_rows.jsonl'),    'joy-makeup','3-flash-preview',0),
 ('Anua',          load(f'{H}/anua_geo_g3/per_response_rows.jsonl'),            'skincare',  '3-flash-preview',0),
 ('Pixi',          load(f'{H}/pixi_geo_g3/per_response_rows.jsonl'),            'skincare',  '3-flash-preview',0),
]
C=[c for c in C if c[1]]

print("="*96)
print("MENTION RATE BY TIER   (raw rates comparable only WITHIN a basis)")
print("="*96)
print(f"{'Brand':<15}{'basis':<12}{'model':<18}{'A brand':>10}{'B unbrand':>11}{'C dupe':>10}{'D compare':>11}{'n':>6}")
for lbl,rows,basis,model,temp in C:
    cells=[]
    for t in 'ABCD':
        k,n=rate(rows,t); cells.append(f"{100*k/n:5.1f}%" if n else "  n/a")
    print(f"{lbl:<15}{basis:<12}{model:<18}{cells[0]:>10}{cells[1]:>11}{cells[2]:>10}{cells[3]:>11}{len(rows):>6}")

print("\n"+"="*96)
print("THE PORTABLE METRIC — branded : unbranded ratio  (comparable ACROSS bases)")
print("="*96)
for lbl,rows,basis,model,temp in C:
    ka,na=rate(rows,'A'); kb,nb=rate(rows,'B')
    ra=100*ka/na if na else 0; rb=100*kb/nb if nb else 0
    ratio=f"{ra/rb:5.1f}x" if rb else "  inf"
    bar='#'*min(40,int((ra/rb) if rb else 40))
    print(f"  {lbl:<14}{basis:<11}{model:<18} {ra:5.1f}% : {rb:5.1f}%   {ratio}  {bar}")

print("\n"+"="*96)
print("DESTINATION DISTRIBUTION, D3 (denominator = brand mentioned)")
print("="*96)
for lbl,rows,basis,model,temp in C:
    den=[r for r in rows if r['mentioned']=='Y']
    if not den: continue
    c=collections.Counter(r.get('link_destination') or 'none' for r in den)
    print(f"  {lbl:<14}{model:<18} n={len(den):<4} " +
          "  ".join(f"{k} {100*v/len(den):.0f}%" for k,v in c.most_common()))

print("\n"+"="*96)
print("DESTINATION SHAPE  (all responses)")
print("="*96)
for lbl,rows,basis,model,temp in C:
    hc=[r.get('host_count') for r in rows if r.get('host_count') is not None]
    if not hc: print(f"  {lbl:<14}{model:<18} host_count not recorded (pre-instrumentation run)"); continue
    multi=sum(1 for x in hc if x>1); zero=sum(1 for x in hc if x==0)
    print(f"  {lbl:<14}{model:<18} multi-host {100*multi/len(hc):5.1f}%   zero-host {100*zero/len(hc):5.1f}%   mean {sum(hc)/len(hc):.2f}")

print("\n"+"="*96)
print("WITHIN-BASIS SIGNIFICANCE TESTS")
print("="*96)
def cmp(a,b,t,note=''):
    A=[c for c in C if c[0]==a[0] and c[3]==a[1]]; B=[c for c in C if c[0]==b[0] and c[3]==b[1]]
    if not A or not B: return
    k1,n1=rate(A[0][1],t); k2,n2=rate(B[0][1],t); z=z2(k1,n1,k2,n2)
    sig='SIGNIFICANT' if abs(z)>1.96 else 'not significant'
    print(f"  Tier {t}: {a[0]}/{a[1]} {100*k1/n1:.1f}% vs {b[0]}/{b[1]} {100*k2/n2:.1f}%   z={z:+.2f}  {sig} {note}")
for t in 'BC':
    cmp(('Anua','3-flash-preview'),('Pixi','3-flash-preview'),t,'[same skincare basis]')
for t in 'BC':
    cmp(('Flower Knows','3-flash-preview'),('Flower Knows','2.5-flash'),t,'[model generation]')
