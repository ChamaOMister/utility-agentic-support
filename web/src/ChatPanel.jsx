import React, { useEffect, useRef, useState } from 'react';

export default function ChatPanel({ui, turns, busy, ready, submit, retry}) {
  const [text, setText] = useState('');
  const input = useRef(null);
  const bottom = useRef(null);
  const length = Array.from(text).length;
  useEffect(() => { bottom.current?.scrollIntoView({block: 'nearest'}); }, [turns, busy]);
  async function onSubmit(event) {
    event.preventDefault();
    if (!text.trim() || length > 2000 || busy || !ready) return;
    const message = text.trim();
    setText('');
    await submit(message);
    input.current?.focus();
  }
  return <section className="chat-panel" aria-labelledby="chat-title">
    <header className="chat-header"><span className="assistant-symbol" aria-hidden="true">V</span><div>
      <h2 id="chat-title">{ui.chat_title}</h2><p>{ui.chat_subtitle}</p>
    </div><span className="status-dot" aria-hidden="true" /></header>
    <div className="transcript" role="log" aria-label={ui.transcript} aria-live="polite" aria-relevant="additions text" aria-busy={busy}>
      <article className="message assistant"><span className="speaker">{ui.assistant}</span><p>{ui.welcome}</p></article>
      {turns.map(turn => <React.Fragment key={turn.client_message_id}>
        <article className="message user"><span className="speaker">{ui.you}</span><p data-testid="user-message">{turn.text}</p></article>
        {turn.reply && <article className="message assistant"><span className="speaker">{ui.assistant}</span>
          <p data-testid="assistant-reply">{turn.reply}</p>
          {turn.retry && <button type="button" className="retry-button" disabled={busy || !ready} onClick={() => retry(turn)}>{ui.retry}</button>}
        </article>}
      </React.Fragment>)}
      {busy && <p className="pending" role="status">{ui.sending}</p>}
      <div ref={bottom} />
    </div>
    <form className="composer" onSubmit={onSubmit}>
      <label htmlFor="message">{ui.message_label}</label>
      <div className="composer-row"><textarea id="message" ref={input} value={text} onChange={e => setText(e.target.value)}
        placeholder={ui.placeholder} rows={2} disabled={!ready || busy} aria-describedby="message-hint" aria-invalid={length > 2000}
        onKeyDown={e => {if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {e.preventDefault(); onSubmit(e);}}} />
        <button type="submit" disabled={!ready || busy || !text.trim() || length > 2000} aria-label={ui.send}><span>{ui.send}</span><span aria-hidden="true">↗</span></button></div>
      <div className="composer-hint"><p id="message-hint">{length > 2000 ? ui.too_long : ui.message_hint}</p><span aria-hidden="true">{length}/2000</span></div>
    </form>
  </section>;
}
