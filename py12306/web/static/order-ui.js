/* Account login at top; booking controls in each task's expanded availability. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const api = async (url, method = 'GET', body) => {
    const options = {method, cache:'no-store', credentials:'same-origin', headers:{}};
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.headers['X-Py12306-Manage'] = '1';
      options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
    return data;
  };
  const tag = (name, text) => {
    const el = document.createElement(name);
    el.textContent = text;
    return el;
  };
  const tasksRoot = $('tasks');
  if (!tasksRoot) return;
  const header = document.querySelector('main header.top');
  const accountCard = document.createElement('section');
  accountCard.id = 'booking-account-card';
  accountCard.className = 'card';
  accountCard.innerHTML = [
    '<h2>12306 账号管理</h2>',
    '<div class="actions"><label for="booking-account">当前账号</label>',
    '<select id="booking-account" style="width:auto;min-width:170px;max-width:100%"></select>',
    '<button id="booking-qr-refresh" type="button" class="small">获取 / 刷新二维码</button></div>',
    '<p id="booking-qr-status" class="muted" role="status" style="font-size:13px;margin:10px 0"></p>',
    '<img id="booking-qr-image" alt="12306 登录二维码，请用铁路 12306 App 扫描" style="display:none;background:#fff;border-radius:8px;padding:8px;width:220px;max-width:100%;height:auto">',
    '<p id="booking-contacts-status" class="muted" style="font-size:12px"></p>',
    '<p id="booking-status" class="muted" role="status" style="font-size:13px"></p>',
    '<p id="booking-message" class="message" role="status"></p>'
  ].join('');
  header.insertAdjacentElement('afterend', accountCard);
  const styles=document.createElement('style');
  styles.textContent=[
    '.task-booking-controls{border:1px solid var(--border);border-radius:10px;margin:12px 0;padding:12px}',
    '.task-booking-controls .booking-members{display:flex;flex-wrap:wrap;gap:9px 15px;padding:10px 0}',
    '.task-booking-controls .booking-members label{display:inline-flex;align-items:center;gap:5px;font-size:13px;cursor:pointer}',
    '.task-booking-controls .booking-members input{width:auto}',
    '.task-booking-controls .booking-auto-actions{display:flex;gap:8px;flex-wrap:wrap}',
    '.task-booking-controls .booking-note{color:var(--muted);font-size:12px;margin-top:8px}',
    '#booking-account-card select{padding:7px 10px}'
  ].join('\n');
  document.head.append(styles);

  const storageKey='py12306:selected-account';
  const readPreferred=()=>{try{return localStorage.getItem(storageKey)||'';}catch(_){return '';}};
  const savePreferred=key=>{try{localStorage.setItem(storageKey,key);}catch(_){}};
  let state={accounts:[],tasks:[],order:{state:'idle'}};
  let accountKey=readPreferred(),knownSignature='';
  const selections=new Map(); // Per task/account, not submitted until explicitly confirmed.
  const currentAccount=()=>state.accounts.find(a=>a.key===accountKey);
  const taskFor=details=>state.tasks.find(t=>t.job_name===details.dataset.taskName);
  const selectionKey=name=>accountKey+':'+name;
  const selectedFor=task=>{
    const key=selectionKey(task.job_name);
    if(!selections.has(key)){
      const rule=task.auto_order||{};
      selections.set(key,rule.enabled && rule.account_key===accountKey ? [...(rule.members||[])] : []);
    }
    return selections.get(key);
  };
  const showMessage=message=>{$('booking-message').textContent=message;};

  function renderQr(){
    const a=currentAccount(),qr=a?.qr,img=$('booking-qr-image');
    const clear=()=>{img.style.display='none';img.removeAttribute('src');delete img.dataset.qr;};
    $('booking-qr-refresh').disabled=!qr||a.ready;
    if(!a){
      $('booking-qr-status').textContent='请在 env.py 配置 USER_ACCOUNTS，重启后刷新页面。';
      $('booking-contacts-status').textContent='';clear();return;
    }
    if(a.ready){
      $('booking-qr-status').textContent='已登录：'+a.label;
      $('booking-contacts-status').textContent=a.passengers.length
        ? '已自动加载 '+a.passengers.length+' 位常用联系人'
        : '正在自动加载常用联系人…';
      clear();return;
    }
    $('booking-contacts-status').textContent='';
    $('booking-qr-status').textContent=qr?.message||'等待扫码';
    if(qr?.has_image){
      const url='/manage/api/booking/qr/'+encodeURIComponent(a.key)+'/image?v='+encodeURIComponent(qr.version);
      if(img.dataset.qr!==url){img.src=url;img.dataset.qr=url;}
      img.style.display='block';
    } else clear();
  }

  function updateAccounts(){
    const select=$('booking-account');
    select.replaceChildren();
    for(const a of state.accounts){
      const option=tag('option',a.label+(a.ready?'（已登录）':'（未登录）'));
      option.value=a.key;select.append(option);
    }
    if(!state.accounts.some(a=>a.key===accountKey))accountKey=state.accounts[0]?.key||'';
    select.value=accountKey;
  }

  function renderOrderStatus(){
    const order=state.order||{};
    const labels={idle:'空闲',working:'下单或排队中',success:'已取得订单号，待支付',failed:'下单结果待核实'};
    let message='订单状态：'+(labels[order.state]||'未知');
    if(order.train)message+=' · '+order.train+' '+order.seat;
    if(order.order_id)message+=' · 订单号：'+order.order_id;
    if(order.message)message+=' · '+order.message;
    $('booking-status').textContent=message;
  }

  function getTaskControls(details){
    let controls=details.querySelector('.task-booking-controls');
    if(controls)return controls;
    controls=document.createElement('section');
    controls.className='task-booking-controls';
    controls.innerHTML=[
      '<div class="label">乘车人（多选，最多 5 位）</div>',
      '<div class="booking-members"></div>',
      '<div class="booking-auto-actions">',
      '<button type="button" class="primary booking-auto-start small">启用自动抢票</button>',
      '<button type="button" class="booking-auto-stop small">关闭自动抢票</button>',
      '</div><p class="booking-note" role="status"></p>'
    ].join('');
    details.insertBefore(controls,details.querySelector('.task-availability-content'));
    controls.querySelector('.booking-auto-start').addEventListener('click',()=>saveAuto(details,true));
    controls.querySelector('.booking-auto-stop').addEventListener('click',()=>saveAuto(details,false));
    return controls;
  }

  function renderTaskControls(details,force=false){
    if(!details.open)return;
    const task=taskFor(details),a=currentAccount();
    if(!task)return;
    const controls=getTaskControls(details),list=controls.querySelector('.booking-members');
    const signature=JSON.stringify([a?.key,a?.passengers||[]]);
    if(force||list.dataset.signature!==signature){
      list.dataset.signature=signature;
      list.replaceChildren();
      if(!a?.ready)list.append(tag('span','请先在顶部完成 12306 登录'));
      else if(!a.passengers?.length)list.append(tag('span','正在自动加载常用联系人…'));
      else{
        const allowed=new Set(a.passengers.map(p=>p.value));
        selections.set(selectionKey(task.job_name),selectedFor(task).filter(x=>allowed.has(x)));
        for(const passenger of a.passengers){
          const label=document.createElement('label'),checkbox=document.createElement('input');
          checkbox.type='checkbox';checkbox.value=passenger.value;
          checkbox.checked=selectedFor(task).includes(passenger.value);
          checkbox.addEventListener('change',()=>{
            const chosen=selectedFor(task);
            if(checkbox.checked&&!chosen.includes(checkbox.value))chosen.push(checkbox.value);
            if(!checkbox.checked){
              const index=chosen.indexOf(checkbox.value);
              if(index>=0)chosen.splice(index,1);
            }
            if(chosen.length>5){checkbox.checked=false;chosen.splice(chosen.indexOf(checkbox.value),1);alert('最多选择 5 位乘车人');}
          });
          label.append(checkbox,document.createTextNode(passenger.name));
          list.append(label);
        }
      }
    }
    const rule=task.auto_order||{},enabled=!!rule.enabled;
    controls.querySelector('.booking-note').textContent=enabled
      ? '自动抢票已开启'+(rule.account_key!==accountKey?'（绑定了其他账号）':'')
      : '自动抢票未开启。勾选乘车人后，可在余票表格点击「预订」。';
    controls.querySelector('.booking-auto-start').disabled=state.order?.state==='working'||!a?.ready||!a.passengers?.length;
    controls.querySelector('.booking-auto-stop').disabled=!enabled;
  }
  function renderAllControls(force=false){
    for(const details of tasksRoot.querySelectorAll('details.task-availability'))
      if(details.open)renderTaskControls(details,force);
  }

  async function refreshState(){
    try{
      const next=await api('/manage/api/booking/state');
      const signature=JSON.stringify([
        next.accounts.map(a=>[a.key,a.label,a.ready,a.passengers]),
        next.tasks.map(t=>[t.index,t.job_name,t.auto_order,t.enabled])
      ]);
      state=next;
      if(knownSignature!==signature){
        knownSignature=signature;
        updateAccounts();
        renderAllControls();
      }
      renderQr();renderOrderStatus();renderAllControls();
    }catch(error){showMessage('加载账号或订单状态失败：'+error.message);}
  }

  async function saveAuto(details,enabled){
    const task=taskFor(details),a=currentAccount();
    if(!task){showMessage('查询任务已改变，请刷新');return;}
    const members=selectedFor(task);
    if(enabled){
      if(!a?.ready||!members.length||members.length>5){alert('请选择已登录账号及 1～5 位乘车人');return;}
      if(state.order?.state==='failed'&&!confirm('上次下单结果不确定，请在 12306 官方核对后继续。'))return;
      if(!confirm('为“'+task.job_name+'”开启自动抢票？符合条件时将直接提交订单，但不自动付款。'))return;
    }
    try{
      await api('/manage/api/booking/auto/'+task.index,'PUT',{
        enabled,account_key:accountKey,members:enabled?[...members]:[]
      });
      showMessage(enabled?'已开启 '+task.job_name+' 自动抢票':'已关闭 '+task.job_name+' 自动抢票');
      await refreshState();
    }catch(error){showMessage('保存自动抢票失败：'+error.message);}
  }

  async function bookManual(details,date,train,seat){
    const task=taskFor(details),a=currentAccount();
    if(!task||!a?.ready){alert('请先在顶部完成 12306 登录');return;}
    const members=selectedFor(task);
    if(!members.length||members.length>5){alert('请先在此任务中勾选 1～5 位乘车人');return;}
    if(state.order?.state==='working'){alert('已有订单正在排队');return;}
    if(state.order?.state==='failed'&&!confirm('此前下单结果不确定，请先在 12306 官方核对订单后再继续。'))return;
    if(!confirm('确认提交订单？\n'+date+' · '+train+' · '+seat+' · '+members.length+' 位乘车人\n成功后需要自行付款。'))return;
    try{
      await api('/manage/api/booking/manual','POST',{
        job_name:task.job_name,date,train,seat,account_key:accountKey,members:[...members]
      });
      showMessage('已开始下单，请等待 12306 排队结果');
      await refreshState();
    }catch(error){showMessage('手动下单失败：'+error.message);alert(error.message);}
  }

  function decorate(){
    for(const details of tasksRoot.querySelectorAll('details.task-availability')){
      if(!details.open)continue;
      if(!details.querySelector('.task-booking-controls'))renderTaskControls(details);
      for(const table of details.querySelectorAll('table.availability-table')){
        const headers=[...table.querySelectorAll('thead th')]
          .map(th=>th.textContent.trim().replace(/\s*[↕↑↓]\s*$/,''));
        if(headers.includes('预订'))continue;
        const trainIdx=headers.indexOf('车次'),seatIdx=headers.indexOf('席别'),qtyIdx=headers.indexOf('余票');
        if([trainIdx,seatIdx,qtyIdx].some(i=>i<0))continue;
        table.querySelector('thead tr').append(tag('th','预订'));
        let heading=table.closest('.table-scroll')?.previousElementSibling;
        while(heading&&!heading.matches('h3.route-title'))heading=heading.previousElementSibling;
        const route=heading?.textContent.match(/ · (\d{4}-\d{2}-\d{2})\s/);
        for(const tr of table.querySelectorAll('tbody tr')){
          if(tr.cells.length!==headers.length){
            if(tr.cells.length===1)tr.cells[0].colSpan=headers.length+1;
            continue;
          }
          const td=document.createElement('td'),train=tr.cells[trainIdx].textContent.trim();
          const seat=tr.cells[seatIdx].textContent.trim(),qty=tr.cells[qtyIdx].textContent.trim();
          const canBook=qty==='有'||(/^\d+$/.test(qty)&&Number(qty)>0);
          const button=tag('button','预订');button.type='button';button.className='small';
          button.disabled=!canBook||!route;
          button.addEventListener('click',()=>bookManual(details,route[1],train,seat));
          td.append(button);tr.append(td);
        }
      }
    }
  }

  $('booking-account').addEventListener('change',()=>{
    accountKey=$('booking-account').value;
    savePreferred(accountKey);selections.clear();
    renderQr();renderAllControls(true);
  });
  $('booking-qr-refresh').addEventListener('click',async()=>{
    if(!accountKey)return;
    $('booking-qr-refresh').disabled=true;
    try{
      const result=await api('/manage/api/booking/qr/'+encodeURIComponent(accountKey)+'/refresh','POST',{});
      $('booking-qr-status').textContent=result.message||'正在生成二维码';
      await refreshState();
    }catch(error){showMessage('二维码刷新失败：'+error.message);}
    finally{renderQr();}
  });
  const observer=new MutationObserver(decorate);
  observer.observe(tasksRoot,{childList:true,subtree:true});
  refreshState().then(decorate);
  setInterval(()=>{if(!document.hidden)refreshState();},5000);
})();