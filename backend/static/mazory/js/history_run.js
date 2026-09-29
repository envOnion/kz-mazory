(() => {
  const panel = document.querySelector('[data-progress-url]');
  if (!panel) return;
  const terminal = new Set(['completed', 'completed_with_errors', 'empty', 'failed', 'cancelled']);
  async function refresh() {
    try {
      const response = await fetch(panel.dataset.progressUrl, {credentials: 'same-origin', headers: {'Accept': 'application/json'}});
      if (!response.ok) return;
      const value = await response.json();
      const previous = panel.dataset.state;
      panel.dataset.state = value.state;
      panel.querySelector('[data-history-label]').textContent = value.label;
      panel.querySelector('[data-history-message]').textContent = value.message;
      panel.querySelector('[data-history-counts]').textContent = `Получено: ${value.fetched} · Новых: ${value.imported} · Уже были: ${value.existing}`;
      panel.querySelector('[data-history-analysis]').textContent = `Обработано: ${value.analysis.processed} / ${value.analysis.total} · Ошибок: ${value.analysis.errors}`;
      panel.querySelector('[data-history-error]').textContent = value.error;
      if (previous !== value.state && (terminal.has(value.state) || previous === 'paused' || value.state === 'paused' || value.state === 'analyzing')) {
        window.location.reload();
        return;
      }
      if (!terminal.has(value.state)) window.setTimeout(refresh, 3000);
    } catch (_) {
      panel.querySelector('[data-history-error]').textContent = 'Не удалось обновить состояние. Повторяем проверку.';
      window.setTimeout(refresh, 5000);
    }
  }
  refresh();
})();
