import React from 'react';
const formatCPF = cpf => cpf.replace(/(\d{3})(\d{3})(\d{3})(\d{2})/, '$1.$2.$3-$4');
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
        <div><dt>{ui.birth_date}</dt><dd>{person.birth_date.split('-').reverse().join('/')}</dd></div>
        <div><dt>{ui.phone_last4}</dt><dd>{person.phone_last4}</dd></div>
      </dl>
    </details>)}
    <p className="demo-notice">{ui.demo_notice}</p>
  </aside>;
}
