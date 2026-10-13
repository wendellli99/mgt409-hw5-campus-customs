import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowDownLeft, ArrowRight, Check, CheckCheck, ChevronRight, CircleDollarSign, ClipboardList, Coffee, Crown, FileCheck2, Headphones, LoaderCircle, Package, Play, RefreshCcw, ShieldCheck, Sparkles, Wrench, X, AlertCircle, ArrowUpRight, Send, TerminalSquare } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000';

type Ticket = { id: number; ticket_id: number; title: string; description: string; status: string; priority?: string; agent_summary?: string; customer_name?: string; category?: string; type?: string; requester?: string; notes?: string };
type Approval = { id: string; approval_id: string; ticket_id: number; kind: string; amount: number; description: string; status: string; payee?: string; recipient?: string; created_at: string };
type AgentEvent = { event_id: string; run_id: string; ticket_id: number | null; agent: string; event_type: string; message: string; timestamp: string; tool?: string; input?: Record<string, unknown>; output?: unknown };
type Run = { run_id: string; ticket_id: number; status: string; started_at: string; finished_at?: string; summary?: string; error?: string; contributions?: Record<string, string> };
type DeskState = { tickets: Ticket[]; cash: { balance: number; starting_balance?: number; total_outflow?: number; currency?: string; account_name?: string }; approvals: Approval[]; events: AgentEvent[]; runs: Run[] };
type AgentName = 'Boss' | 'Inventory' | 'Accounting' | 'Facilities' | 'Customer Service';

const agents: { name: AgentName; key: string; initials: string; role: string; icon: LucideIcon; color: string }[] = [
  { name: 'Boss', key: 'boss', initials: 'B', role: 'Routes the work. Closes the loop.', icon: Crown, color: 'navy' },
  { name: 'Inventory', key: 'inventory', initials: 'I', role: 'Checks stock and purchase options.', icon: Package, color: 'sage' },
  { name: 'Accounting', key: 'accounting', initials: 'A', role: 'Checks the books. Prepares payment.', icon: CircleDollarSign, color: 'ochre' },
  { name: 'Facilities', key: 'facilities', initials: 'F', role: 'Keeps the shop running smoothly.', icon: Wrench, color: 'clay' },
  { name: 'Customer Service', key: 'customer-service', initials: 'CS', role: 'Finds the customer story and responds.', icon: Headphones, color: 'lilac' },
];
const money = (amount: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(amount);
const ticketId = (ticket: Ticket) => ticket.ticket_id ?? ticket.id;
const approvalId = (approval: Approval) => approval.approval_id ?? approval.id;
const normalize = (value = '') => value.toLowerCase().replace(/[\s_-]/g, '');
const agentFor = (name = '') => agents.find(agent => normalize(agent.name) === normalize(name)) ?? agents[0];
const isResolved = (status: string) => ['resolved', 'completed', 'closed'].includes(status?.toLowerCase());
const isActive = (status?: string) => status === 'running' || status === 'awaiting_approval';
const runFor = (state: DeskState | null, id: number) => [...(state?.runs ?? [])].reverse().find(run => run.ticket_id === id);
const relativeTime = (stamp: string) => {
  const date = new Date(stamp);
  return Number.isNaN(date.getTime()) ? stamp : date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', second: '2-digit' });
};

function StatusBadge({ status, label }: { status: string; label?: string }) {
  const resolved = isResolved(status);
  const running = status === 'running';
  const pending = status === 'awaiting_approval';
  const failed = status === 'failed';
  return <span className={`status-badge ${resolved ? 'resolved' : pending ? 'pending' : running ? 'running' : failed ? 'failed' : 'open'}`}>
    {resolved ? <Check size={11} /> : running ? <span className="pulse-dot" /> : pending ? <CircleDollarSign size={12} /> : failed ? <AlertCircle size={12} /> : <span className="small-dot" />}
    {label || (resolved ? 'Resolved' : pending ? 'Needs approval' : running ? 'Working' : failed ? 'Run failed' : 'Open')}
  </span>;
}

export default function App() {
  const [state, setState] = useState<DeskState | null>(null);
  const [selectedId, setSelectedId] = useState(101);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [allEvents, setAllEvents] = useState(false);
  const [showReset, setShowReset] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const inFlight = useRef(false);
  const resetDialog = useRef<HTMLElement>(null);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const response = await fetch(`${API}/api/state`);
      if (!response.ok) throw new Error(`The desk could not refresh (${response.status}).`);
      const next: DeskState = await response.json();
      setState(next);
      setConnected(true);
      setError(previous => previous?.startsWith('Connection') ? null : previous);
    } catch {
      setConnected(false);
      setError('Connection lost. Start the backend on port 8000, then reconnect.');
    } finally { inFlight.current = false; }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!showReset) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    const buttons = () => Array.from(resetDialog.current?.querySelectorAll<HTMLButtonElement>('button') ?? []);
    buttons()[0]?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setShowReset(false);
      if (event.key !== 'Tab') return;
      const items = buttons();
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('keydown', onKey); previousFocus?.focus(); };
  }, [showReset]);

  const act = async (path: string, label: string, body?: unknown) => {
    setBusy(label);
    setError(null);
    try {
      const response = await fetch(`${API}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(typeof detail.detail === 'string' ? detail.detail : `Could not complete this action (${response.status}).`);
      }
      await refresh();
      if (label === 'reset') { setSelectedId(101); setShowReset(false); setNotice('A fresh desk is ready. All three tickets are open.'); }
      if (label.startsWith('approve')) setNotice('Payment approved by Wendell. The team is continuing the ticket.');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Something went wrong. Please try again.'); }
    finally { setBusy(null); }
  };

  const tickets = state?.tickets ?? [];
  const ticket = tickets.find(item => ticketId(item) === selectedId) ?? tickets[0];
  const id = ticket ? ticketId(ticket) : selectedId;
  const run = runFor(state, id);
  const pending = (state?.approvals ?? []).filter(item => item.status === 'pending');
  const currentApproval = pending.find(item => item.ticket_id === id);
  const sourceEvents = state?.events ?? [];
  const lastReset = sourceEvents.findLastIndex(event => event.event_type === 'database_reset');
  const sessionEvents = sourceEvents.slice(lastReset + 1);
  const selectedEvents = sessionEvents.filter(event => event.ticket_id === id);
  const timeline = (allEvents ? sessionEvents : selectedEvents).slice().reverse();
  const resolvedCount = tickets.filter(item => isResolved(item.status)).length;
  const anyRunning = state?.runs.some(item => item.status === 'running') ?? false;
  const status = currentApproval ? 'awaiting_approval' : isResolved(ticket?.status ?? '') ? 'resolved' : run?.status === 'completed' ? ticket?.status ?? 'open' : run?.status ?? ticket?.status ?? 'open';
  const currentCash = state?.cash.balance ?? 0;
  const approvedTotal = (state?.approvals ?? []).filter(item => item.status === 'approved').reduce((sum, item) => sum + item.amount, 0);
  const startingCash = state?.cash.starting_balance ?? currentCash + approvedTotal;
  const outflow = state?.cash.total_outflow ?? startingCash - currentCash;
  const runButton = isResolved(status) ? 'Ticket resolved' : currentApproval ? 'Waiting for approval' : status === 'running' || busy === 'run' ? 'Team is working' : status === 'failed' ? 'Try this ticket again' : 'Start the team';

  return <div className="desk-shell">
    <aside className="side-rail" aria-label="Campus Customs identity">
      <a className="monogram" href="#desk" aria-label="Campus Customs desk">CC<span>®</span></a>
      <div className="rail-rule" />
      <div className="rail-active"><ClipboardList size={21} /><span>Desk</span></div>
      <div className="rail-bottom"><Coffee size={21} /><span>New Haven</span></div>
    </aside>
    <div className="desk-main" id="desk">
      <header className="topbar">
        <div className="brand"><strong>CAMPUS CUSTOMS</strong><span>Good work, together.</span></div>
        <div className="topbar-right">
          <span className={`connection ${connected ? 'connected' : ''}`}><span />{connected ? 'Desk connected' : state ? 'Reconnecting' : 'Connecting'}</span>
          <span className="operator-avatar" aria-label="Wendell, desk operator">W</span>
        </div>
      </header>

      <main>
        <div className="page-heading">
          <div><div className="eyebrow"><span className="tiny-rule" /> THE OPERATIONS DESK</div><h1>A little teamwork. <br /><em>A smoother shop.</em></h1><p>Three requests. Five specialists. You keep the final say.</p></div>
          <div className="heading-aside"><div className="desk-date">CAMPUS CUSTOMS · NEW HAVEN</div><div className="session-progress"><span>{String(resolvedCount).padStart(2, '0')}</span><div>of 03 tickets<br /><strong>resolved</strong></div></div><div className="progress-track"><span style={{ width: `${resolvedCount / 3 * 100}%` }} /></div></div>
        </div>

        {error && <div className="alert error" role="alert"><AlertCircle size={18} /><span>{error}</span><button onClick={() => void refresh()}>Reconnect<RefreshCcw size={13} /></button><button className="icon-button" onClick={() => setError(null)} aria-label="Dismiss error"><X size={16} /></button></div>}
        {notice && <div className="alert success" role="status"><CheckCheck size={18} /><span>{notice}</span><button className="icon-button" onClick={() => setNotice(null)} aria-label="Dismiss notice"><X size={16} /></button></div>}

        <section className="overview-grid" aria-label="Desk overview">
          <div className="cash-card"><div className="cash-top"><span><CircleDollarSign size={17} /> Checking account</span><span className="cash-mark">USD</span></div><div className="cash-balance">{state ? money(currentCash) : <span className="skeleton money-skeleton" />}</div><div className="cash-bottom"><span>Starting <strong>{state ? money(startingCash) : '—'}</strong></span><span><ArrowDownLeft size={13} /> Approved outflow <strong>{state ? money(outflow) : '—'}</strong></span></div></div>
          <div className="stat-card"><div className="stat-icon"><FileCheck2 size={20} /></div><div><span className="stat-label">Tickets resolved</span><div className="stat-value">{resolvedCount}<span>/ 3</span></div><p>{resolvedCount === 3 ? 'Every request has a clear outcome.' : 'One clear outcome at a time.'}</p></div></div>
          <div className={`stat-card approvals-stat ${pending.length ? 'has-approval' : ''}`}><div className="stat-icon"><ShieldCheck size={20} /></div><div><span className="stat-label">Your approval queue</span><div className="stat-value">{pending.length}<span>{pending.length === 1 ? 'payment' : 'payments'}</span></div><p>{pending.length ? 'The team is ready for your decision.' : 'Agents prepare. You approve.'}</p></div></div>
        </section>

        <section className="workbench">
          <aside className="ticket-queue" aria-label="Ticket queue"><div className="section-title"><h2>The inbox</h2><span className="count-chip">03</span></div><p className="section-caption">A small queue. A big difference.</p>
            <div className="ticket-list">
              {!state ? [0, 1, 2].map(index => <div className="ticket-skeleton" key={index}><span className="skeleton" /><span className="skeleton" /><span className="skeleton" /></div>) : tickets.map(item => {
                const itemId = ticketId(item); const itemRun = runFor(state, itemId); const itemPending = pending.some(approval => approval.ticket_id === itemId); const itemStatus = itemPending ? 'awaiting_approval' : isResolved(item.status) ? 'resolved' : itemRun?.status === 'completed' ? item.status : itemRun?.status ?? item.status;
                return <button key={itemId} className={`ticket-button ${id === itemId ? 'selected' : ''}`} onClick={() => { setSelectedId(itemId); setAllEvents(false); }} aria-pressed={id === itemId}>
                  <div className="ticket-top"><span className="ticket-number">REQUEST / {itemId}</span><StatusBadge status={itemStatus} /></div><h3>{item.title}</h3><p>{item.description || item.notes}</p><div className="ticket-bottom"><span>{item.category || item.type?.replace(/_/g, ' ') || 'Shop request'}</span><ArrowUpRight size={16} /></div>
                </button>;
              })}
            </div>
            <div className="queue-note"><Sparkles size={16} /><p>The Boss finds the right specialist. The team takes it from there.</p></div>
          </aside>

          <div className="ticket-workspace">
            <section className="ticket-detail" aria-label="Selected ticket"><div className="detail-kicker"><span>ON YOUR DESK / {id}</span><StatusBadge status={status} /></div><div className="detail-title-row"><div><h2>{ticket?.title || 'Your desk is getting ready'}</h2><p>{ticket?.description || ticket?.notes || 'Connecting to the shop’s tickets and checking account.'}</p>{ticket?.requester && <span className="requester">From {ticket.requester}</span>}</div><button className={`primary-button ${isResolved(status) ? 'finished-button' : ''}`} disabled={!state || !connected || !!busy || anyRunning || !!currentApproval || isResolved(status)} onClick={() => void act(`/api/tickets/${id}/run`, 'run')}>
              {isResolved(status) ? <CheckCheck size={17} /> : status === 'running' || busy === 'run' ? <LoaderCircle size={17} className="spin" /> : <Play size={15} fill="currentColor" />}{runButton}{!isResolved(status) && !isActive(status) && <ArrowRight size={16} />}
            </button></div>
              {run?.error && <div className="run-error"><AlertCircle size={16} /><span>{run.error}</span></div>}
              {isResolved(status) && <div className="outcome"><FileCheck2 size={18} /><div><strong>A clear outcome</strong><p>{run?.summary || ticket?.agent_summary || 'The team resolved this request. Review the activity below for each step and tool result.'}</p></div></div>}
              {status === 'running' && <div className="working-note" role="status"><span className="pulse-dot" /><span>The team is on it. Activity updates as each agent works.</span></div>}
            </section>

            {currentApproval && <section className="approval-panel" aria-label="Human approval required"><div className="approval-icon"><ShieldCheck size={23} /></div><div className="approval-copy"><div className="eyebrow">READY FOR YOUR DECISION</div><h3>Approve {currentApproval.kind.replace(/_/g, ' ')}?</h3><p>{currentApproval.description}</p><div className="approval-meta"><span>Payee <strong>{currentApproval.payee || currentApproval.recipient || 'See payment description'}</strong></span><span>Approved by <strong>Wendell</strong></span></div></div><div className="approval-action"><strong>{money(currentApproval.amount)}</strong><button className="approve-button" disabled={!!busy || !connected || anyRunning} onClick={() => void act(`/api/approvals/${approvalId(currentApproval)}/approve`, `approve-${approvalId(currentApproval)}`, { approved_by: 'Wendell' })}>{busy?.startsWith('approve') ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}Approve payment</button><span>{anyRunning ? 'Team is finishing its report' : 'Changes checking balance'}</span></div></section>}

            <section className="team-section" aria-label="Five-agent team"><div className="section-title"><h2>Meet the working team</h2><span className="team-hint"><span className="small-dot" />Live on request {id}</span></div>
              <div className="agent-grid">{agents.map(agent => {
                const events = selectedEvents.filter(event => normalize(event.agent) === normalize(agent.name)); const latest = events[events.length - 1]; const tools = [...new Set(events.filter(event => event.tool).map(event => event.tool!))]; const didWork = events.length > 0; const latestMessage = [...events].reverse().find(event => event.event_type === 'agent_completed')?.message || [...events].reverse().find(event => event.message && !['tool_result', 'tool_call'].includes(event.event_type))?.message || latest?.message; const active = status === 'running' && latest?.event_type !== 'complete' && didWork && agent.name === agentFor(selectedEvents[selectedEvents.length - 1]?.agent).name;
                const Icon = agent.icon;
                return <article className={`agent-card ${agent.color} ${active ? 'agent-active' : ''}`} key={agent.key}><div className="agent-card-top"><span className="agent-symbol"><Icon size={18} /></span><span className={`agent-presence ${active ? 'working' : didWork ? 'participated' : ''}`}>{active ? 'Working' : didWork ? isResolved(status) ? 'Contributed' : 'In the loop' : 'On standby'}</span></div><h3>{agent.name}</h3><p className="agent-role">{agent.role}</p><div className="agent-message">{didWork ? <><p className="agent-excerpt">{latestMessage}</p>{latestMessage && latestMessage.length > 280 && <details className="agent-full"><summary>Full contribution<ChevronRight size={10} /></summary><p>{latestMessage}</p></details>}</> : <span>Ready when this request needs {agent.name === 'Boss' ? 'a plan' : agent.name === 'Customer Service' ? 'a customer voice' : agent.name.toLowerCase()}.</span>}</div>{tools.length > 0 && <div className="agent-tools" aria-label={`${agent.name} tools used`}>{tools.map(tool => <span title={tool} key={tool}><TerminalSquare size={10} />{tool.replace(/^.*\./, '')}</span>)}</div>}</article>;
              })}</div>
            </section>

            <section className="activity-section" aria-label="Agent activity and tools"><div className="section-title"><div className="activity-title"><h2>The paper trail</h2><span className="count-chip">{timeline.length}</span></div><div className="activity-filter" aria-label="Activity filter"><button className={!allEvents ? 'active' : ''} onClick={() => setAllEvents(false)}>This request</button><button className={allEvents ? 'active' : ''} onClick={() => setAllEvents(true)}>All requests</button></div></div><p className="section-caption">The conversations, handoffs, and tools behind every outcome.</p>
              {timeline.length === 0 ? <div className="empty-activity"><div><Send size={22} /></div><h3>A quiet desk, for now.</h3><p>Start the team to follow its plan, specialist handoffs, and tool results here.</p><span>Every step leaves a trace.</span></div> : <ol className="event-list">{timeline.map((event, index) => {
                const agent = agentFor(event.agent); const Icon = event.agent === 'Human' ? ShieldCheck : agent.icon; const toolEvent = event.event_type.includes('tool') || !!event.tool;
                return <li className="event-row" key={event.event_id || `${event.timestamp}-${index}`}><span className={`event-marker ${agent.color}`}><Icon size={14} /></span><div className="event-content"><div className="event-meta"><strong>{event.agent}</strong><span className="event-type">{event.event_type.replace(/_/g, ' ')}</span>{allEvents && <span className="event-ticket">#{event.ticket_id}</span>}<time dateTime={event.timestamp}>{relativeTime(event.timestamp)}</time></div><p>{event.message}</p>{toolEvent && <div className="tool-detail"><span><TerminalSquare size={12} />{event.tool || 'Tool activity'}</span>{event.input && Object.keys(event.input).length > 0 && <details><summary>View input<ChevronRight size={11} /></summary><pre>{JSON.stringify(event.input, null, 2)}</pre></details>}{event.output !== undefined && <details><summary>View result<ChevronRight size={11} /></summary><pre>{typeof event.output === 'string' ? event.output : JSON.stringify(event.output, null, 2)}</pre></details>}</div>}</div></li>;
              })}</ol>}
            </section>
          </div>
        </section>

        <footer className="desk-footer"><span><ShieldCheck size={14} /> Human approval keeps money in your hands.</span><button onClick={() => setShowReset(true)} disabled={anyRunning || !!busy || !state}><RefreshCcw size={13} />Start a fresh desk</button><span className="footer-signature">MADE FOR THE WORKDAY.</span></footer>
      </main>
    </div>
    {showReset && <div className="modal-backdrop" onClick={() => setShowReset(false)}><section className="reset-dialog" ref={resetDialog} role="dialog" aria-modal="true" aria-labelledby="reset-heading" onClick={event => event.stopPropagation()}><div className="dialog-symbol"><RefreshCcw size={23} /></div><h2 id="reset-heading">Start a fresh desk?</h2><p>This restores the original database, reopens all three tickets, and clears this run’s payments and starts a new activity view. The saved audit trail stays intact. Your checking balance returns to its starting amount.</p><div><button className="secondary-button" onClick={() => setShowReset(false)}>Keep this desk</button><button className="primary-button" disabled={!!busy} onClick={() => void act('/api/reset', 'reset')}>{busy === 'reset' ? <LoaderCircle size={16} className="spin" /> : <RefreshCcw size={15} />}Reset the desk</button></div></section></div>}
  </div>;
}
