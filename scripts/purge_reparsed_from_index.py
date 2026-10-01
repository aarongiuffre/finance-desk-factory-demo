
import sys
import lancedb
DB='/data/finance/index/lancedb'; TABLE='filings'
import json
fix=json.load(open('/data/finance/logs/primary_fixlist.json'))
# parsed dir names: <DATE>_<ACCN-dash>; accession in metadata = dash form
accs=[a.split('_',1)[1] for _t,_f,a,_c,_n in fix]
print('accessions to purge:', len(accs), 'unique:', len(set(accs)))
db=lancedb.connect(DB)
tbl=db.open_table(TABLE)
n=tbl.count_rows()
print('rows before:', n)
# lancedb delete with IN filter — batch to keep filters manageable
B=500
tot=0
for i in range(0,len(accs),B):
    part=accs[i:i+B]
    vals=','.join(f"'{a}'" for a in part)
    tbl.delete(f"accession IN ({vals})")
    tot+=len(part)
    if (i//B)%10==0: print(f'  purged {tot} accessions, rows now {tbl.count_rows()}')
print('rows after:', tbl.count_rows(), 'removed ≈', n-tbl.count_rows())
