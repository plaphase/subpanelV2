(() => {
  const path = location.pathname;
  const links = [
    ['/dashboard', '◈', 'Обзор'],
    ['/', '♙', 'Пользователи'],
    ['/sources', '⌘', 'Источники'],
    ['/bulk', '⊞', 'Массовые действия'],
    ['/logs', '⌁', 'Журнал'],
    ['/trash', '⌫', 'Корзина'],
  ];
  const active = path === '/dashboard' ? '/dashboard'
    : path === '/sources' ? '/sources'
      : path === '/bulk' ? '/bulk'
        : path === '/logs' ? '/logs'
          : path === '/trash' ? '/trash' : '/';

  const rail = document.createElement('aside');
  rail.id = 'multi-rail';
  rail.innerHTML = `
    <div class="mr-brand"><div class="mr-logo">MS</div><div>MultiSub<small>CONTROL PLANE · V2</small></div></div>
    <div class="mr-label">РАБОЧАЯ ОБЛАСТЬ</div>
    <nav class="mr-nav">${links.map(([href, icon, label]) => `
      <a href="${href}" class="${href === active ? 'active' : ''}">
        <span class="mr-icon">${icon}</span><span class="mr-text">${label}</span>
        ${href === '/' ? `<span class="mr-count">${document.querySelectorAll('.user-card-item').length}</span>` : ''}
      </a>`).join('')}
    </nav>
    <div class="mr-label">БЫСТРО</div>
    <nav class="mr-nav">
      <a href="/user/new"><span class="mr-icon">＋</span><span class="mr-text">Новый пользователь</span></a>
      <button type="button" id="mr-search"><span class="mr-icon">⌕</span><span class="mr-text">Поиск по панели</span></button>
      <a href="/sources"><span class="mr-icon">⟳</span><span class="mr-text">Проверить VPS</span></a>
    </nav>
    <div class="mr-account"><form method="post" action="/logout">
      <input type="hidden" name="csrf_token">
      <button type="submit"><span class="mr-icon">↪</span><span class="mr-text">Выйти</span></button>
    </form></div>
    <div class="mr-bottom"><div class="mr-health" id="mr-health"><i></i>Панель в норме<small>Проверяю соединение…</small></div></div>`;

  document.body.classList.add('panel-shell');
  document.body.prepend(rail);
  rail.querySelector('.mr-account input[name="csrf_token"]').value =
    document.querySelector('meta[name="csrf-token"]')?.content || '';

  document.getElementById('mr-search')?.addEventListener('click', () => {
    const field = document.querySelector('#userSearch,#searchInput');
    if (field) {
      field.focus();
      field.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else {
      location.href = '/?focus=search';
    }
  });

  const params = new URLSearchParams(location.search);
  const field = document.getElementById('userSearch');
  if (path === '/' && field) {
    const query = params.get('q');
    if (query) {
      field.value = query;
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
    if (params.has('focus')) field.focus();
  }

  fetch('/health', { cache: 'no-store' })
    .then((response) => {
      if (!response.ok) throw new Error('health check failed');
      document.querySelector('#mr-health small').textContent = 'Локальная проверка · доступно';
    })
    .catch(() => {
      const health = document.getElementById('mr-health');
      health.classList.add('down');
      health.childNodes[1].textContent = 'Панель недоступна';
      health.querySelector('small').textContent = 'Проверь службу';
    });
})();