"""Do merchants blocked by extension_interaction_required carry checkout UI extensions
that completable merchants don't? Runs BOTH cohorts — the control is the whole point."""
import asyncio, json, sys, re, html
sys.path.insert(0, '/Users/pengchydan/dev/pivota-backend-quality-gate')
import httpx
from services import merchant_ucp_checkout as M

S='/private/tmp/claude-501/-Users-pengchydan-dev-pivota-backend-quality-gate/0abb1392-efc4-40a9-be1e-6b06a1897773/scratchpad'
UA={'user-agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36'}

async def audit(domain):
    out={'domain':domain}
    try:
        cat=await M._call_tool(domain,'search_catalog',{'meta':M.build_meta(),
            'catalog':{'query':'','context':{'address_country':'US','currency':'USD','language':'en-US'}}})
        vid=None
        for p in cat.get('products') or []:
            for v in p.get('variants') or []:
                a=v.get('availability')
                if isinstance(a,dict) and a.get('available') and (v.get('price') or {}).get('amount'):
                    vid=v['id']; break
            if vid: break
        if not vid:
            out['error']='no variant'; return out
        ck=await M.create_checkout(domain,line_items=[{'variant_id':vid,'quantity':1}],click_id='extaudit')
        async with httpx.AsyncClient(timeout=40,follow_redirects=True,headers=UA) as c:
            t=html.unescape((await c.get(ck['continue_url'])).text)
        # handle is the app's own extension name; version string carries the vendor
        exts=sorted(set(re.findall(r'ui_extension/handle/([A-Za-z0-9_-]+)/version/([A-Za-z0-9_.-]+)', t)))
        gws=sorted(set(n for k,n in re.findall(r'PaymentsPartners::Entities::(\w+)/\d+","name":"([^"]+)"', t)))
        out['extensions']=[f'{h} ({v})' for h,v in exts]
        out['gateways']=gws
    except Exception as e:
        out['error']=f'{type(e).__name__}: {e}'
    return out

async def main():
    fin=json.load(open(f'{S}/final.json'))
    blocked=[r['domain'] for r in fin if 'extension_interaction_required' in (r['messages'] or [])]
    ready=[r['domain'] for r in fin if r['checkout_status']=='ready_for_complete']
    sem=asyncio.Semaphore(4)
    async def run(d, cohort):
        async with sem:
            r=await audit(d); r['cohort']=cohort; return r
    res=await asyncio.gather(*[run(d,'blocked_on_extension') for d in blocked],
                             *[run(d,'ready_for_complete') for d in ready])
    json.dump(res, open(f'{S}/ext_audit.json','w'), indent=1)
    for cohort in ('blocked_on_extension','ready_for_complete'):
        rows=[r for r in res if r['cohort']==cohort]
        withext=[r for r in rows if r.get('extensions')]
        print(f"\n===== {cohort}: {len(withext)}/{len(rows)} carry a checkout UI extension =====")
        for r in sorted(rows,key=lambda x:x['domain']):
            e=r.get('extensions'); err=r.get('error')
            print(f"  {r['domain']:22s} {('; '.join(e) if e else ('ERR '+err if err else '— none —'))}")

asyncio.run(main())
