"use strict";
const $ = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let S = null, formsFilled = false, timer = null;

function toast(msg, kind=""){
  const d = document.createElement("div"); d.className = kind; d.textContent = msg;
  $("#toast").appendChild(d); setTimeout(() => d.remove(), kind === "err" ? 9000 : 4500);
}
async function api(path, body){
  const opt = body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json","X-Poster":"1"}, body:JSON.stringify(body)};
  const r = await fetch(path, opt);
  let j = {}; try { j = await r.json(); } catch(e) {}
  if (r.status === 401) { showAuth(); throw new Error("Потрібно увійти"); }
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
const SLOW = {start: "Запускаю бота…", stop: "Зупиняю бота…", restart: "Перезапускаю бота…"};
async function run(fn, btn, slow){
  let label = null;
  if (btn) { btn.disabled = true; if (slow) { label = btn.innerHTML; btn.innerHTML = '<span class="spin"></span>' + esc(slow); } }
  if (slow) toast("⏳ " + slow + " Це може тривати до 30 секунд.");
  try { const j = await fn(); if (j && j.message) toast(j.message, "okk"); await refresh(); return j; }
  catch(e){ toast(e.message, "err"); }
  finally { if (btn) { btn.disabled = false; if (label !== null) btn.innerHTML = label; } }
}

/* ---------- вхід ---------- */
async function boot(){
  const s = await api("/api/session");
  if (s.version) $("#ver").textContent = "версія " + s.version;
  if (!s.password) document.querySelectorAll("[data-pw-only]").forEach(e => e.classList.add("hidden"));
  if (s.authed) return showApp();
  showAuth(s.has_password);
}
function showAuth(hasPw){
  clearInterval(timer);
  $("#app").classList.add("hidden"); $("#auth").classList.remove("hidden");
  const setup = hasPw === false;
  $("#auth").dataset.setup = setup ? "1" : "";
  $("#authText").textContent = setup ? "Перший вхід: створіть пароль для панелі керування (щонайменше 4 символи)." : "Введіть пароль панелі керування.";
  $("#pw2wrap").classList.toggle("hidden", !setup);
  $("#authBtn").textContent = setup ? "Створити пароль" : "Увійти";
  $("#pw1").autocomplete = setup ? "new-password" : "current-password";
  $("#pw1").focus();
}
$("#authForm").addEventListener("submit", async ev => {
  ev.preventDefault(); $("#authErr").textContent = "";
  const setup = $("#auth").dataset.setup === "1", pw = $("#pw1").value;
  if (setup && pw !== $("#pw2").value) { $("#authErr").textContent = "Паролі не збігаються"; return; }
  try { await api(setup ? "/api/setup" : "/api/login", {password: pw}); $("#pw1").value = $("#pw2").value = ""; showApp(); }
  catch(e){ $("#authErr").textContent = e.message; }
});
function showApp(){
  $("#auth").classList.add("hidden"); $("#app").classList.remove("hidden");
  formsFilled = false; refresh(); clearInterval(timer); timer = setInterval(refresh, 4000);
}

/* ---------- вкладки ---------- */
$("#tabs").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-tab]"); if (!b) return;
  $$("#tabs button").forEach(x => x.classList.toggle("on", x === b));
  $$("section[data-page]").forEach(p => p.classList.toggle("hidden", p.dataset.page !== b.dataset.tab));
  try { localStorage.setItem("tab", b.dataset.tab); } catch(e) {}
});
try { const t = localStorage.getItem("tab"); if (t) $(`#tabs button[data-tab="${t}"]`)?.click(); } catch(e) {}

/* ---------- відображення стану ---------- */
function pill(el, text, kind){ el.className = "pill " + (kind || ""); el.innerHTML = (kind ? '<span class="dot"></span>' : "") + esc(text); }
function jobText(j){ return !j ? "" : (j.state === "running" ? "⏳ " : j.state === "ok" ? "✅ " : "❌ ") + (j.message || ""); }
function showJob(id, j){ const el = $(id); if (!el) return; el.classList.toggle("hidden", !j); el.textContent = jobText(j); }

async function refresh(){
  let s; try { s = await api("/api/state"); } catch(e) { return; }
  S = s;
  pill($("#hBot"), s.running ? "Бот працює" : "Бот зупинено", s.running ? "ok" : "bad");
  pill($("#hMode"), s.dry_run ? "Тестовий режим" : "Бойовий режим", s.dry_run ? "warn" : "ok");
  pill($("#hPause"), "Розклад на паузі", "warn"); $("#hPause").classList.toggle("hidden", !s.paused);
  pill($("#hStop"), "⛔ Публікації зупинено", "bad"); $("#hStop").classList.toggle("hidden", !s.stopped);
  $("#bStopPub").textContent = s.stopped ? "✅ Відновити публікації" : "⛔ Стоп публікацій";
  $("#bStopPub").dataset.act = s.stopped ? "go_pub" : "stop_pub";
  $("#restartBanner").classList.toggle("hidden", !(s.restart_needed && s.running));
  $("#foreignBanner").classList.toggle("hidden", !s.foreign_bot);
  $("#foreignCmd").textContent = s.foreign_bot || "";
  $("#pathBanner").classList.toggle("hidden", !s.path_problems.length);
  $("#pathText").textContent = "⚠️ Неправильні папки — бот не запуститься: " + s.path_problems.join("; ");
  $("#tgBanner").classList.toggle("hidden", s.secrets.TELEGRAM_BOT_TOKEN.set && s.secrets.TELEGRAM_CHAT_ID.set);

  $("#botState").textContent = s.running ? "🟢 Працює" : "🔴 Зупинено";
  $("#bStart").disabled = s.running; $("#bStop").disabled = !s.running; $("#bRestart").disabled = !s.running;
  $("#pauseState").textContent = s.paused ? "⏸ На паузі" : "▶️ Активний";
  $("#bStreamOff").classList.toggle("hidden", !s.stream_until);
  $("#powerInfo").textContent = (s.stream_until ? "🎮 Режим стріму до " + s.stream_until + ". " : "") + (s.power.on ? "💤 Сон між слотами увімкнено" + (s.power.next_wake ? " · пробудження " + s.power.next_wake : "") +
    (s.power.state ? " · зараз: " + s.power.state : "") : "");
  $("#bPause").textContent = s.paused ? "▶️ Продовжити" : "⏸ Пауза"; $("#bPause").dataset.act = s.paused ? "resume" : "pause";
  $("#modeState").textContent = s.dry_run ? "🧪 Тестовий" : "🚀 Бойовий";
  $("#bMode").textContent = s.dry_run ? "🚀 Увімкнути бойовий" : "🧪 Повернути тестовий";
  $("#autoState").textContent = s.autostart === null ? "Лише у Windows" : (s.autostart ? "✅ Увімкнено" : "Вимкнено");
  $("#autoJob").textContent = jobText(s.jobs.autostart);
  showJob("#updJob", s.jobs.update);
  $("#updHome").textContent = s.update.newer ? "⬆️ Доступна версія " + s.update.remote : (s.update.error ? "⚠️ Оновлення: " + s.update.error : "");
  $("#bUpdHome").classList.toggle("primary", !!s.update.newer);
  $("#updLocal").textContent = $("#ver").textContent.replace("версія ", "");
  $("#updRemote").textContent = s.update.remote || "—";
  pill($("#updStatus"), s.update.error ? "помилка" : (s.update.newer ? "є оновлення " + s.update.remote : (s.update.remote ? "остання версія" : "ще не перевірено")),
       s.update.error ? "bad" : (s.update.newer ? "warn" : (s.update.remote ? "ok" : "")));
  if (s.update.error) { const e = $("#updJob"); if (e.classList.contains("hidden")) { e.classList.remove("hidden"); e.textContent = "❌ " + s.update.error; } }
  showJob("#checkJob", s.jobs.check); showJob("#tgJob", s.jobs.tg_test); showJob("#tgJob2", s.jobs.tg_test);
  $("#healthTime").textContent = s.health_time ? "Остання автоматична перевірка: " + s.health_time : "";

  const tok = s.tokens, sec = s.secrets;
  const conn = {youtube: tok.youtube.connected, tiktok: tok.tiktok.connected, instagram: !!(tok.meta.connected && sec.IG_USER_ID.set), facebook: !!(tok.meta.connected && sec.FB_PAGE_ID.set)};
  $("#platTable").innerHTML = Object.entries(s.platforms).map(([k, p]) => `<tr><td><b>${esc(p.name)}</b></td>
    <td>${p.enabled ? '<span class="pill ok">увімкнено</span>' : '<span class="pill">вимкнено</span>'}</td>
    <td>${conn[k] ? '<span class="pill ok">підключено</span>' : '<span class="pill bad">не підключено</span>'}</td>
    <td class="small muted">${esc(p.health || "")}</td>
    <td><button class="btn" data-plat="${k}" data-on="${p.enabled ? 0 : 1}">${p.enabled ? "🚫 Вимкнути" : "✅ Увімкнути"}</button></td></tr>`).join("");

  renderWork(s);
  const UK = {notified:"⏳ чекає підтвердження", approved:"👍 підтверджено", publishing:"⏫ публікується", planned:"📦 з черги"};
  $("#upcoming").innerHTML = s.upcoming.map(u => `<li><b>${esc(u.slot)}</b>${u.manual ? " (тестовий)" : ""} — ${u.item ? `#${u.item} ${esc(u.name)} <span class="muted small">${UK[u.kind] || esc(u.kind)}</span>` : (u.kind === "paused" ? "⏸ пауза" : '<span class="muted">порожньо — черга порожня</span>')}</li>`).join("") || '<li class="muted">—</li>';
  $("#qCount").textContent = s.queue.length;
  const ri = s.ready_info; $("#readyPath").textContent = ri.path;
  $("#readyInfo").innerHTML = !ri.exists ? '<span style="color:var(--bad)">папки не існує — оберіть іншу</span>' :
    `знайдено відео: <b>${ri.total}</b>, готових до публікації: <b>${ri.ready}</b>${ri.games.length ? " · ігри: " + ri.games.map(esc).join(", ") : ""}` +
    (ri.no_json ? `<br><span class="muted">з них без json: ${ri.no_json} — назву й хештеги програма створить сама${s.settings["auto_meta.enabled"] ? "" : " (зараз вимкнено!)"}</span>` : "");
  setHTML("#queue", s.queue_items.map(i => `<div class="qrow"><span class="n">#${i.id} ${i.game ? "[" + esc(i.game) + "] " : ""}${esc(i.title)}${i.when ? ` → <b>${esc(i.when)}</b>` : ""}${i.skips ? ` <span class="pill warn">пропусків: ${i.skips}</span>` : ""}</span>
      <button class="btn" data-edit="${i.id}" title="Переглянути відео, змінити назву, обрати час">🎬 Відкрити</button>
      <button class="btn" data-iop="first" data-id="${i.id}" title="Першим у черзі">⬆️</button>
      <button class="btn danger" data-iop="remove" data-id="${i.id}" title="Скасувати (файл — у папку «скасовані»)">🗑</button></div>`).join("") || '<p class="muted">черга порожня — покладіть шортси в папку ready</p>');
  $("#reqCard").classList.toggle("hidden", !s.pending_users.length);
  setHTML("#reqRows", s.pending_users.map(u => `<tr><td>${esc(u.name || "")}</td><td>${u.chat_id}</td><td><div class="row">
      <button class="btn" data-user="${u.chat_id}" data-role="viewer">👁 Сповіщення</button>
      <button class="btn" data-user="${u.chat_id}" data-role="admin">🛠 Адмін</button>
      <button class="btn danger" data-user="${u.chat_id}" data-role="blocked">❌ Відхилити</button></div></td></tr>`).join(""));
  if (s.jobs.report) { const j = s.jobs.report, o = $("#repOut"); o.classList.remove("hidden");
    const html = j.state === "ok" ? tgHtml(j.message) : esc(jobText(j)); if (o.dataset.h !== html) { o.dataset.h = html; o.innerHTML = html; } }
  const fl = s.failed || [];
  $("#failedCount").textContent = fl.filter(f => !f.ignored).length + (fl.some(f => f.ignored) ? " + 🙈 " + fl.filter(f => f.ignored).length : "");
  $("#failedRows").innerHTML = fl.map(f => `<tr><td>${f.ignored ? "🙈 " : ""}<b>#${f.id}</b> ${esc(f.name)}${f.exists ? "" : ' <span class="pill warn">файл не знайдено</span>'}
      <div class="small">${Object.entries(f.errors).map(([p, e]) => `❌ <b>${esc(p)}</b>: ${esc(e)}`).join("<br>")}</div>
      ${f.ok.length ? `<div class="small muted">✅ Вийшло: ${f.ok.map(esc).join(", ")}</div>` : ""}
      ${f.retry ? `<div class="small">🔁 Повтор: <b>${esc(f.retry)}</b></div>` : ""}</td>
    <td><div class="row">
      <button class="btn primary" data-fail="${f.id}" data-op="now">🔁 Зараз</button>
      <button class="btn" data-fail="${f.id}" data-op="slot">📦 У вільний слот</button>
      <button class="btn" data-det="${f.id}">🧾 Деталі</button>
      ${f.ignored ? `<button class="btn" data-fail="${f.id}" data-op="unignore">↩️ Повернути</button>`
                  : `<button class="btn" data-fail="${f.id}" data-op="ignore">✖️ Ігнорувати</button>`}
    </div></td></tr>`).join("") || '<tr><td class="muted">немає — усе опубліковано ✅</td></tr>';
  $("#posted").innerHTML = s.posted.map(i => `<tr><td>${esc(i.when)}</td><td>${esc(i.name)}</td><td>${
    Object.entries(i.links).map(([p, u]) => u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(s.platforms[p].name)}</a>` : esc(s.platforms[p].name)).join(" · ")}${
    Object.keys(i.errors).length ? ' <span class="pill bad">помилки: ' + Object.keys(i.errors).map(p => esc(s.platforms[p].name)).join(", ") + "</span>" : ""}${
    i.details ? ` <button class="btn" data-det="${i.id}" title="Повні причини помилок і що пробувала програма">🧾 Деталі</button>` : ""}</td></tr>`).join("") || '<tr><td class="muted">ще нічого</td></tr>';

  const roles = {admin:"🛠 Адмін", viewer:"👁 Глядач", pending:"⏳ Запит", blocked:"🚫 Відхилено"};
  $("#users").innerHTML = s.users.map(u => `<tr><td>${esc(u.name || "")}${u.owner ? ' <span class="pill">головний</span>' : ""}</td><td>${u.chat_id}</td><td>${roles[u.role] || u.role}</td><td><div class="row">${
    u.owner ? '<span class="muted small">змінюється в полі вище</span>' :
    (u.role !== "viewer" ? `<button class="btn" data-user="${u.chat_id}" data-role="viewer">👁 Глядач</button>` : "") +
    (u.role !== "admin" ? `<button class="btn" data-user="${u.chat_id}" data-role="admin">🛠 Адмін</button>` : "") +
    (u.role === "pending" ? `<button class="btn" data-user="${u.chat_id}" data-role="blocked">Відхилити</button>` : "") +
    `<button class="btn danger" data-user="${u.chat_id}" data-role="delete" title="Прибрати доступ">🗑</button>`}</div></td></tr>`).join("") || '<tr><td class="muted" colspan="4">нікого</td></tr>';

  pill($("#ytStatus"), tok.youtube.connected ? "підключено" : "не підключено", tok.youtube.connected ? "ok" : "bad");
  pill($("#ytSecret"), tok.youtube.client_secret ? "завантажено" : "немає", tok.youtube.client_secret ? "ok" : "warn");
  { const t = jobText(s.jobs.youtube_auth), m = t.match(/https:\/\/\S+/);   // посилання на вхід — клікабельне
    $("#ytJob").innerHTML = m ? esc(t.slice(0, m.index)) + `<a href="${esc(m[0])}" target="_blank" rel="noopener">відкрити вхід у Google ↗</a>`
                              : esc(t); }
  pill($("#metaStatus"), conn.facebook ? "підключено" : "не підключено", conn.facebook ? "ok" : "bad");
  $("#fbPage").textContent = sec.FB_PAGE_ID.value || "—"; $("#igUser").textContent = sec.IG_USER_ID.value || "—";
  const tt = tok.tiktok;
  pill($("#ttStatus"), tt.connected ? "підключено" + (tt.refresh_expires ? " до " + tt.refresh_expires : "") : "не підключено", tt.connected ? "ok" : "bad");
  $("#ttJob").textContent = jobText(s.jobs.tiktok_auth);
  showJob("#aiJob", s.jobs.ai_test);
  const aiOn = sec.GEMINI_API_KEY.set && s.settings["ai.enabled"];
  pill($("#aiStatus"), aiOn ? "увімкнено" : (sec.GEMINI_API_KEY.set ? "вимкнено" : "немає ключа"), aiOn ? "ok" : "warn");

  if ($("#logFollow").checked) { const box = $("#logBox"), atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    let lines = (s.log || "").split("\n");
    if ($("#logErr").checked) lines = lines.filter(l => /\[(WARNING|ERROR|CRITICAL)\]/.test(l));
    const q = $("#logFind").value.trim().toLowerCase(); if (q) lines = lines.filter(l => l.toLowerCase().includes(q));
    box.textContent = lines.join("\n") || (s.log ? "нічого не знайдено" : "журнал порожній"); if (atEnd) box.scrollTop = box.scrollHeight; }
  renderChannels(s);
  if (!formsFilled) fillForms(s);
}


/* ---------- додатковий канал (EN) ---------- */
function renderChannels(s){
  const c = s.channels || {list: []};
  $("#tabChan").classList.toggle("hidden", !!c.is_channel);
  $("#chanName").classList.toggle("hidden", !c.is_channel);
  if (c.is_channel) { $("#chanName").textContent = c.name; document.title = "poster — " + c.name; return; }
  const ch = c.list[0]; if (!ch) return;
  $("#chCreate").classList.toggle("hidden", ch.exists);
  $("#chMain").classList.toggle("hidden", !ch.exists);
  if (!ch.exists) { pill($("#chStatus"), "Не створено", "warn"); return; }
  if (ch.error) { pill($("#chStatus"), "Помилка: " + ch.error, "bad"); return; }
  pill($("#chStatus"), ch.running ? "Бот працює" : "Бот зупинено", ch.running ? "ok" : "bad");
  $("#chDir").textContent = ch.dir;
  $("#chBot").textContent = ch.running ? "🟢 Працює" : "🔴 Зупинено";
  $("#chTg").textContent = ch.telegram_ready ? "✅ Бот і chat_id задано" : "⚠️ Немає свого бота — панель каналу → «✈️ Telegram і доступ»";
  $("#chYt").textContent = ch.youtube_authorized ? "✅ Авторизовано" : "⚠️ Не авторизовано — панель каналу → «▶️ YouTube»";
  $("#chPlat").textContent = (ch.platforms || []).join(", ") || "жодної";
  $("#chSlots").textContent = (ch.slots || []).join(", ") + " (" + (ch.timezone || "") + ")";
  $("#chReadyNow").textContent = ch.ready;
  $("#chFooter").textContent = ch.footer || "—";
  $("#chStart").disabled = ch.running; $("#chStop").disabled = !ch.running;
  if (document.activeElement !== $("#chAuto")) $("#chAuto").checked = !!ch.autostart;
}
document.addEventListener("click", ev => {
  const b = ev.target.closest("button[data-ch]"); if (!b) return;
  const op = b.dataset.ch, body = {name: "channel", key: "en", op};
  if (op === "create") body.ready = $("#chReady").value.trim();
  if (op === "stop" && !confirm("Зупинити бота EN-каналу?")) return;
  run(() => api("/api/action", body), b, op === "start" ? "Запускаю бота каналу…" : op === "panel" ? "Відкриваю панель каналу…" : null)
    .then(j => { if (op === "panel" && j && j.message) { const u = j.message.split(" ").pop(); window.open(u, "_blank"); } });
});
$("#chAuto").addEventListener("change", ev => run(() => api("/api/action", {name: "channel", key: "en", op: "autostart", on: ev.target.checked})));

function fillForms(s){
  formsFilled = true;
  $$("[data-set]").forEach(el => { const v = s.settings[el.dataset.set];
    if (el.type === "checkbox") el.checked = !!v; else el.value = v ?? ""; });
  const slots = s.settings.slots || [];
  $("#slotInputs").innerHTML = Array.from({length:6}, (_, i) => `<input type="time" value="${esc(slots[i] || "")}">`).join("");
  $$("[data-secret]").forEach(el => { const v = s.secrets[el.dataset.secret]; el.value = "";
    el.placeholder = v && v.set ? (el.type === "password" ? "збережено: " + v.value : "") : "не задано";
    if (el.type !== "password" && v && v.set) el.value = v.value; });
}

/* ---------- шортси: картки, черга, редактор ---------- */
const ST = {notified:"⏳ Чекає підтвердження", approved:"👍 Підтверджено", publishing:"⏫ Публікується…"};
const SHORTN = {youtube:"YouTube", instagram:"Instagram", facebook:"Facebook", tiktok:"TikTok"};
function setHTML(sel, html){ const el = $(sel); if (el.dataset.h !== html) { el.dataset.h = html; el.innerHTML = html; } }
function tgHtml(t){  // лише безпечні теги Telegram: b, i, u, s, code, pre, a href=http(s)
  let h = esc(t);
  h = h.replace(/&lt;(\/?)(b|i|u|s|code|pre)&gt;/g, "<$1$2>");
  h = h.replace(/&lt;a href=&quot;(https?:\/\/[^"&]+?)&quot;&gt;/g, '<a href="$1" target="_blank" rel="noopener">').replace(/&lt;\/a&gt;/g, "</a>");
  return h;
}
function renderWork(s){
  $("#workCount").textContent = s.work.length;
  const waiting = s.work.filter(i => i.status === "notified").length;
  $("#pubAllRow").classList.toggle("hidden", waiting < 2);
  $("#bPubAll").textContent = `🚀 Опублікувати всі, що чекають (${waiting})`;
  $("#cancelBox").classList.toggle("hidden", !s.cancelled.length);
  setHTML("#cancelRows", s.cancelled.map(c => `<div class="qrow"><span class="n">#${c.id} ${esc(c.name)}${c.exists ? "" : ' <span class="pill warn">файл не знайдено</span>'}</span>
    ${c.exists ? `<button class="btn" data-iop="restore" data-id="${c.id}">↩️ Повернути в чергу</button>` : ""}</div>`).join(""));
  const plats = Object.entries(s.platforms).filter(([k, p]) => p.enabled);
  setHTML("#work", s.work.map(i => `<div class="short">
    ${i.exists ? `<video src="/api/video?id=${i.id}" controls preload="metadata" playsinline></video>` : '<div class="pill warn">файл не знайдено</div>'}
    <div class="info"><div class="t">#${i.id} ${esc(i.title)}</div>
      <div class="small muted">${esc(i.tags)} ${esc(i.always)}</div>
      <div style="margin-top:6px"><b>${ST[i.status] || esc(i.status)}</b> · 🕐 ${esc(i.when || "—")}${i.edited.length ? ` <span class="pill">змінено: ${esc(i.edited.join(", "))}</span>` : ""}</div>
      <div class="chips">${plats.map(([k]) => `<button class="chip ${i.disabled.includes(k) ? "off" : ""}" data-iop="toggle" data-id="${i.id}" data-platform="${k}" title="Увімкнути/вимкнути для цього шортса" ${i.status === "publishing" ? "disabled" : ""}>${SHORTN[k] || k}</button>`).join("")}</div>
      ${i.status === "publishing" ? "" : `<div class="row">
        <button class="btn primary" data-edit="${i.id}">${i.status === "notified" ? "🚀 Опублікувати…" : "✏️ Назва, хештеги, час"}</button>
        <button class="btn" data-iop="publish" data-when="now" data-id="${i.id}">⚡ Зараз</button>
        ${i.status === "approved" ? `<button class="btn" data-iop="unapprove" data-id="${i.id}">↩️ Скасувати підтвердження</button>` : ""}
        <button class="btn danger" data-iop="cancel" data-id="${i.id}" title="Файл — у папку «скасовані», наступний шортс стає на цей слот">🚫 Скасувати шортс</button>
        ${s.can_regen ? `<button class="btn" data-iop="regen" data-id="${i.id}">🔄 Перегенерувати тексти</button>` : ""}</div>`}
    </div></div>`).join("") || '<p class="muted">Зараз нічого не чекає. Шортс з’явиться тут за годину до свого слоту — або відкрийте будь-яке відео з черги нижче й опублікуйте коли завгодно.</p>');
}
let EDIT = null;
function openEditor(id){
  const it = [...S.work, ...S.queue_items].find(x => x.id === id); if (!it) return toast("Шортса вже немає", "err");
  EDIT = it;
  $("#imTitle").textContent = `🎬 Шортс #${it.id}` + (it.status === "queued" ? " (у черзі)" : "");
  $("#imVideo").src = `/api/video?id=${it.id}`;
  $("#imName").value = it.title; $("#imTags").value = it.tags;
  $("#imAlways").textContent = it.always ? "У кінець назви й підписів автоматично додаються: " + it.always : "";
  const opts = [];
  if (it.ts && it.ts * 1000 > Date.now() + 60000) opts.push(`<option value="keep">✅ Залишити: ${esc(it.when)}</option>`);
  opts.push(`<option value="now">⚡ Зараз</option>`);
  S.free_slots.filter(f => f.ts !== it.ts).forEach(f => opts.push(`<option value="${f.ts}">📦 ${esc(f.label)}</option>`));
  opts.push(`<option value="custom">⌨️ Свій час…</option>`);
  $("#imWhen").innerHTML = opts.join(""); $("#imCustom").classList.add("hidden"); $("#imCustom").value = "";
  const mn = new Date(Date.now() + 3 * 60000); mn.setSeconds(0, 0);
  $("#imCustom").min = new Date(mn.getTime() - mn.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  $("#imNote").textContent = it.status === "queued"
    ? "«Зберегти» з обраним часом — шортс стане в розклад і чекатиме підтвердження (картка прийде в Telegram). «Підтвердити» — опублікується в обраний час без додаткових питань."
    : "«Зберегти» — лише змінити тексти/час. «Підтвердити» — шортс опублікується в обраний час.";
  $("#itemModal").classList.remove("hidden");
}
function closeEditor(){ $("#itemModal").classList.add("hidden"); $("#imVideo").pause(); $("#imVideo").removeAttribute("src"); $("#imVideo").load(); EDIT = null; }
$("#imWhen").addEventListener("change", () => $("#imCustom").classList.toggle("hidden", $("#imWhen").value !== "custom"));
$("#imCancel").addEventListener("click", closeEditor);
$("#detClose").addEventListener("click", () => $("#detModal").classList.add("hidden"));
$("#detModal").addEventListener("click", ev => { if (ev.target.id === "detModal") $("#detModal").classList.add("hidden"); });
["#logErr", "#logFind"].forEach(sel => $(sel).addEventListener("input", () => S && refresh()));
$("#itemModal").addEventListener("click", ev => { if (ev.target.id === "itemModal") closeEditor(); });
async function submitEditor(op, btn){
  if (!EDIT) return;
  let when = $("#imWhen").value;
  if (when === "custom") { const v = $("#imCustom").value; if (!v) return toast("Оберіть дату й час", "err");
    when = Math.floor(new Date(v).getTime() / 1000);
    if (when * 1000 <= Date.now() + 60000) return toast("Цей час уже минув — оберіть пізніший", "err"); }
  if (op === "save" && when === "now") return toast("«Зараз» — це публікація: натисніть «Підтвердити»", "err");
  const body = {name:"item", op, id: EDIT.id, title: $("#imName").value, tags: $("#imTags").value, when};
  const j = await run(() => api("/api/action", body), btn, op === "publish" ? "Передаю боту…" : null);
  if (j) closeEditor();
}
$("#imSave").addEventListener("click", ev => submitEditor("save", ev.target));
$("#imPub").addEventListener("click", ev => submitEditor("publish", ev.target));

/* ---------- кнопки ---------- */
document.addEventListener("click", ev => {
  const b = ev.target.closest("button"); if (!b) return;
  if (b.dataset.act) {
    const name = b.dataset.act, body = {name};
    if (name === "testslot") body.minutes = +$("#testMin").value || 10;
    if (name === "stream") body.hours = +b.dataset.hours;
    if (name === "publish_all" && !confirm("Опублікувати всі шортси, що чекають на підтвердження, по черзі зараз?")) return;
    if (name === "stop" && !confirm("Зупинити бота? Сповіщення і публікації не працюватимуть, доки не запустите знову.")) return;
    if (name === "testslot" && body.minutes < 7) { body.minutes = 7; $("#testMin").value = 7; toast("Найменше — 7 хв: бот має встигнути надіслати шортс і дочекатися відповіді"); }
    if (SLOW[name]) $$(`button[data-act="${name}"]`).forEach(x => { if (x !== b) x.disabled = true; });
    run(() => api("/api/action", body), b, SLOW[name]).then(() => $$(`button[data-act="${name}"]`).forEach(x => x.disabled = false));
  } else if (b.dataset.open) {
    run(() => api("/api/action", {name:"open", what:b.dataset.open}), b);
  } else if (b.dataset.user) {
    if (b.dataset.role === "delete" && !confirm("Прибрати доступ?")) return;
    run(() => api("/api/users", {chat_id:+b.dataset.user, role:b.dataset.role}), b);
  } else if (b.dataset.adduser) {
    const id = $("#newUser").value.trim(); if (!/^-?\d+$/.test(id)) return toast("chat_id — це число", "err");
    run(() => api("/api/users", {chat_id:+id, role:b.dataset.adduser}), b).then(() => $("#newUser").value = "");
  } else if (b.hasAttribute("data-toggle-pw")) {
    ev.preventDefault(); const i = b.previousElementSibling; i.type = i.type === "password" ? "text" : "password";
  } else if (b.dataset.edit) {
    openEditor(+b.dataset.edit);
  } else if (b.dataset.iop) {
    const op = b.dataset.iop, body = {name:"item", op, id:+b.dataset.id};
    if ((op === "remove" || op === "cancel") && !confirm("Скасувати шортс #" + body.id + "? Файл буде перенесено в папку «скасовані», черга зсунеться. Повернути можна кнопкою «↩️ Повернути».")) return;
    if (op === "publish") { if (!confirm("Опублікувати шортс #" + body.id + " зараз?")) return; body.when = "now"; }
    if (b.dataset.platform) body.platform = b.dataset.platform;
    run(() => api("/api/action", body), b, op === "regen" ? "Перегенеровую…" : null);
  } else if (b.dataset.det) {
    api("/api/action", {name:"details", id:+b.dataset.det}).then(j => {
      $("#detBody").innerHTML = tgHtml(j.message || ""); $("#detModal").classList.remove("hidden");
    }).catch(e => toast(e.message, "err"));
  } else if (b.dataset.period) {
    $("#repOut").classList.remove("hidden"); $("#repOut").dataset.h = "";
    run(() => api("/api/action", {name:"analytics", days:+b.dataset.period}), b);
  } else if (b.dataset.report) {
    $("#repOut").classList.remove("hidden"); $("#repOut").dataset.h = ""; $("#repOut").textContent = "⏳ Збираю дані…";
    run(() => api("/api/action", {name:"report", kind:b.dataset.report, send:$("#repSend").checked}), b);
  } else if (b.dataset.plat) {
    run(() => api("/api/action", {name:"platform", key:b.dataset.plat, on: b.dataset.on === "1"}), b);
  } else if (b.dataset.fail) {
    run(() => api("/api/action", {name: "failed", id: +b.dataset.fail, op: b.dataset.op}), b);
  } else if (b.dataset.pick) {
    pickFolder(b.dataset.pick, b);
  } else if (b.hasAttribute("data-save-settings")) {
    saveSettings(b);
  } else if (b.dataset.saveSecrets) {
    saveSecrets(b, b.closest(".card"));
  }
});
$("#bMode").addEventListener("click", ev => {
  const live = S && S.dry_run;
  if (live && !confirm("Увімкнути БОЙОВИЙ режим? Підтверджені шортси публікуватимуться по-справжньому.\nПереконайтесь, що всі потрібні платформи підключено.")) return;
  run(() => api("/api/action", {name:"mode", mode: live ? "live" : "test"}), ev.target);
});

function collectSettings(){
  const out = {};
  $$("[data-set]").forEach(el => { out[el.dataset.set] = el.type === "checkbox" ? el.checked : (el.type === "number" ? +el.value : el.value); });
  out.slots = $$("#slotInputs input").map(i => i.value).filter(Boolean);
  return out;
}
function saveSettings(btn){ run(() => api("/api/settings", collectSettings()), btn).then(() => formsFilled = false); }
function saveSecrets(btn, scope){
  const out = {}; $$("[data-secret]", scope).forEach(el => { if (el.value.trim()) out[el.dataset.secret] = el.value.trim(); });
  if (!Object.keys(out).length) return toast("Немає змін", "");
  return run(() => api("/api/secrets", out), btn).then(j => { if (j) formsFilled = false; return j; });
}

$("#goTg").addEventListener("click", () => $('#tabs button[data-tab="tg"]').click());
$("#goSched").addEventListener("click", () => $('#tabs button[data-tab="sched"]').click());

/* ---------- YouTube ---------- */
$("#ytUpload").addEventListener("click", async ev => {
  const f = $("#ytFile").files[0]; if (!f) return toast("Оберіть файл client_secret….json", "err");
  const content = await f.text(); run(() => api("/api/youtube/secret", {content}), ev.target);
});
$("#ytAuth").addEventListener("click", ev => run(() => api("/api/auth/youtube", {}), ev.target));

/* ---------- Meta ---------- */
$("#metaGet").addEventListener("click", ev => run(async () => {
  const j = await api("/api/auth/meta", {token: $("#metaToken").value.trim()});
  $("#metaList").innerHTML = j.pages.map((p, i) => `<label class="check"><input type="radio" name="mp" value="${p.index}" ${i ? "" : "checked"}>
    ${esc(p.name)} <span class="muted small">(id ${esc(p.id)}) — Instagram: ${p.instagram ? "@" + esc(p.instagram) : "не прив'язано"}</span></label>`).join("");
  $("#metaPages").classList.remove("hidden"); $("#metaToken").value = "";
  return {message: "Знайдено сторінок: " + j.pages.length};
}, ev.target));
$("#metaSelect").addEventListener("click", ev => run(async () => {
  const sel = $('input[name="mp"]:checked'); const j = await api("/api/auth/meta/select", {index: +sel.value});
  $("#metaPages").classList.add("hidden"); return j;
}, ev.target));

/* ---------- TikTok ---------- */
$("#ttSave").addEventListener("click", async ev => {
  const card = ev.target.closest(".card");
  await run(() => api("/api/settings", {"platforms.tiktok.redirect_uri": $('[data-set="platforms.tiktok.redirect_uri"]').value}), null);
  saveSecrets(ev.target, card);
});
$("#ttAuth").addEventListener("click", ev => run(async () => {
  const w = window.open("about:blank", "_blank");
  const j = await api("/api/auth/tiktok", {});
  if (w) w.location = j.url; else window.open(j.url, "_blank");
  $("#ttManual").classList.toggle("hidden", j.local);
  return {message: j.local ? "Увійдіть у TikTok у новій вкладці — панель сама отримає токен" : "Увійдіть у TikTok, потім вставте адресу нижче"};
}, ev.target));
$("#ttFinish").addEventListener("click", ev => run(async () => {
  const j = await api("/api/auth/tiktok/finish", {url: $("#ttUrl").value.trim()}); $("#ttManual").classList.add("hidden"); return j;
}, ev.target));

/* ---------- безпека ---------- */
$("#chPw").addEventListener("click", ev => run(() => api("/api/password", {old: $("#oldPw").value, new: $("#newPw").value}), ev.target)
  .then(j => { if (j) $("#oldPw").value = $("#newPw").value = ""; }));
$("#logout").addEventListener("click", async () => { await api("/api/logout", {}); showAuth(true); });
$("#shutdown").addEventListener("click", async () => {
  if (!confirm("Закрити панель? Бот продовжить працювати.")) return;
  await api("/api/shutdown", {}); clearInterval(timer);
  document.body.innerHTML = '<div class="center"><div class="card login"><div class="big">Панель закрито</div><p class="muted">Бот продовжує працювати. Щоб відкрити панель знову — запустіть panel.bat.</p></div></div>';
});

/* ---------- вибір папки ---------- */
const PICK_NAMES = {"paths.ready": "папку з готовими шортсами", "paths.posted": "папку для опублікованих",
  "paths.skipped": "папку для пропущених", "paths.cancelled": "папку для скасованих", "regenerate_cwd": "робочу папку команди",
  "highlights.folder": "папку з хайлайтами"};
let fsTarget = null, fsCur = "", fsSep = "/";
async function pickFolder(key, btn){
  const input = $(`[data-set="${key}"]`), initial = (input && input.value) || (S && S.settings[key]) || "";
  if (btn) btn.disabled = true;
  try {
    if (S && S.windows) {
      toast("Відкриваю вікно вибору папки… (якщо не видно — воно може бути за браузером)");
      const j = await api("/api/pick_folder", {initial});
      if (j.native) { if (j.path) await applyFolder(key, j.path); return; }
    }
    await openFs(key, initial);
  } catch(e){ toast(e.message, "err"); }
  finally { if (btn) btn.disabled = false; }
}
async function applyFolder(key, path){
  await api("/api/settings", {[key]: path});
  $$(`[data-set="${key}"]`).forEach(i => i.value = path);
  let msg = "Збережено: " + path;
  if (key === "paths.ready") { const f = await api("/api/fs", {path}); msg += `\nЗнайдено шортсів: ${f.shorts.total}, готових: ${f.shorts.ready}`; }
  if (S && S.running) msg += "\nБот підхопить нову папку протягом хвилини.";
  toast(msg, "okk"); await refresh();
}
async function openFs(key, path){
  fsTarget = key; $("#fsTitle").textContent = "📂 Оберіть " + (PICK_NAMES[key] || "папку");
  $("#fsModal").classList.remove("hidden"); await loadFs(path);
}
function joinPath(base, name){
  const sep = base.includes("\\") ? "\\" : "/";
  return base.endsWith(sep) ? base + name : base + sep + name;
}
async function loadFs(path){
  try {
    const j = await api("/api/fs", {path}); fsCur = j.path;
    $("#fsPath").textContent = j.path;
    $("#fsDrives").innerHTML = j.drives.map(d => `<button class="btn" data-fs="${esc(d)}">${esc(d)}</button>`).join("");
    $("#fsHome").dataset.fs = j.home;
    $("#fsList").innerHTML = (j.parent ? `<button data-fs="${esc(j.parent)}">⬆️ .. (вгору)</button>` : "") +
      j.dirs.map(d => `<button data-fs="${esc(joinPath(j.path, d))}">📁 ${esc(d)}</button>`).join("") +
      (j.dirs.length ? "" : '<div class="muted small" style="padding:10px">підпапок немає</div>');
    const sh = j.shorts;
    $("#fsInfo").innerHTML = fsTarget !== "paths.ready" ? "" : (sh.total
      ? `✅ Тут відео: <b>${sh.total}</b>, готових: <b>${sh.ready}</b>${sh.games.length ? " · ігри: " + sh.games.map(esc).join(", ") : ""}`
      : '<span class="muted">У цій папці (і її підпапках-іграх) відео mp4 не знайдено.</span>');
  } catch(e){ toast(e.message, "err"); }
}
$("#fsModal").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-fs]"); if (b) { loadFs(b.dataset.fs); return; }
  if (ev.target.id === "fsModal") $("#fsModal").classList.add("hidden");
});
$("#fsCancel").addEventListener("click", () => $("#fsModal").classList.add("hidden"));
$("#fsOk").addEventListener("click", async () => { $("#fsModal").classList.add("hidden");
  try { await applyFolder(fsTarget, fsCur); } catch(e){ toast(e.message, "err"); } });
$("#fsMk").addEventListener("click", async () => { const name = $("#fsNew").value.trim(); if (!name) return;
  try { const j = await api("/api/fs/mkdir", {path: fsCur, name}); $("#fsNew").value = ""; await loadFs(j.path); } catch(e){ toast(e.message, "err"); } });
document.addEventListener("keydown", ev => { if (ev.key === "Escape") $("#fsModal").classList.add("hidden"); });

boot();

/* ---------- історія версій ---------- */
async function loadVersions(btn){
  const j = await run(() => api("/api/versions"), btn);
  if (!j) return;
  const list = j.versions.filter(v => v.version), cur = list.findIndex(v => v.current);
  const rows = list.map((v, i) => {
    const tag = v.current ? '<span class="pill ok">встановлена</span>' : (v.local ? '<span class="pill">💾 на ПК</span>' : '<span class="pill">☁️ GitHub</span>');
    const go = v.current ? "" : `<button class="btn" data-ver="${esc(v.version)}">${i < cur ? "⬆️ Перейти" : "↩️ Відкотити"}</button>`;
    const notes = v.notes.length ? "<ul class='small'>" + v.notes.slice(0, 6).map(n => "<li>" + esc(n) + "</li>").join("") + "</ul>" : "<p class='small'>—</p>";
    return `<div class="card" style="margin:8px 0"><div class="row"><b>${esc(v.version)}</b> ${tag} ${go}</div>${notes}</div>`;
  });
  const err = j.versions.find(v => v.error);
  $("#verList").innerHTML = (j.pinned ? `<p class="small">⏸ Автооновлення на паузі (встановлено вручну ${esc(j.pinned)}).</p>` : "")
    + rows.join("") + (err ? `<p class="small">⚠️ GitHub: ${esc(err.error)} — показано лише збережені на ПК.</p>` : "");
}
$("#bVersions").addEventListener("click", ev => loadVersions(ev.target));
$("#verList").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-ver]"); if (!b) return;
  const v = b.dataset.ver;
  if (!confirm("Перейти на версію " + v + "? Поточна збережеться, повернутись можна будь-коли.")) return;
  run(() => api("/api/action", {name: "switch_version", version: v}), b, "Встановлюю " + v + "…");
});
