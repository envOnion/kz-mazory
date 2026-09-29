(() => {
  const panel = document.querySelector('[data-progress-url]');
  if (!panel) return;
  const terminal = new Set(['completed', 'completed_with_errors', 'empty', 'failed', 'cancelled']);
  const importing = new Set(['waiting_connection', 'waiting_sync', 'watching', 'collecting', 'importing']);
  const names = JSON.parse(document.getElementById('history-field-names').textContent);
  const readonly = document.querySelectorAll('#whatsapphistoryrun_form .field-line .readonly');
  const fields = {};
  names.forEach((name, index) => {
    if (readonly[index]) {
      fields[name] = readonly[index];
      fields[name].dataset.historyField = name;
    }
  });
  async function refresh() {
    try {
      const response = await fetch(panel.dataset.progressUrl, {credentials: 'same-origin', headers: {'Accept': 'application/json'}});
      if (!response.ok) return;
      const value = await response.json();
      panel.dataset.state = value.state;
      panel.querySelector('[data-history-label]').textContent = value.label;
      panel.querySelector('[data-history-message]').textContent = value.message;
      panel.querySelector('[data-history-counts]').textContent = `Получено: ${value.fetched} · Новых: ${value.imported} · Уже были: ${value.existing}`;
      panel.querySelector('[data-history-analysis]').textContent = `Обработано: ${value.analysis.processed} / ${value.analysis.total} · Ошибок: ${value.analysis.errors}`;
      panel.querySelector('[data-history-error]').textContent = value.error;
      panel.querySelector('[data-history-empty-help]').hidden = value.state !== 'empty';
      panel.querySelector('[data-history-group-title]').textContent = value.fields.source_snapshot.group_title || 'Название не получено';
      document.querySelectorAll('[data-history-action]').forEach(form => {
        const action = form.dataset.historyAction;
        form.hidden = !(action === 'resume' ? value.state === 'paused' : action === 'pause' ? importing.has(value.state) : importing.has(value.state) || value.state === 'paused');
      });
      Object.entries(value.fields).forEach(([name, content]) => {
        const field = fields[name];
        if (field) field.textContent = content === '' ? '—' : typeof content === 'object' ? JSON.stringify(content) : String(content);
      });
      if (!terminal.has(value.state)) window.setTimeout(refresh, 3000);
    } catch (_) {
      panel.querySelector('[data-history-error]').textContent = 'Не удалось обновить состояние. Повторяем проверку.';
      window.setTimeout(refresh, 5000);
    }
  }
  refresh();
})();
