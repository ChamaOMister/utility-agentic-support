import React from 'react';

const toolFields = ['name', 'status', 'error_code', 'attempts', 'duration_ms', 'record_count'];
const modelFields = ['kind', 'model', 'service_tier', 'input_tokens', 'output_tokens', 'estimated_cost_usd', 'outcome_class'];
function Fields({value, fields}) {
  // Pick scalar values explicitly: spreading or serializing a trace could expose payloads.
  return <dl className="metadata-fields">{fields.map(field => <div key={field}>
    <dt>{field}</dt><dd>{['string', 'number', 'boolean'].includes(typeof value?.[field]) ? String(value[field]) : '—'}</dd>
  </div>)}</dl>;
}
export default function DebugDrawer({turns}) {
  return <details className="debug-drawer" lang="en">
    <summary>Debug metadata</summary>
    <div data-testid="debug-metadata" className="debug-content">
      {turns.length === 0 && <p>No turns yet.</p>}
      {turns.map((turn, index) => <section key={turn.client_message_id} className="debug-turn">
        <h3>Turn {index + 1}</h3>
        <Fields value={{correlation_id: turn.correlationId}} fields={['correlation_id']} />
        <h4>Tool calls</h4>
        {(turn.trace?.tool_calls ?? []).length === 0 && <p>No tool calls.</p>}
        {(turn.trace?.tool_calls ?? []).map((call, i) => <Fields key={i} value={call} fields={toolFields} />)}
        <h4>Model calls</h4>
        {(turn.trace?.model_calls ?? []).length === 0 && <p>No provider calls.</p>}
        {(turn.trace?.model_calls ?? []).map((call, i) => <Fields key={i} value={call} fields={modelFields} />)}
      </section>)}
    </div>
  </details>;
}
