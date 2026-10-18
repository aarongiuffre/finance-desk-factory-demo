
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edgar_docparse as dp

fix = json.load(open('/data/finance/logs/primary_fixlist.json'))
RAW = '/data/finance/raw/edgar'
OUT = '/data/finance/parsed'
LOG = open('/data/finance/logs/reparse_primary.log', 'a', buffering=1)

# group by ticker, keep order
by_t = {}
for t, form, acc, cur, new in fix:
    by_t.setdefault(t, []).append((form, acc, new))

ciks = {}
def cik_of(t):
    if t not in ciks:
        try:
            ciks[t] = json.load(open(f'{RAW}/{t}/submissions.json'))['cik']
        except Exception:
            ciks[t] = None
    return ciks[t]

n_done = n_err = 0
t0 = time.time()
total = sum(len(v) for v in by_t.values())
for t, items in by_t.items():
    cik = cik_of(t)
    for form, acc, new in items:
        date, accdash = acc.split('_', 1)
        rawd = f'{RAW}/{t}/{form}/{date}-{accdash.replace("-", "")}'
        outdir = f'{OUT}/{t}/{form}/{acc}'
        try:
            meta, err = dp.parse_filing(rawd, t, form, date, accdash, cik)
            if meta:
                n_done += 1
            else:
                n_err += 1
                LOG.write(f'ERR {t} {form} {acc}: {err}\n')
        except Exception as e:
            n_err += 1
            LOG.write(f'EXC {t} {form} {acc}: {type(e).__name__}: {e}\n')
        if (n_done + n_err) % 250 == 0:
            el = time.time() - t0
            LOG.write(f'PROG {n_done+n_err}/{total} done={n_done} err={n_err} {el:.0f}s\n')
LOG.write(f'FINISH done={n_done} err={n_err} total={total} {time.time()-t0:.0f}s\n')
print('FINISH', n_done, n_err, total)
