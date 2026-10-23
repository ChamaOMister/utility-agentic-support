import React, { useState } from 'react';
// The local calendar day as YYYY-MM-DD, the value format of <input type="date">.
const today = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };
const minBirthDate = '1900-01-01';
export function checkIdentity({identifier, birthDate, phone}) {
  const digits = identifier.replace(/\D/g, '');
  return {
    digits,
    identifier: /^[\d.\-\s]+$/.test(identifier) && (digits.length === 10 || digits.length === 11),
    birthDate: birthDate >= minBirthDate && birthDate <= today(),
    phone: /^\d{4}$/.test(phone),
  };
}
const empty = {identifier: '', birthDate: '', phone: ''};
export default function VerifyIdentity({ui, disabled, submit}) {
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState(empty);
  const [checked, setChecked] = useState(false);
  const valid = checkIdentity(values);
  const set = key => event => setValues(previous => ({...previous, [key]: event.target.value}));
  function close() { setOpen(false); setValues(empty); setChecked(false); }
  function onSubmit(event) {
    event.preventDefault();
    setChecked(true);
    if (disabled || !valid.identifier || !valid.birthDate || !valid.phone) return;
    const text = ui.verify_message.replace('{identifier}', valid.digits)
      .replace('{birth_date}', values.birthDate).replace('{phone_last4}', values.phone);
    close();
    submit(text);
  }
  if (!open) return <div className="verify-open"><button type="button" className="verify-open-button" disabled={disabled}
    onClick={() => setOpen(true)}><span aria-hidden="true">🪪</span>{ui.verify_open}</button></div>;
  const field = (key, id, label, error, input) => <div className="verify-field">
    <label htmlFor={id}>{label}</label>
    {React.cloneElement(input, {id, value: values[key], onChange: set(key), 'aria-invalid': checked && !valid[key],
      'aria-describedby': checked && !valid[key] ? `${id}-error` : undefined})}
    {checked && !valid[key] && <p id={`${id}-error`} className="verify-error">{error}</p>}
  </div>;
  return <form className="verify-card" aria-labelledby="verify-title" onSubmit={onSubmit} noValidate>
    <h3 id="verify-title">{ui.verify_title}</h3>
    {field('identifier', 'verify-identifier', ui.verify_identifier, ui.verify_identifier_error,
      <input type="text" inputMode="numeric" autoComplete="off" autoFocus />)}
    {field('birthDate', 'verify-birth-date', ui.verify_birth_date, ui.verify_birth_date_error,
      <input type="date" min={minBirthDate} max={today()} />)}
    {field('phone', 'verify-phone', ui.verify_phone, ui.verify_phone_error,
      <input type="text" inputMode="numeric" maxLength={4} autoComplete="off" />)}
    <div className="verify-actions">
      <button type="button" className="verify-cancel" onClick={close}>{ui.verify_cancel}</button>
      <button type="submit" disabled={disabled}>{ui.verify_submit}</button>
    </div>
  </form>;
}
