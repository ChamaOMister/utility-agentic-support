import React, { useEffect, useRef, useState } from 'react';
import { ApiError, createConversation, getConversation, getDemoCustomers, getHealth, sendMessage } from './api';
import fallback from './ui-text.json';
import ChatPanel from './ChatPanel';
import DebugDrawer from './DebugDrawer';
import DemoDataPanel, { formatCPF } from './DemoDataPanel';
const conversationKey = 'vertice-conversation-id';
function storedConversation() { try {return localStorage.getItem(conversationKey);} catch {return null;} }
function remember(id) { try {localStorage.setItem(conversationKey, id);} catch {/* In-memory conversations still work. */} }
function restoredTurn(turn) {
  return {...turn, correlationId: turn.correlation_id,
    retry: turn.status === 'interrupted' ? 'new' : turn.status === 'in_progress' ? 'same' : null};
}
// A turn restored while still running on the server (e.g. after a reload): it shows a pending
// bubble with the same-ID retry, and the transcript is re-read until the server finishes it.
const isPending = turn => turn.status === 'in_progress' && !turn.reply;
const pendingPollMs = 2000;
const pendingPollLimitMs = 75000; // past the 60 s lease, when a stuck turn is recovered as interrupted
export default function App() {
  const [ui, setUi] = useState(fallback);
  const [health, setHealth] = useState(null);
  const [customers, setCustomers] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  const [turns, setTurns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  // A demo profile's details for the verify form; `filled` makes each button press a new value.
  const [prefill, setPrefill] = useState(null);
  const fill = person => setPrefill({identifier: formatCPF(person.cpf), birthDate: person.birth_date,
    phone: person.phone_last4, filled: Date.now()});
  const sending = useRef(false);
  async function initialize() {
    setLoading(true); setError(false);
    try {
      const status = await getHealth();
      setHealth(status); setUi({...fallback, ...status.ui});
      const saved = storedConversation();
      let id = saved;
      let transcript = [];
      if (saved) {
        try { transcript = (await getConversation(saved)).turns.map(restoredTurn); }
        catch (err) {if (err instanceof ApiError && err.status === 404) id = null; else throw err;}
      }
      if (!id) {id = await createConversation(); remember(id);}
      setConversationId(id); setTurns(transcript);
      setCustomers(await getDemoCustomers().catch(() => null));
    } catch {setError(true);}
    finally {setLoading(false);}
  }
  useEffect(() => {initialize();}, []);
  const hasPending = turns.some(isPending);
  useEffect(() => {
    if (!conversationId || !hasPending) return undefined;
    let active = true;
    const started = Date.now();
    const timer = setInterval(async () => {
      if (Date.now() - started > pendingPollLimitMs) {clearInterval(timer); return;}
      try {
        const finished = new Map((await getConversation(conversationId)).turns
          .filter(t => t.status !== 'in_progress').map(t => [t.client_message_id, t]));
        if (!active) return;
        // Only turns still pending here: a same-ID retry in flight owns its turn.
        setTurns(previous => previous.map(t => isPending(t) && finished.has(t.client_message_id)
          ? restoredTurn(finished.get(t.client_message_id)) : t));
      } catch {/* The same-ID retry stays available. */}
    }, pendingPollMs);
    return () => {active = false; clearInterval(timer);};
  }, [conversationId, hasPending]);
  async function submit(text, retryTurn = null) {
    if (sending.current) return;
    sending.current = true; setBusy(true);
    const id = retryTurn?.retry === 'same' ? retryTurn.client_message_id : crypto.randomUUID();
    const turn = {client_message_id: id, text};
    setTurns(previous => retryTurn ? previous.map(t => t.client_message_id === retryTurn.client_message_id ? turn : t) : [...previous, turn]);
    try {
      const result = await sendMessage(conversationId, text, id);
      const completed = {...turn, ...result, retry: result.retryWithNewId ? 'new' : result.retryWithSameId ? 'same' : null};
      setTurns(previous => previous.map(t => t.client_message_id === id ? completed : t));
    } catch (err) {
      const failed = {...turn, reply: err instanceof ApiError ? ui.unavailable : ui.network_error,
        retry: err instanceof ApiError ? null : 'same'};
      setTurns(previous => previous.map(t => t.client_message_id === id ? failed : t));
    } finally {sending.current = false; setBusy(false);}
  }
  return <div className="app-shell">
    <header className="site-header"><a className="brand" href="/" aria-label={ui.brand}><span className="brand-mark" aria-hidden="true">V</span><span>{ui.brand}</span></a><span className="fictional">{ui.fictional}</span></header>
    <main><section className="intro"><div><p className="eyebrow">{ui.eyebrow}</p><h1>{ui.title}</h1><p className="intro-copy">{ui.intro}</p></div>
      {health?.mode === 'scripted' && <span className="offline-badge"><span aria-hidden="true">●</span>{ui.offline_badge}</span>}
    </section>
    {loading && <p className="connection-status" role="status">{ui.loading}</p>}
    {error && <div className="connection-error" role="alert"><p>{ui.network_error}</p><button type="button" onClick={initialize}>{ui.retry}</button></div>}
    <div className="workspace"><div className="chat-column"><ChatPanel ui={ui} turns={turns} busy={busy} ready={!loading && !error && Boolean(conversationId)} submit={submit} retry={turn => submit(turn.text, turn)} prefill={prefill} /><DebugDrawer turns={turns} /></div>
      <DemoDataPanel ui={ui} customers={customers} fill={fill} /></div>
    </main><footer className="site-footer">{ui.footer}</footer>
  </div>;
}
