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
###app\static\panel-v2.css
:root{--bg:#080d16;--card:#111a27;--border:#26364c;--text:#eef4ff;--muted:#8b9bb1;--primary:#718bff;--green:#55ddb0;--red:#ff7b8e;--yellow:#ffd177}
body.panel-shell{padding-left:0!important;background:radial-gradient(ellipse at 75% -20%,#27365b70,transparent 40%),radial-gradient(ellipse at 5% 90%,#0e55503b,transparent 30%),#080d16!important;color:var(--text)!important;min-height:100vh}
body.panel-shell .container{width:min(1380px,calc(100% - 275px))!important;max-width:none!important;margin:0 24px 0 245px!important;padding-top:27px!important}
#multi-rail{position:fixed;z-index:100;inset:0 auto 0 0;width:218px;padding:20px 14px 16px;background:#09111ded;border-right:1px solid #1c2b3f;backdrop-filter:blur(18px);display:flex;flex-direction:column;color:#edf4ff;font:12px/1.45 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
#multi-rail *{box-sizing:border-box}#multi-rail a{text-decoration:none;color:inherit}.mr-brand{display:flex;align-items:center;gap:10px;padding:0 8px 24px;font-size:14px;font-weight:800;letter-spacing:-.03em}.mr-logo{width:38px;height:38px;display:grid;place-items:center;border-radius:13px;background:linear-gradient(140deg,#68e2d1,#9483ff);color:#111728;font-size:13px;font-weight:900}.mr-brand small{display:block;margin-top:3px;color:#8092aa;font-size:8px;letter-spacing:.15em;font-weight:600}.mr-label{margin:13px 10px 7px;color:#596d88;font-size:8px;letter-spacing:.16em;font-weight:800}.mr-nav{display:grid;gap:4px}.mr-nav a,.mr-nav button{height:40px;display:flex;align-items:center;gap:10px;width:100%;border:1px solid transparent;border-radius:10px;background:transparent;color:#90a2bb;padding:0 10px;text-align:left;font:600 11px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer}.mr-nav a:hover,.mr-nav button:hover{background:#142033;color:#fff}.mr-nav a.active{border-color:#69e1d130;background:linear-gradient(90deg,#65e3d21b,#858fff0b);color:#f1f6ff;box-shadow:inset 2px 0 #68e2d1}.mr-icon{width:20px;text-align:center;color:#6fe0d2;font-size:14px}.mr-count{margin-left:auto;color:#73859d;font-size:9px}.mr-bottom{margin-top:auto}.mr-health{border:1px solid #26374c;border-radius:12px;background:linear-gradient(145deg,#122031,#101724);padding:11px;font-size:9px;font-weight:750}.mr-health i{display:inline-block;width:7px;height:7px;margin-right:7px;border-radius:50%;background:#55ddb0;box-shadow:0 0 10px #55ddb0aa}.mr-health.down i{background:#ff7b8e;box-shadow:0 0 10px #ff7b8eaa}.mr-health small{display:block;margin:5px 0 0 14px;color:#8294ab;font-size:8px;font-weight:500}
body.panel-shell .header{padding-bottom:14px;border-bottom:1px solid #1b293c;margin-bottom:25px}body.panel-shell .header .brand-title,body.panel-shell .page-title,body.panel-shell .title,body.panel-shell h1{color:#f2f6ff!important;letter-spacing:-.035em}body.panel-shell .page-subtitle,body.panel-shell .subtitle,body.panel-shell .muted{color:#91a2b9!important}body.panel-shell .header-button,body.panel-shell .action-button,body.panel-shell .btn,body.panel-shell .button,body.panel-shell button{transition:transform .15s,border-color .15s,background .15s}body.panel-shell .header-button:hover,body.panel-shell .action-button:hover,body.panel-shell .btn:hover,body.panel-shell .button:hover{transform:translateY(-1px)}
body.panel-shell .stat,body.panel-shell .section,body.panel-shell .card,body.panel-shell .user-card,body.panel-shell .user-card-item,body.panel-shell .event,body.panel-shell .source,body.panel-shell .trash-item,body.panel-shell .log-card{background:linear-gradient(145deg,#131f2e,#0f1723)!important;border-color:#26364c!important;border-radius:15px!important;color:#eaf2ff!important;box-shadow:0 12px 32px #0002}body.panel-shell input,body.panel-shell select,body.panel-shell textarea{background:#0a1320!important;border-color:#2a3a51!important;color:#edf4ff!important;border-radius:9px!important}body.panel-shell input::placeholder,body.panel-shell textarea::placeholder{color:#6f829d!important}body.panel-shell .status.enabled,body.panel-shell .status.active{color:#69e0b4!important}body.panel-shell .status.disabled{color:#ff8495!important}body.panel-shell .primary,body.panel-shell .header-button.primary{background:linear-gradient(100deg,#65e1d1,#8492ff)!important;border-color:transparent!important;color:#101725!important}
body.panel-shell .favorite-form{display:inline-flex}body.panel-shell .favorite-button{min-width:34px;border:1px solid #30415a;background:#172335;color:#ffd177;border-radius:9px;cursor:pointer;font-size:15px}body.panel-shell .favorite-button:hover{border-color:#ffd177}
@media(max-width:850px){body.panel-shell .container{width:calc(100% - 205px)!important;margin-left:190px!important;margin-right:15px!important}.mr-brand{padding-left:3px}.mr-nav a,.mr-nav button{font-size:10px}}
@media(max-width:650px){#multi-rail{width:58px;padding:14px 7px}.mr-brand{padding:0 0 20px}.mr-brand>div:last-child,.mr-label,.mr-text,.mr-count,.mr-bottom{display:none}.mr-logo{width:42px;height:42px}.mr-nav a,.mr-nav button{justify-content:center;padding:0}.mr-icon{font-size:16px}body.panel-shell .container{width:calc(100% - 78px)!important;margin:0 10px 0 68px!important;padding-top:14px!important}body.panel-shell .header{align-items:flex-start;flex-wrap:wrap;margin-bottom:18px}.header-actions{flex-wrap:wrap}.header-button-text{display:none!important}}
#multi-rail .mr-account{margin-top:10px;padding-top:10px;border-top:1px solid #1c2b3f}#multi-rail .mr-account form{margin:0}#multi-rail .mr-account button{height:38px;display:flex;align-items:center;gap:10px;width:100%;border:1px solid transparent;border-radius:10px;background:transparent;color:#90a2bb;padding:0 10px;text-align:left;font:600 11px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer}#multi-rail .mr-account button:hover{background:#251722;color:#ff9da8}#multi-rail .mr-account button:hover .mr-icon{color:#ff9da8}