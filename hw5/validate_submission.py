"""Verify the source-bounded homework package; never make model or external calls."""
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent
checks = []

def check(name, ok, detail=''):
    checks.append({'check': name, 'passed': bool(ok), 'detail': detail})

required = ['AI_prompts.md', 'requirements.txt', '.env.example', '.gitignore', '.mcp.json',
    'README.md', 'data/campus_customs.db', 'data/campus_customs_new.db',
    'mcp_server/server.py', 'mcp_server/README.md', 'backend/main.py', 'backend/models.py',
    'frontend/package.json', 'frontend/src/App.tsx', 'output/harness.md',
    'output/mcp_smoke.json', 'output/desk_tickets.html', 'output/design.md',
    'output/resolved_tickets.json', 'output/resolved_board.html', 'output/audit_trail.json',
    'output/github_url.txt']
for f in required:
    check('Required '+f, (ROOT/f).is_file() and (ROOT/f).stat().st_size > 0)
for role in ['boss','inventory','accounting','facilities','customer_service']:
    p=ROOT/'backend/prompts'/f'{role}.md'
    check('Detailed '+role+' prompt', p.exists() and len(p.read_text()) > 500)
original=ROOT/'data/campus_customs.db'
check('Original download unchanged', original.exists() and hashlib.sha256(original.read_bytes()).hexdigest()=='23686a90d698f7fa6f901a3f0a9774db9cbb31e13151c9e5717275f3321f360a')
if (ROOT/'data/campus_customs_new.db').exists():
    with sqlite3.connect(ROOT/'data/campus_customs_new.db') as db:
        rows=db.execute('select id,status from tickets order by id').fetchall()
        check('All three tickets resolved', rows==[(101,'resolved'),(102,'resolved'),(103,'resolved')],str(rows))
        balance=db.execute("select balance from cash_accounts where name='checking'").fetchone()[0]
        paid=db.execute('select coalesce(sum(amount),0) from payments').fetchone()[0]
        check('Cash equals original minus payments', abs(balance-(3400-paid))<0.005,f'{balance:.2f} = 3400 - {paid:.2f}')
        check('Nonnegative checking',balance>=0)
        check('Every payment records a human approver',not db.execute("select 1 from payments where trim(approved_by)='' or approved_by is null").fetchone())
prompts=(ROOT/'AI_prompts.md').read_text() if (ROOT/'AI_prompts.md').exists() else ''
check('Eleven prompt sections', len(re.findall(r'^## Problem \d+ ',prompts,re.M))==11)
check('No forbidden user prompt heading', 'actual user prompt' not in '\n'.join(x.lower() for x in prompts.splitlines() if x.startswith('#')))
sections=re.split(r'^## Problem \d+ ',prompts,flags=re.M)[1:]
check('Several prepared prompts per problem', len(sections)==11 and all(len(re.findall(r'^\d+\. ',s,re.M))>=3 for s in sections))
smoke=ROOT/'output/mcp_smoke.json'
if smoke.exists():
    evidence=json.loads(smoke.read_text())
    native=evidence.get('tests',[])
    names={'get_ticket','get_inventory','get_financial_context'}
    check('Connected coding assistant smoke recorded',evidence.get('vibe_coder')=='Codex CLI 0.162.1' and bool(evidence.get('native_session_id')))
    check('Three successful native MCP calls',len(native)==3 and {t.get('tool_name') for t in native}==names and all(t.get('native_mcp_event',{}).get('status')=='completed' and not t.get('native_mcp_event',{}).get('error') for t in native))
    check('Native outputs preserved exactly',all(t.get('output')==t.get('native_mcp_event',{}).get('result',{}).get('structured_content') for t in native))
    check('Executed smoke prompt recorded',bool(evidence.get('request_prompt')) and all(t.get('prompt_asked_to_coder')==evidence.get('request_prompt') for t in native))
    bytool={t.get('tool_name'):t.get('output',{}) for t in native}
    matches=False
    if names.issubset(bytool) and (ROOT/'data/campus_customs_new.db').exists():
        with sqlite3.connect(ROOT/'data/campus_customs_new.db') as db:
            matches=(bytool['get_ticket'].get('ticket',{}).get('status')==db.execute('select status from tickets where id=101').fetchone()[0]
                and bytool['get_inventory'].get('items',[{}])[0].get('qty')==db.execute("select qty from inventory where sku='CC-HOOD-NAVY' and size='M'").fetchone()[0]
                and bytool['get_financial_context'].get('checking',{}).get('balance')==db.execute("select balance from cash_accounts where name='checking'").fetchone()[0])
    check('Native MCP evidence matches final database',matches)
page=ROOT/'output/desk_tickets.html'
if page.exists():
    text=page.read_text()
    check('Ticket Cash Reflection tabs',all(f'data-tab="{x}"' in text for x in ['101','102','103','cash','reflection']))
    check('Actual evidence filled', 'Awaiting the fresh, recorded board run.' not in text)
    check('Reflection filled', 'Will be grounded in the recorded' not in text)
    check('Cash filled', 'Will be filled after the working' not in text)
board=ROOT/'output/resolved_board.html'
if board.exists():
    text=board.read_text()
    imgs=re.findall(r'<img[^>]+src=["\']([^"\']+)',text)
    check('Three saved board screenshots', len(imgs)>=3 and all((board.parent/src).is_file() for src in imgs if not src.startswith('data:')))
audit=ROOT/'output/audit_trail.json'
if audit.exists():
    records=json.loads(audit.read_text())
    check('Real audit present',isinstance(records,list) and len(records)>0)
    models={str(r['model']) for r in records if isinstance(r,dict) and r.get('model')}
    check('Only required model in audit', models=={'gpt-6-luna'},str(models))
url=ROOT/'output/github_url.txt'
if url.exists(): check('Published repository URL',bool(re.fullmatch(r'https://github\.com/[^/\s]+/[^/\s]+',url.read_text().strip())))
failures=[c for c in checks if not c['passed']]
report={'passed':not failures,'scope':'Artifact, database and recorded connection checks; passing does not establish personal prompt history or Canvas submission.',
    'manual_requirements':{'problem_1':'44 prepared prompts retained with user confirmation; literal per-problem student-typed history is unavailable.',
        'problem_10':'Evidence-based reflection is prepared for student review and adoption.',
        'problem_11':'Public repository and URL file verified; Canvas submission remains pending.'},
    'checks':checks,'failures':len(failures)}
(ROOT/'output/validation.json').write_text(json.dumps(report,indent=2)+'\n')
for c in failures: print('OUTSTANDING:',c['check'],c['detail'])
print(f'{len(checks)-len(failures)}/{len(checks)} checks passed')
sys.exit(bool(failures))
