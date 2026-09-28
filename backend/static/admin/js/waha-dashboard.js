(() => {
  const dashboard = document.getElementById('waha-dashboard');
  if (!dashboard) return;
  for (const form of dashboard.querySelectorAll('[data-waha-action]')) {
    form.addEventListener('submit', (event) => {
      if (form.dataset.wahaAction === 'logout' && !window.confirm('Выйти из WhatsApp? Авторизация будет сброшена; для подключения потребуется новый QR-код.')) {
        event.preventDefault();
        return;
      }
      for (const button of dashboard.querySelectorAll('[data-waha-action] button')) button.disabled = true;
    });
  }
  if (dashboard.dataset.busy !== 'true') return;
  const deadline = Date.now() + 120000;
  const poll = async () => {
    try {
      const response = await fetch(dashboard.dataset.stateUrl, { cache: 'no-store', credentials: 'same-origin' });
      if (!response.ok || response.redirected) throw new Error('State unavailable');
      const state = await response.json();
      if (!state.busy) {
        window.location.replace(dashboard.dataset.dashboardUrl);
        return;
      }
      const operation = dashboard.querySelector('[data-testid="waha-operation"]');
      operation.dataset.state = state.state;
      operation.querySelector('strong').textContent = `${state.action}: ${state.message}`;
      if (Date.now() >= deadline) throw new Error('Queue wait exceeded');
      window.setTimeout(poll, 1500);
    } catch {
      document.getElementById('waha-poll-error').hidden = false;
    }
  };
  window.setTimeout(poll, 1000);
})();
