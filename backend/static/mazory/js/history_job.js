(() => {
  const mode = document.querySelector('#id_only_new');
  if (!mode) return;
  const toggle = (name, visible) => {
    const field = document.querySelector(`#id_${name}`);
    const row = field?.closest('.form-row, .field-line');
    if (row) row.hidden = !visible;
  };
  const update = () => {
    toggle('new_message_poll_seconds', mode.checked);
    ['interval_minutes', 'initial_wait_seconds', 'poll_seconds', 'stable_scans_required'].forEach(name => toggle(name, !mode.checked));
    const help = document.querySelector('[data-new-messages-help]');
    if (help) help.hidden = !mode.checked;
  };
  mode.addEventListener('change', update);
  update();
})();
