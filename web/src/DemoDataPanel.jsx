import React from 'react';
const formatCPF = cpf => cpf.replace(/(\d{3})(\d{3})(\d{3})(\d{2})/, '$1.$2.$3-$4');
// "1994-12-25" -> "Dec 25, 1994": a spelled month can't be read day-first or month-first.
const formatDate = iso => new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-US', {month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC'});
export default function DemoDataPanel({customers, ui}) {
  return <aside className="demo-panel" aria-labelledby="demo-title">
    <div className="demo-heading"><span className="eyebrow">{ui.synthetic}</span><h2 id="demo-title">{ui.demo_heading}</h2></div>
    <p className="demo-intro">{ui.demo_intro}</p>
    {customers === null && <p role="status">{ui.demo_unavailable}</p>}
    {customers?.map(person => <details key={person.persona} className="persona">
      <summary><span className="persona-letter" aria-hidden="true">{person.persona}</span><span>{person.name}</span></summary>
      <dl>
        <div><dt>{ui.cpf}</dt><dd>{formatCPF(person.cpf)}</dd></div>
        <div><dt>{ui.account}</dt><dd>{person.account_number}</dd></div>
        <div><dt>{ui.birth_date}</dt><dd>{formatDate(person.birth_date)}</dd></div>
        <div><dt>{ui.phone_last4}</dt><dd>{person.phone_last4}</dd></div>
      </dl>
    </details>)}
    <p className="demo-notice">{ui.demo_notice}</p>
  </aside>;
}
