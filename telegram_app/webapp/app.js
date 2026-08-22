const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand();
const headers = () => ({"Content-Type":"application/json", "X-Telegram-Init-Data": tg?.initData || ""});
const $ = (id) => document.getElementById(id);
let thresholdDirty = false;
let refreshDirty = false;
const DRAFT_THRESHOLD = "paychain.threshold.draft";
const DRAFT_REFRESH = "paychain.refresh.draft";
const rememberDraft = (id, key) => { try { localStorage.setItem(key, $(id).value); } catch (_) {} };
const loadDraft = (id, key) => { try { const value = localStorage.getItem(key); if (value !== null && value !== "") $(id).value = value; } catch (_) {} };
$("threshold").addEventListener("input", () => { thresholdDirty = true; rememberDraft("threshold", DRAFT_THRESHOLD); });
$("threshold").addEventListener("change", () => { thresholdDirty = true; rememberDraft("threshold", DRAFT_THRESHOLD); command("set_threshold"); });
$("refresh_seconds").addEventListener("input", () => { refreshDirty = true; rememberDraft("refresh_seconds", DRAFT_REFRESH); });
$("refresh_seconds").addEventListener("change", () => { refreshDirty = true; rememberDraft("refresh_seconds", DRAFT_REFRESH); command("set_threshold"); });
async function request(path, options = {}) { const r = await fetch(path, {...options, headers:{...headers(), ...(options.headers||{})}}); const data = await r.json(); if (!r.ok) throw new Error(data.detail || "Помилка"); return data; }
async function refresh() { try { const s = await request('/api/state'); if (!thresholdDirty && document.activeElement !== $('threshold')) $('threshold').value = s.threshold; if (!refreshDirty && document.activeElement !== $('refresh_seconds')) $('refresh_seconds').value = s.refresh_seconds; $('status').textContent = s.connected ? s.status : (s.paired ? 'Очікування агента' : 'Готово до підключення'); $('substatus').textContent = s.connected ? (s.running ? 'Моніторинг увімкнений' : 'Моніторинг зупинений') : (s.paired ? 'Запустіть агент на ПК' : 'Натисніть «Підключити цей ПК»'); $('dot').className = `dot ${s.connected ? 'ok' : ''}`; $('pair').hidden = s.connected; $('pair').disabled = s.paired && s.connected; $('pair').textContent = 'Підключити цей ПК'; $('disconnect').hidden = !s.paired; } catch(e) { $('status').textContent='Не вдалося отримати стан'; } }
async function command(action) { try { const threshold=Number($('threshold').value.replace(',', '.')); const refreshSeconds=Number($('refresh_seconds').value.replace(',', '.')); if (!Number.isFinite(threshold) || threshold < 0) throw new Error('Введіть коректну суму.'); if (!Number.isFinite(refreshSeconds) || refreshSeconds < 1) throw new Error('Введіть інтервал від 1 секунди.'); await request('/api/command',{method:'POST',body:JSON.stringify({action,threshold,refresh_seconds:refreshSeconds})}); thresholdDirty = false; refreshDirty = false; try { localStorage.removeItem(DRAFT_THRESHOLD); localStorage.removeItem(DRAFT_REFRESH); } catch (_) {} await refresh(); } catch(e) { const message = e.message === 'Локальний агент не підключений.' ? 'Спочатку натисніть «Підключити цей ПК».' : e.message; tg?.showAlert?.(message) || alert(message); } }
$('login').onclick=()=>command('open_login'); $('start').onclick=()=>command('start'); $('stop').onclick=()=>command('stop');
 $('disconnect').onclick=()=>{ if(confirm('Відключити цей ПК? Моніторинг зупиниться, локальний вхід Paychain буде видалено, і наступного разу потрібно буде увійти знову.')) command('disconnect'); };
 $('pair').onclick=async()=>{ try {
   const c=await request('/api/pair',{method:'POST'});
   const config={server_ws_url:location.origin.replace('https','wss').replace('http','ws')+'/ws/agent',agent_id:c.agent_id,agent_token:c.agent_token};
   const blob=new Blob([JSON.stringify(config,null,2)],{type:'application/json'});
   const link=document.createElement('a'); link.href=URL.createObjectURL(blob); link.download='agent-config.json';
   document.body.appendChild(link); link.click(); setTimeout(()=>{ URL.revokeObjectURL(link.href); link.remove(); },1000);
   $('credentials').hidden=false; $('credentials').textContent='Файл передано агенту. Очікую автоматичне підключення…'; $('pair').disabled=true;
   for (let attempt=0; attempt<12; attempt++) {
     await new Promise(resolve=>setTimeout(resolve,1000));
     const state=await request('/api/state');
     if (state.connected) { $('credentials').textContent='Агент підключено. Можна відкривати Paychain або запускати алгоритм.'; await refresh(); return; }
   }
   $('credentials').textContent='Файл завантажено. Якщо підключення не з’явилось за 12 секунд, запустіть агент через START/install_agent.cmd один раз.';
   await refresh();
 } catch(e) { $('pair').disabled=false; alert(e.message); } };
loadDraft("threshold", DRAFT_THRESHOLD); loadDraft("refresh_seconds", DRAFT_REFRESH);
refresh(); setInterval(refresh, 5000);
