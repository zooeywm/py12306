/* Booking UI is isolated from the live table's sorting and filtering implementation. */
(() => {
  'use strict';
  const api = async (url, method = 'GET', body) => {
    const options = {method, credentials: 'same-origin', cache: 'no-store', headers: {}};
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-Py12306-Manage'] = '1';
      options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  };
  const tag = (name, text) => {
    const el = document.createElement(name);
    el.textContent = text;
    return el;
  };
  const card = document.createElement('section');
  card.className = 'card';
  card.id = 'booking-panel';
  card.innerHTML = `
    <h2>下单控制</h2>
    <p class="muted" style="font-size:13px">手动：点击余票列表的「预订」。自动：选择任务并明确启用。不会自动支付。</p>
    <p id="booking-login" class="note"></p><button type="button" class="small" id="booking-load-contacts">加载常用联系人</button>
    <div id="booking-status" role="status" class="muted" style="font-size:13px;padding:8px 0"></div>
    <div class="form-grid" id="booking-manual" style="display:none;margin:12px 0">
      <div class="wide"><strong>手动下单</strong><p id="booking-summary" class="muted"></p></div>
      <div class="field"><label for="booking-account">12306 账号</label><select id="booking-account"></select></div>
      <div class="field wide"><span class="label">乘车人（1～5 位）</span><div class="seats" id="booking-contacts"></div></div>
      <div class="wide actions"><button type="button" class="primary" id="booking-confirm">确认提交到 12306</button><button type="button" id="booking-cancel">取消</button></div>
    </div>
    <hr style="border:0;border-top:1px solid var(--border);margin:16px 0">
    <h3 style="font-size:15px">自动抢票</h3>
    <div class="form-grid">
      <div class="field"><label for="booking-task">监控任务</label><select id="booking-task"></select></div>
      <div class="field"><label for="booking-auto-account">12306 账号</label><select id="booking-auto-account"></select></div>
      <div class="field wide"><span class="label">乘车人（1～5 位）</span><div class="seats" id="booking-auto-contacts"></div></div>
      <div class="wide"><p id="booking-auto-info" class="muted" style="font-size:13px"></p><div class="actions"><button type="button" class="primary" id="booking-auto-start">启用自动抢票</button><button type="button" id="booking-auto-stop">关闭自动抢票</button></div></div>
    </div>
    <div id="booking-message" class="message" role="status"></div>`;
  const banner = document.querySelector('main > p.note');
  if (banner) banner.textContent = '任务默认只监控；订单仅在手动确认或显式开启自动抢票后提交。不会自动支付。页面仅限本机使用。';
  const avail = document.getElementById('availability-data');
  if (!avail) return;
  avail.closest('section.card').insertAdjacentElement('afterend', card);
  const $ = id => document.getElementById(id);
  const styles = document.createElement('style');
  styles.textContent = '#booking-panel .seats label{cursor:pointer} #booking-panel .field{min-width:0} #booking-panel .booking-strong{color:var(--accent);font-weight:650}';
  document.head.append(styles);

  let state = {accounts: [], tasks: [], order: {state: 'idle'}};
  let current = null;
  let lastListSignature = '';
  const option = (value, label) => {
    const el = document.createElement('option');
    el.value = String(value); el.textContent = label;
    return el;
  };
  const account = key => state.accounts.find(item => item.key === key);
  const selectedMembers = id => [...$(id).querySelectorAll('input:checked')].map(el => el.value);
  const chooseAccount = (id, previous) => {
    const select = $(id);
    select.replaceChildren(option('', '请选择账号'));
    for (const a of state.accounts) select.append(option(a.key, a.label + (a.ready ? '（已登录）' : '（未登录）')));
    select.value = state.accounts.some(a => a.key === previous) ? previous : '';
  };
  const updateContacts = (targetId, accountKey, chosen = []) => {
    const root = $(targetId);
    root.replaceChildren();
    const a = account(accountKey);
    if (!a || !a.ready) { root.append(tag('span', '请先完成 12306 登录')); return; }
    if (!a.passengers.length) {root.append(tag('span', '联系人尚未加载，请稍后刷新'));return;}
    a.passengers.forEach(passenger => {
      const label = document.createElement('label'), checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.value = passenger.value;
      checkbox.checked = chosen.includes(passenger.value);
      label.append(checkbox, document.createTextNode(passenger.name));
      root.append(label);
    });
  };
  const currentTask = () => state.tasks.find(t => String(t.index) === $('booking-task').value);
  const loadAuto = () => {
    const task = currentTask();
    const rule = task?.auto_order || {};
    chooseAccount('booking-auto-account', rule.account_key || '');
    updateContacts('booking-auto-contacts', $('booking-auto-account').value, rule.members || []);
    $('booking-auto-info').textContent = task
      ? `席别：${task.seats.join(' / ') || '未配置'} · 当前状态：${rule.enabled ? '自动抢票已开启' : '未开启'}${task.enabled ? '' : '（任务已暂停）'}`
      : '尚未创建查询任务';
  };
  const updateStatus = order => {
    const label = {idle:'空闲',working:'提交或排队中',success:'已提交成功，待支付',failed:'下单未确认成功'}[order.state] || '未知';
    $('booking-status').replaceChildren(tag('strong', '订单状态：' + label));
    $('booking-status').append(tag('span', ' · ' + (order.message || '')));
    if (order.train) $('booking-status').append(tag('span', ` · ${order.train} ${order.seat}`));
    if (order.order_id) $('booking-status').append(tag('span', ' · 订单号：' + order.order_id));
    $('booking-confirm').disabled = order.state === 'working';
    $('booking-auto-start').disabled = order.state === 'working';
  };
  const refreshState = async (force = false) => {
    try {
      const newState = await api('/manage/api/booking/state');
      const signature = JSON.stringify([newState.accounts, newState.tasks]);
      state = newState;
      $('booking-login').textContent = state.accounts.length
        ? '12306 账号准备状态见下方账号选择框。请先保证账号已扫码登录。'
        : '暂未配置 12306 账号。请在 env.py 中配置 USER_ACCOUNTS（type: qr），重启后扫码登录。';
      updateStatus(state.order || {});
      if (force || signature !== lastListSignature) {
        lastListSignature = signature;
        const prevManual = $('booking-account').value;
        const prevTask = $('booking-task').value;
        chooseAccount('booking-account', prevManual);
        updateContacts('booking-contacts', $('booking-account').value, selectedMembers('booking-contacts'));
        const taskSelect = $('booking-task');
        taskSelect.replaceChildren(option('', '请选择任务'));
        for (const t of state.tasks) taskSelect.append(option(t.index, t.job_name + ' · ' + t.date));
        taskSelect.value = state.tasks.some(t => String(t.index) === prevTask) ? prevTask : '';
        if (!taskSelect.value && state.tasks.length) taskSelect.value = String(state.tasks[0].index);
        loadAuto();
      }
    } catch (e) { $('booking-message').textContent = '获取下单状态失败：' + e.message; }
  };

  $('booking-load-contacts').addEventListener('click', async () => {
    const accountKey = ($('booking-manual').style.display === 'none'
      ? $('booking-auto-account').value : $('booking-account').value)
      || $('booking-auto-account').value || $('booking-account').value;
    if (!accountKey) {alert('先在账号选择框选择 12306 账号');return;}
    try {
      await api('/manage/api/booking/contacts', 'POST', {account_key: accountKey});
      $('booking-message').textContent = '联系人已加载';
      await refreshState(true);
    } catch (e) { $('booking-message').textContent = e.message; }
  });
  $('booking-task').addEventListener('change', loadAuto);
  $('booking-account').addEventListener('change', () => updateContacts('booking-contacts', $('booking-account').value));
  $('booking-auto-account').addEventListener('change', () => updateContacts('booking-auto-contacts', $('booking-auto-account').value));
  $('booking-cancel').addEventListener('click', () => {current = null; $('booking-manual').style.display = 'none';});
  $('booking-confirm').addEventListener('click', async () => {
    if (!current) return;
    const members = selectedMembers('booking-contacts');
    const accountKey = $('booking-account').value;
    if (!accountKey || !members.length || members.length > 5) {alert('请选择已登录账号及 1～5 位乘车人');return;}
    const info = `${current.date} ${current.train} ${current.seat}，${members.length} 位乘车人`;
    if (state.order?.state === 'failed' && !confirm('之前存在未确认成功的订单。请先到 12306 官方未支付订单核对，确认没有订单后再继续。')) return;
    if (!confirm(`确定将以下订单提交到 12306？\n${info}\n提交成功后需要自行支付。`)) return;
    $('booking-confirm').disabled = true;
    try {
      await api('/manage/api/booking/manual', 'POST', {...current, account_key: accountKey, members});
      $('booking-message').textContent = '已开始提交，等待 12306 排队结果';
      $('booking-manual').style.display = 'none'; current = null;
      await refreshState();
    } catch (e) { $('booking-message').textContent = '手动下单失败：' + e.message; alert(e.message); }
    finally { $('booking-confirm').disabled = false; }
  });
  const saveAuto = async enabled => {
    const task = currentTask();
    if (!task) {alert('请选择一个监控任务');return;}
    const accountKey = $('booking-auto-account').value;
    const members = selectedMembers('booking-auto-contacts');
    if (enabled && (!accountKey || !members.length || members.length > 5)) {alert('请选择已登录账号及 1～5 位乘车人');return;}
    if (enabled && state.order?.state === 'failed' && !confirm('上次下单未确认成功。请先确认 12306 订单列表没有未支付订单，再重新启用。')) return;
    if (enabled && !confirm(`为任务“${task.job_name}”开启自动提交订单？\n监控到符合条件的余票后将无需再次确认，直接尝试下单（不自动付款）。`)) return;
    try {
      await api('/manage/api/booking/auto/' + task.index, 'PUT', {enabled, account_key: accountKey, members});
      $('booking-message').textContent = enabled ? '自动抢票已开启' : '自动抢票已关闭（正在处理的订单无法撤回）';
      await refreshState(true);
    } catch (e) { $('booking-message').textContent = '保存自动抢票失败：' + e.message; }
  };
  $('booking-auto-start').addEventListener('click', () => saveAuto(true));
  $('booking-auto-stop').addEventListener('click', () => saveAuto(false));

  // The availability renderer replaces the table every five seconds; decorate
  // only new tables without interfering with column sorting and filters.
  const decorate = () => {
    for (const table of avail.querySelectorAll('table.availability-table')) {
      const header = [...table.querySelectorAll('thead th')].map(th => th.textContent.trim());
      if (header.includes('预订')) continue;
      const trainIdx = header.indexOf('车次');
      const seatIdx = header.indexOf('席别');
      const quantityIdx = header.indexOf('余票');
      if ([trainIdx, seatIdx, quantityIdx].some(i => i < 0)) continue;
      const cell = document.createElement('th');cell.textContent = '预订';
      table.querySelector('thead tr').append(cell);
      let node = table.closest('.table-scroll')?.previousElementSibling;
      while (node && !(node.matches('h3.route-title'))) node = node.previousElementSibling;
      const match = node?.textContent.match(/^(.*) · (\d{4}-\d{2}-\d{2})\s/);
      table.querySelectorAll('tbody tr').forEach(tr => {
        const td = document.createElement('td');
        if (match && tr.cells.length > Math.max(trainIdx, seatIdx, quantityIdx)) {
          const train = tr.cells[trainIdx].textContent.trim();
          const seat = tr.cells[seatIdx].textContent.trim();
          const qty = tr.cells[quantityIdx].textContent.trim();
          const canBook = qty === '有' || (/^\d+$/.test(qty) && Number(qty) > 0);
          const button = tag('button', '预订');
          button.type = 'button';button.className = 'small';button.disabled = !canBook;
          button.addEventListener('click', () => {
            current = {job_name: match[1], date: match[2], train, seat};
            $('booking-summary').textContent = `${current.date} · ${current.train} · ${current.seat} · ${qty} 张/有票`;
            $('booking-manual').style.display = 'grid';
            $('booking-message').textContent = '';
            card.scrollIntoView({behavior:'smooth',block:'start'});
          });
          td.append(button);
        }
        tr.append(td);
      });
    }
  };
  const observer = new MutationObserver(decorate);
  observer.observe(avail, {childList:true,subtree:true});
  decorate();
  refreshState();
  setInterval(() => {if (!document.hidden) refreshState();}, 5000);
})();
