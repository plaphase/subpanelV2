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
  const linkMarkup = (href, icon, label, current = active) =>
    `<a href="${href}" class="${href === current ? 'active' : ''}"><span class="mr-icon">${icon}</span><span class="mr-text">${label}</span></a>`;

  const rail = document.createElement('aside');
  rail.id = 'multi-rail';
  rail.innerHTML = `
    <div class="mr-brand"><div class="mr-logo">MS</div><div>MultiSub<small>CONTROL PLANE · V2</small></div></div>
    <div class="mr-label">РАБОЧАЯ ОБЛАСТЬ</div>
    <nav class="mr-nav">${links.map(([href, icon, label]) => `
      <a href="${href}" class="${href === active ? 'active' : ''}">
        <span class="mr-icon">${icon}</span><span class="mr-text">${label}</span>
        ${href === '/' ? '<span class="mr-count" data-user-count>…</span>' : ''}
      </a>`).join('')}
    </nav>
    <div class="mr-label">БЫСТРО</div>
    <nav class="mr-nav">
      <a href="/user/new"><span class="mr-icon">＋</span><span class="mr-text">Новый пользователь</span></a>
      <button type="button" id="mr-search"><span class="mr-icon">⌕</span><span class="mr-text">Поиск по панели</span></button>
      <a href="/sources"><span class="mr-icon">⟳</span><span class="mr-text">Проверить VPS</span></a>
    </nav>
    <div class="mr-account"><form method="post" action="/logout"><input type="hidden" name="csrf_token"><button type="submit"><span class="mr-icon">↪</span><span class="mr-text">Выйти</span></button></form></div>
    <div class="mr-bottom"><div class="mr-health" id="mr-health"><i></i>Панель в норме<small>Проверяю соединение…</small></div></div>`;

  document.body.classList.add('panel-shell');
  document.body.prepend(rail);
  let viewport = document.querySelector('meta[name="viewport"]');
  if (!viewport) {
    viewport = document.createElement('meta');
    viewport.name = 'viewport';
    document.head.append(viewport);
  }
  viewport.content = 'width=device-width,initial-scale=1,viewport-fit=cover';
  rail.querySelector('.mr-account input[name="csrf_token"]').value =
    document.querySelector('meta[name="csrf-token"]')?.content || '';

  const searchPanel = () => {
    const field = document.querySelector('#userSearch,#searchInput');
    if (field) {
      field.focus();
      field.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else {
      location.href = '/?focus=search';
    }
  };
  document.getElementById('mr-search')?.addEventListener('click', searchPanel);

  const mobile = document.createElement('div');
  mobile.id = 'mobile-navigation';
  mobile.innerHTML = `
    <section class="mobile-more-panel" id="mobile-more-panel" hidden>
      <div class="mobile-more-heading">Быстрый переход</div>
      ${linkMarkup('/bulk', '⊞', 'Массовые действия')}
      ${linkMarkup('/logs', '⌁', 'Журнал')}
      ${linkMarkup('/trash', '⌫', 'Корзина')}
      ${linkMarkup('/user/new', '＋', 'Новый пользователь')}
      <button type="button" id="mobile-search"><span class="mr-icon">⌕</span>Поиск по панели</button>
      <div class="mobile-logout"></div>
    </section>
    <nav class="mobile-tabbar" aria-label="Основная навигация">
      ${linkMarkup('/dashboard', '◈', 'Обзор')}
      ${linkMarkup('/', '♙', 'Пользователи')}
      ${linkMarkup('/sources', '⌘', 'Источники')}
      <button type="button" id="mobile-more" aria-expanded="false"><span class="mobile-tab-icon">···</span><span>Ещё</span></button>
    </nav>`;
  document.body.append(mobile);
  const logoutForm = rail.querySelector('.mr-account form').cloneNode(true);
  logoutForm.querySelector('input[name="csrf_token"]').value =
    document.querySelector('meta[name="csrf-token"]')?.content || '';
  mobile.querySelector('.mobile-logout').append(logoutForm);
  mobile.querySelector('#mobile-search').addEventListener('click', () => {
    mobile.querySelector('#mobile-more-panel').hidden = true;
    mobile.querySelector('#mobile-more').setAttribute('aria-expanded', 'false');
    searchPanel();
  });
  mobile.querySelector('#mobile-more').addEventListener('click', (event) => {
    const panel = mobile.querySelector('#mobile-more-panel');
    panel.hidden = !panel.hidden;
    event.currentTarget.setAttribute('aria-expanded', String(!panel.hidden));
  });
  document.addEventListener('click', (event) => {
    if (!mobile.contains(event.target)) {
      mobile.querySelector('#mobile-more-panel').hidden = true;
      mobile.querySelector('#mobile-more').setAttribute('aria-expanded', 'false');
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      mobile.querySelector('#mobile-more-panel').hidden = true;
      mobile.querySelector('#mobile-more').setAttribute('aria-expanded', 'false');
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

  fetch('/nav-count', { cache: 'no-store' })
    .then((response) => {
      if (!response.ok) throw new Error('navigation count unavailable');
      return response.json();
    })
    .then((data) => {
      const count = rail.querySelector('[data-user-count]');
      if (count && Number.isInteger(data.users)) count.textContent = String(data.users);
    })
    .catch(() => {
      const count = rail.querySelector('[data-user-count]');
      if (count) count.textContent = '—';
    });

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
