// Comportamento da página. Fica em arquivo próprio para a CSP poder bloquear scripts inline.
document.addEventListener('DOMContentLoaded', () => {
  // Abas: cada .tab-shell controla só os próprios botões e painéis
  document.querySelectorAll('.tab-shell').forEach((shell) => {
    const buttons = shell.querySelectorAll(':scope > .tab-nav .tab-btn');
    const panels = shell.querySelectorAll(':scope > .tab-content');
    const activate = (tabName) => {
      buttons.forEach((button) => button.classList.toggle('active', button.dataset.tab === tabName));
      panels.forEach((panel) => panel.classList.toggle('active', panel.id === `tab-${tabName}`));
    };
    buttons.forEach((button) => button.addEventListener('click', () => activate(button.dataset.tab)));
    if (shell.dataset.initial) {
      activate(`mode-${shell.dataset.initial}`);
    }
  });

  const hideGood = document.getElementById('hide-good');
  if (hideGood) {
    const apply = () => {
      document.querySelectorAll('#interval-rows tr').forEach((row) => {
        row.hidden = hideGood.checked && row.dataset.state === 'GOOD';
      });
    };
    hideGood.addEventListener('change', apply);
    apply();
  }

  // Modal de carregamento enquanto o relatório é gerado (o POST só volta quando termina)
  const form = document.getElementById('main-form');
  const modal = document.getElementById('loading-modal');
  const loadingTitle = document.getElementById('loading-title');
  const loadingElapsed = document.getElementById('loading-elapsed');
  const loadingGroup = document.getElementById('loading-group');
  const loadingPeriod = document.getElementById('loading-period');
  let loadingTimer = null;

  const formatLocal = (value) => {
    if (!value) return '—';
    const [date, time] = value.split('T');
    const [year, month, day] = date.split('-');
    return `${day}/${month}/${year} ${time || ''}`.trim();
  };

  const closeLoading = () => {
    clearInterval(loadingTimer);
    if (modal.open) modal.close();
    form.querySelectorAll('button').forEach((button) => { button.disabled = false; });
  };

  // O modal não pode ser fechado com Esc enquanto o relatório é gerado
  modal.addEventListener('cancel', (event) => event.preventDefault());

  form.addEventListener('submit', (event) => {
    const submitter = event.submitter;
    if (!submitter || !submitter.dataset.loading) return;
    const groupSelect = document.getElementById('group-select');
    const groupOption = groupSelect.options[groupSelect.selectedIndex];
    loadingTitle.textContent = `${submitter.dataset.loading}…`;
    loadingGroup.textContent = groupOption ? groupOption.text : '—';
    loadingPeriod.textContent = `${formatLocal(form.elements.start.value)} até ${formatLocal(form.elements.end.value)}`;

    const startedAt = Date.now();
    const tick = () => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      const mm = String(Math.floor(seconds / 60)).padStart(2, '0');
      const ss = String(seconds % 60).padStart(2, '0');
      loadingElapsed.textContent = `${mm}:${ss}`;
    };
    tick();
    loadingTimer = setInterval(tick, 1000);
    modal.showModal();
    // Desabilita depois do envio para não perder o formaction do botão clicado
    setTimeout(() => form.querySelectorAll('button').forEach((button) => { button.disabled = true; }), 0);
  });

  // Ao voltar pelo histórico do navegador a página pode vir do cache com o modal aberto
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) closeLoading();
  });

  // Conexão: ao ter URL, usuário e senha, carrega todos os host groups
  const connectBtn = document.getElementById('connect-btn');
  const status = document.getElementById('connect-status');
  const select = document.getElementById('group-select');
  const filter = document.getElementById('group-filter');
  const connectionFields = form.querySelectorAll('[data-connection]');
  const passwordField = form.elements.password;
  const logoutBtn = document.getElementById('logout-btn');
  // A senha fica só na sessão do servidor; o navegador recebe apenas "conectado ou não"
  let connected = form.dataset.connected === '1';
  let groups = [];
  let lastConnection = '';

  const applyConnectedState = () => {
    passwordField.required = !connected;
    passwordField.placeholder = connected ? 'Sessão ativa: deixe em branco para manter' : '';
    logoutBtn.hidden = !connected;
  };
  applyConnectedState();

  const setStatus = (text, kind) => {
    status.textContent = text;
    status.className = `connect-status ${kind || ''}`;
  };

  const renderGroups = () => {
    const term = filter.value.trim().toLowerCase();
    const current = select.value;
    const visible = groups.filter((g) => !term || g.name.toLowerCase().includes(term) || g.groupid === term);
    select.replaceChildren();
    const placeholder = new Option(visible.length ? 'Selecione um grupo' : 'Nenhum grupo encontrado', '');
    select.add(placeholder);
    visible.forEach((g) => select.add(new Option(`${g.name} (${g.groupid})`, g.groupid)));
    // Mantém a seleção atual mesmo que o filtro a esconda
    if (current && !visible.some((g) => g.groupid === current)) {
      const selected = groups.find((g) => g.groupid === current);
      if (selected) select.add(new Option(`${selected.name} (${selected.groupid})`, selected.groupid));
    }
    select.value = current;
  };

  const connect = async (force) => {
    const data = new FormData();
    let complete = true;
    connectionFields.forEach((field) => {
      const optional = field === passwordField && connected;
      if (!optional && !field.value.trim()) complete = false;
      data.append(field.name, field.value);
    });
    if (!complete) {
      if (force) setStatus('Preencha URL, usuário e senha.', 'error');
      return;
    }
    const signature = [...connectionFields].map((f) => f.value).join('\u0000');
    if (!force && signature === lastConnection) return;
    lastConnection = signature;

    connectBtn.disabled = true;
    setStatus('Conectando…');
    try {
      const response = await fetch('/api/groups', { method: 'POST', body: data });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
      groups = body.groups;
      filter.disabled = false;
      renderGroups();
      connected = true;
      passwordField.value = '';
      lastConnection = [...connectionFields].map((f) => f.value).join('\u0000');
      applyConnectedState();
      setStatus(`Conectado. ${groups.length} grupos carregados.`, 'ok');
    } catch (err) {
      lastConnection = '';
      setStatus(`Falha ao conectar: ${err.message}`, 'error');
    } finally {
      connectBtn.disabled = false;
    }
  };

  connectBtn.addEventListener('click', () => connect(true));
  connectionFields.forEach((field) => {
    field.addEventListener('change', () => connect(false));
    field.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        connect(true);
      }
    });
  });
  filter.addEventListener('input', renderGroups);

  logoutBtn.addEventListener('click', async () => {
    logoutBtn.disabled = true;
    try {
      await fetch('/api/logout', { method: 'POST' });
    } finally {
      connected = false;
      groups = [];
      lastConnection = '';
      filter.value = '';
      filter.disabled = true;
      select.replaceChildren(new Option('Conecte para carregar os grupos', ''));
      applyConnectedState();
      logoutBtn.disabled = false;
      setStatus('Desconectado. A senha foi apagada do servidor.');
    }
  });

  // Depois de gerar um relatório os campos voltam preenchidos: reconecta sozinho
  connect(false);
});
