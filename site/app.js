(() => {
  const PAGE = 8;            // Meldungen pro Kachel vor "Mehr anzeigen"
  const LAST_VISIT_KEY = "news:lastVisit";
  const TAB_KEY = "news:tab";
  const SAVED_KEY = "news:saved";
  const LANG_KEY = "news:deutsch";
  const HOT = 8;             // ab dieser KI-Relevanz gilt eine Meldung als wichtig
  const TOPIC_META = {
    ausschreibungen: { icon: "🪖", color: "var(--t-ausschreibungen)" },
    konkurrenz: { icon: "🏢", color: "var(--t-konkurrenz)" },
    branche: { icon: "🛡️", color: "var(--t-branche)" },
    technologie: { icon: "🔬", color: "var(--t-technologie)" },
    weltpolitik: { icon: "🌍", color: "var(--t-weltpolitik)" },
  };
  const meta = (id) => TOPIC_META[id] || { icon: "📰", color: "var(--accent)" };

  // ISO-3 → ISO-2 für Flaggen (EU, EWR, NATO, UK, CH)
  const ISO3 = { AUT: "AT", BEL: "BE", BGR: "BG", HRV: "HR", CYP: "CY", CZE: "CZ", DNK: "DK", EST: "EE", FIN: "FI", FRA: "FR",
    DEU: "DE", GRC: "GR", HUN: "HU", IRL: "IE", ITA: "IT", LVA: "LV", LTU: "LT", LUX: "LU", MLT: "MT", NLD: "NL", POL: "PL",
    PRT: "PT", ROU: "RO", SVK: "SK", SVN: "SI", ESP: "ES", SWE: "SE", NOR: "NO", ISL: "IS", LIE: "LI", CHE: "CH", GBR: "GB",
    USA: "US", CAN: "CA", TUR: "TR", ALB: "AL", MNE: "ME", MKD: "MK", UKR: "UA" };
  const flag = (c) => {
    const iso2 = ISO3[c] || (c && c.length === 2 ? c : null);
    return iso2 ? String.fromCodePoint(...[...iso2.toUpperCase()].map((ch) => 0x1f1a5 + ch.charCodeAt(0))) : "🇪🇺";
  };

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* egal */ } },
  };

  const $ = (sel) => document.querySelector(sel);
  const grid = $("#grid"), tabs = $("#tabs"), search = $("#search"), kpis = $("#kpis"), onlyNew = $("#only-new"), deutsch = $("#deutsch"), briefing = $("#briefing"), since = $("#since");
  const lastVisit = store.get(LAST_VISIT_KEY);
  let data = null;
  // Reiter aus dem Link (#ausschreibungen) hat Vorrang, damit man Ansichten teilen kann
  let activeTab = decodeURIComponent(location.hash.slice(1)) || store.get(TAB_KEY) || "alle";
  let fairs = [];
  const expanded = new Set();
  let tenderCountry = "";
  let saved = {};
  try { saved = JSON.parse(store.get(SAVED_KEY) || "{}") || {}; } catch { saved = {}; }
  const byLink = new Map();

  const rtf = new Intl.RelativeTimeFormat("de", { numeric: "auto" });
  function relTime(iso) {
    if (!iso) return "";
    const diff = (new Date(iso) - Date.now()) / 1000;
    const steps = [[60, "second", 1], [3600, "minute", 60], [86400, "hour", 3600], [604800, "day", 86400], [2629800, "week", 604800], [Infinity, "month", 2629800]];
    for (const [limit, unit, div] of steps) {
      if (Math.abs(diff) < limit) return rtf.format(Math.round(diff / div), unit);
    }
  }
  const fmtDate = (iso) => new Date(iso).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
  const startOfDay = (d) => { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; };

  function dayLabel(iso) {
    if (!iso) return "Ohne Datum";
    const days = Math.round((startOfDay(Date.now()) - startOfDay(iso)) / 86400000);
    if (days <= 0) return "Heute";
    if (days === 1) return "Gestern";
    if (days < 7) return new Date(iso).toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" });
    return "Älter";
  }

  function dueInfo(deadline) {
    if (!deadline) return null;
    const days = Math.ceil((new Date(deadline) - Date.now()) / 86400000);
    if (days < 0) return { cls: "over", text: `abgelaufen ${fmtDate(deadline)}`, expired: true };
    const text = days === 0 ? "Frist heute" : days === 1 ? "noch 1 Tag" : `noch ${days} Tage`;
    return { cls: days <= 7 ? "soon" : "", text, title: `Frist ${fmtDate(deadline)}` };
  }

  const isNew = (it) => lastVisit && it.published && it.published > lastVisit;

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function highlight(text, q) {
    const safe = escapeHtml(text);
    if (!q) return safe;
    const rx = new RegExp(`(${escapeHtml(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
    return safe.replace(rx, "<mark>$1</mark>");
  }
  const favicon = (domain) => domain
    ? `<img class="fav" src="https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=32" alt="" loading="lazy" onerror="this.remove()">`
    : "";
  const img = (src, cls) => src
    ? `<div class="${cls}"><img src="${escapeHtml(src)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentElement.remove()"></div>`
    : "";

  function filterItems(items) {
    const q = search.value.trim().toLowerCase();
    return items.filter((it) =>
      (!onlyNew.checked || isNew(it)) &&
      (!q || `${it.title} ${it.ai?.title_de || ""} ${it.summary} ${it.ai?.summary || ""} ${it.source} ${it.buyer || ""}`.toLowerCase().includes(q)));
  }
  const filtered = (topic) => filterItems(topic.items);

  const german = () => deutsch.checked;
  const titleOf = (it) => (german() && it.ai?.title_de) || it.title;
  const topicShort = (id) => data.topics.find((t) => t.id === id)?.short || id;
  const summaryOf = (it) => (german() && it.ai?.summary) || it.summary;
  const hotBadge = (it) => it.ai?.score >= HOT
    ? `<span class="hot" title="KI-Relevanz ${it.ai.score}/10${it.ai.why ? `: ${escapeHtml(it.ai.why)}` : ""}">🔥 wichtig</span>` : "";
  function starBtn(it) {
    const on = !!saved[it.link];
    return `<button type="button" class="star${on ? " on" : ""}" data-save="${escapeHtml(it.link)}" aria-pressed="${on}" title="${on ? "Von der Merkliste nehmen" : "Merken"}">${on ? "★" : "☆"}</button>`;
  }

  // ------------------------------------------------------------ Reiter

  function renderTabs() {
    const btn = (id, label, badge, color) =>
      `<button type="button" data-tab="${id}" aria-pressed="${activeTab === id}" style="--tc:${color}">${label}${badge ? `<span class="badge">${badge}</span>` : ""}</button>`;
    const newBadge = (n) => n ? `${n} neu` : "";
    const totalNew = data.topics.reduce((n, t) => n + t.items.filter(isNew).length, 0);
    const nSaved = Object.keys(saved).length;
    tabs.innerHTML = btn("alle", "Überblick", newBadge(totalNew), "var(--text)") +
      data.topics.map((t) => btn(t.id, `${meta(t.id).icon} ${escapeHtml(t.short || t.name)}`, newBadge(t.items.filter(isNew).length), meta(t.id).color)).join("") +
      btn("termine", "📅 Messen", "", "var(--t-konkurrenz)") +
      btn("gemerkt", "⭐ Merkliste", nSaved ? String(nSaved) : "", "var(--star)");
  }

  // ------------------------------------------------------------ Briefing

  function tenderRow(it, q = "") {
    const due = dueInfo(it.deadline);
    return `<li class="tender ${due?.expired ? "expired" : ""}">
      <span class="flag" title="${escapeHtml(it.country || "")}">${flag(it.country)}</span>
      <div class="item-main">
        <a class="item-title" href="${escapeHtml(it.link)}" target="_blank" rel="noopener">${highlight(titleOf(it), q)}</a>
        <div class="item-meta">
          ${isNew(it) ? `<span class="new-dot">● neu</span>` : ""}
          ${hotBadge(it)}
          ${it.buyer ? `<span>${highlight(it.buyer, q)}</span>` : `<span class="src">${favicon(it.domain)}${escapeHtml(it.source)}</span>`}
          ${it.published ? `<span>veröffentlicht ${relTime(it.published)}</span>` : ""}
          ${starBtn(it)}
        </div>
        ${it.ai?.facts ? `<p class="facts">${escapeHtml(it.ai.facts)}</p>` : ""}
      </div>
      ${due ? `<span class="due ${due.cls}" title="${escapeHtml(due.title || "")}">${due.text}</span>` : ""}
    </li>`;
  }

  function renderSince() {
    const show = lastVisit && activeTab === "alle" && !search.value.trim() && !onlyNew.checked;
    since.hidden = !show;
    if (!show) return;
    const parts = data.topics.map((t) => ({ t, n: t.items.filter(isNew).length })).filter((p) => p.n);
    const when = relTime(lastVisit);
    since.innerHTML = parts.length
      ? `<span class="since-label">Neu seit deinem letzten Besuch (${when}):</span>${parts.map(({ t, n }) =>
          `<button type="button" data-since="${t.id}" style="--tc:${meta(t.id).color}"><b>${n}</b> ${meta(t.id).icon} ${escapeHtml(t.short || t.name)}</button>`).join("")}`
      : `<span class="since-label">Seit deinem letzten Besuch (${when}) gibt es nichts Neues.</span>`;
  }

  const daysUntil = (iso) => Math.ceil((startOfDay(iso) - startOfDay(Date.now())) / 86400000);
  const upcomingFairs = () => fairs.filter((f) => new Date(f.end || f.start) >= startOfDay(Date.now()))
    .sort((a, b) => a.start.localeCompare(b.start));

  function renderKpis() {
    const show = activeTab === "alle" && !search.value.trim() && !onlyNew.checked;
    kpis.hidden = !show;
    if (!show) return;
    const now = Date.now(), week = now - 7 * 86400000;
    const tenders = data.topics.find((t) => t.id === "ausschreibungen")?.items || [];
    const open = tenders.filter((i) => i.deadline && new Date(i.deadline) > now);
    const soon = open.filter((i) => new Date(i.deadline) - now <= 14 * 86400000);
    const newTenders = tenders.filter((i) => i.published && new Date(i.published) > now - 30 * 86400000);
    const all = data.topics.flatMap((t) => t.items);
    const lastWeek = all.filter((i) => i.published && new Date(i.published) > week);
    const papers = all.filter((i) => i.kind === "paper" && i.published && new Date(i.published) > now - 30 * 86400000);
    const fair = upcomingFairs()[0];
    const card = (tab, value, label, sub, color) => `<button type="button" class="kpi" data-kpi="${tab}" style="--tc:${color}">
        <span class="kpi-value">${value}</span><span class="kpi-label">${label}</span>${sub ? `<span class="kpi-sub">${sub}</span>` : ""}</button>`;
    kpis.innerHTML =
      card("ausschreibungen", open.length, "offene Ausschreibungen",
        soon.length ? `${soon.length} mit Frist in 14 Tagen` : "keine Frist in 14 Tagen", "var(--t-ausschreibungen)") +
      card("ausschreibungen", newTenders.length, "neue Ausschreibungen", "letzte 30 Tage", "var(--t-ausschreibungen)") +
      card("alle", lastWeek.length, "Meldungen", "letzte 7 Tage", "var(--new)") +
      card("technologie", papers.length, "Fachartikel", "letzte 30 Tage", "var(--t-technologie)") +
      (fair ? card("termine", daysUntil(fair.start) <= 0 ? "läuft" : `${daysUntil(fair.start)} T.`, escapeHtml(fair.name),
        `${fmtDate(fair.start)} · ${escapeHtml(fair.city)}`, "var(--t-konkurrenz)") : "");
  }

  function fairList(limit) {
    const list = upcomingFairs().slice(0, limit);
    if (!list.length) return `<p class="empty">Keine Termine hinterlegt.</p>`;
    const fmt = (iso) => new Date(iso).toLocaleDateString("de-DE", { day: "numeric", month: "short" });
    return `<ol class="fairs">${list.map((f) => {
      const d = daysUntil(f.start);
      const when = d <= 0 ? "läuft gerade" : d === 1 ? "morgen" : d < 60 ? `in ${d} Tagen` : `in ${Math.round(d / 30)} Monaten`;
      return `<li>
        <div class="fair-date"><b>${new Date(f.start).getDate()}</b><span>${new Date(f.start).toLocaleDateString("de-DE", { month: "short", year: "2-digit" })}</span></div>
        <div class="item-main">
          <a class="item-title" href="${escapeHtml(f.url)}" target="_blank" rel="noopener">${escapeHtml(f.name)}</a>
          <div class="item-meta"><span>${flag(f.country)} ${escapeHtml(f.city)}</span><span>${fmt(f.start)}${f.end && f.end !== f.start ? `–${fmt(f.end)}` : ""}</span>${f.confidence === "unsure" ? `<span title="Datum noch nicht offiziell bestätigt">Datum vorläufig</span>` : ""}</div>
          ${f.note ? `<p class="item-sum">${escapeHtml(f.note)}</p>` : ""}
        </div>
        <span class="due ${d <= 14 ? "soon" : ""}">${when}</span>
      </li>`;
    }).join("")}</ol>`;
  }

  function renderBriefing() {
    const show = activeTab === "alle" && !search.value.trim() && !onlyNew.checked;
    briefing.hidden = !show;
    if (!show) return;

    const twoDays = Date.now() - 2 * 86400000;
    const leads = data.topics.filter((t) => t.id !== "ausschreibungen").map((t) => {
      const recent = t.items.filter((i) => i.published && new Date(i.published) > twoDays);
      const pool = recent.length ? recent : t.items;
      const top = Math.max(-1, ...pool.map((i) => i.ai?.score ?? -1));
      const best = pool.filter((i) => (i.ai?.score ?? -1) === top);
      return { topic: t, item: best.find((i) => i.image) || best[0] };
    }).filter((l) => l.item);

    const tenders = data.topics.find((t) => t.id === "ausschreibungen")?.items || [];
    const open = tenders.filter((i) => i.deadline && new Date(i.deadline) > Date.now())
      .sort((a, b) => a.deadline.localeCompare(b.deadline));
    const list = (open.length ? open : tenders).slice(0, 5);

    // Das Wichtigste der Woche: nach KI-Relevanz, sonst nach Datum, ohne die Top-Meldungen oben
    const leadLinks = new Set(leads.map((l) => l.item.link));
    const week = Date.now() - 7 * 86400000;
    const digest = data.topics.filter((t) => t.id !== "ausschreibungen")
      .flatMap((t) => t.items.filter((i) => i.published && new Date(i.published) > week && !leadLinks.has(i.link)))
      .sort((a, b) => ((b.ai?.score ?? 5) - (a.ai?.score ?? 5)) || b.published.localeCompare(a.published))
      .slice(0, 8);

    briefing.innerHTML = `
      <div class="main-col">
        <h2 class="section-title">Top-Meldungen</h2>
        <div class="leads">${leads.map(({ topic, item }) => `
          <a class="lead" href="${escapeHtml(item.link)}" target="_blank" rel="noopener" style="--tc:${meta(topic.id).color}">
            ${item.image ? img(item.image, "lead-img") : `<div class="lead-img">${meta(topic.id).icon}</div>`}
            <div class="lead-body">
              <span class="chip">${meta(topic.id).icon} ${escapeHtml(topic.short || topic.name)}</span>
              <span class="item-title">${escapeHtml(titleOf(item))}</span>
              <span class="item-meta"><span class="src">${favicon(item.domain)}${escapeHtml(item.source)}</span>${item.published ? `<span>${relTime(item.published)}</span>` : ""}</span>
            </div>
          </a>`).join("")}
        </div>
        ${digest.length ? `<div class="tile digest">
          <header class="tile-head"><span class="tile-icon">📌</span><h2>Das Wichtigste der Woche</h2><span class="count">aus allen Rubriken</span></header>
          <ol class="items">${digest.map((it) => renderItem(it, "", true)).join("")}</ol>
        </div>` : ""}
      </div>
      <div class="side">
        <div class="deadlines">
          <h2 class="section-title">🪖 ${open.length ? "Offene Ausschreibungen" : "Neueste Ausschreibungen"}</h2>
          ${list.length ? `<ol>${list.map((it) => tenderRow(it)).join("")}</ol>`
            : `<p class="empty">Gerade keine Ausschreibungen zu ballistischen Helmen.</p>`}
        </div>
        ${fairs.length ? `<div class="deadlines events">
          <h2 class="section-title">📅 Nächste Messen</h2>
          ${fairList(4)}
          <button type="button" class="more" data-kpi="termine">Alle Termine</button>
        </div>` : ""}
      </div>`;
  }

  // ------------------------------------------------------------ Kacheln

  function renderItem(it, q, withTopic = false) {
    return `<li class="item">
      <div class="item-main">
        <a class="item-title" href="${escapeHtml(it.link)}" target="_blank" rel="noopener">${highlight(titleOf(it), q)}</a>
        <div class="item-meta">
          ${isNew(it) ? `<span class="new-dot">● neu</span>` : ""}
          ${hotBadge(it)}
          ${withTopic ? `<span class="chip" style="--tc:${meta(it.topic).color}">${meta(it.topic).icon} ${escapeHtml(topicShort(it.topic))}</span>` : ""}
          ${it.kind === "paper" ? `<span class="paper">📄 Fachartikel</span>` : ""}
          <span class="src">${favicon(it.domain)}${escapeHtml(it.source)}</span>
          ${it.published ? `<time datetime="${it.published}" title="${new Date(it.published).toLocaleString("de-DE")}">${relTime(it.published)}</time>` : ""}
          ${starBtn(it)}
        </div>
        ${summaryOf(it) ? `<p class="item-sum${german() && it.ai?.summary ? " ai" : ""}">${highlight(summaryOf(it), q)}</p>` : ""}
      </div>
      ${img(it.image, "thumb")}
    </li>`;
  }

  function renderGroupedItems(items, q) {
    let html = "", current = null, open = false;
    for (const it of items) {
      const label = dayLabel(it.published);
      if (label !== current) {
        if (open) html += "</ol>";
        html += `<h3 class="day">${escapeHtml(label)}</h3><ol class="items">`;
        current = label; open = true;
      }
      html += renderItem(it, q);
    }
    return html + (open ? "</ol>" : "");
  }

  function renderTenderTable(topic, q) {
    const now = Date.now();
    const all = filtered(topic);
    const isOpen = (i) => !!i.deadline && new Date(i.deadline) > now;
    const countries = {};
    const cc = (i) => i.country || "-";
    for (const i of all) countries[cc(i)] = (countries[cc(i)] || 0) + 1;
    if (tenderCountry && !countries[tenderCountry]) tenderCountry = "";
    const rows = all.filter((i) => !tenderCountry || cc(i) === tenderCountry)
      .sort((a, b) => (isOpen(b) - isOpen(a)) ||
        (isOpen(a) ? a.deadline.localeCompare(b.deadline) : (b.published || "").localeCompare(a.published || "")));
    const nOpen = all.filter(isOpen).length;
    const nSoon = all.filter((i) => isOpen(i) && new Date(i.deadline) - now <= 7 * 86400000).length;
    const chip = (c, label, n) => `<button type="button" data-country="${escapeHtml(c)}" aria-pressed="${tenderCountry === c}">${label} <b>${n}</b></button>`;
    const chips = chip("", "Alle Länder", all.length) + Object.entries(countries).sort((a, b) => b[1] - a[1])
      .map(([c, n]) => chip(c, c !== "-" ? `${flag(c)} ${escapeHtml(c)}` : "🌐 ohne Land", n)).join("");

    const row = (it) => {
      const due = dueInfo(it.deadline);
      const days = it.deadline ? (new Date(it.deadline) - now) / 86400000 : null;
      const pct = days == null || days < 0 ? 0 : Math.max(4, Math.min(100, (days / 60) * 100));
      return `<tr class="${due?.expired ? "expired" : ""}">
        <td class="t-flag" title="${escapeHtml(it.country || "")}">${it.country ? flag(it.country) : "🌐"}<small>${escapeHtml(it.country || "")}</small></td>
        <td class="t-main">
          <a class="item-title" href="${escapeHtml(it.link)}" target="_blank" rel="noopener">${highlight(titleOf(it), q)}</a>
          <div class="item-meta">
            ${isNew(it) ? `<span class="new-dot">● neu</span>` : ""}
            ${hotBadge(it)}
            ${it.buyer ? `<span>${highlight(it.buyer, q)}</span>` : `<span class="src">${favicon(it.domain)}${escapeHtml(it.source)}</span>`}
            ${starBtn(it)}
          </div>
          ${it.ai?.facts ? `<p class="facts">${escapeHtml(it.ai.facts)}</p>` : ""}
          ${german() && it.ai?.summary ? `<p class="item-sum ai">${highlight(it.ai.summary, q)}</p>` : ""}
        </td>
        <td class="t-pub">${it.published ? `<span class="t-label">Veröffentlicht </span>${fmtDate(it.published)}` : "–"}</td>
        <td class="t-due">${due ? `<span class="due ${due.cls}" title="${escapeHtml(due.title || "")}">${due.text}</span>
          ${pct ? `<div class="due-bar ${due.cls}"><span style="width:${pct}%"></span></div>` : ""}
          ${due.expired ? "" : `<small>${fmtDate(it.deadline)}</small>`}` : `<span class="t-none">keine Frist bekannt</span>`}</td>
      </tr>`;
    };
    return `<section class="tile tender-view" style="--tc:${meta(topic.id).color}">
      <header class="tile-head">
        <span class="tile-icon">${meta(topic.id).icon}</span>
        <h2>${escapeHtml(topic.name)}</h2>
        <span class="count">${nOpen} offen${nSoon ? ` · <b class="warn">${nSoon} Frist ≤ 7 Tage</b>` : ""}</span>
        <button type="button" class="export" data-export="1" title="Als CSV-Datei für Excel herunterladen">⬇ Excel</button>
      </header>
      <div class="country-chips">${chips}</div>
      ${rows.length ? `<div class="table-wrap"><table class="tenders">
        <thead><tr><th>Land</th><th>Ausschreibung</th><th>Veröffentlicht</th><th>Frist</th></tr></thead>
        <tbody>${rows.map(row).join("")}</tbody></table></div>`
        : `<p class="empty">${topic.items.length ? "Keine Treffer." : "Gerade keine Ausschreibungen zu ballistischen Helmen."}</p>`}
    </section>`;
  }

  function exportTenders() {
    const topic = data.topics.find((t) => t.id === "ausschreibungen");
    const rows = filtered(topic).filter((i) => !tenderCountry || (i.country || "-") === tenderCountry);
    const cell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const day = (iso) => iso ? iso.slice(0, 10) : "";
    const lines = [["Land", "Titel", "Titel (Deutsch)", "Auftraggeber", "Veröffentlicht", "Frist", "Menge/Details", "KI-Relevanz", "Quelle", "Link"],
      ...rows.map((i) => [i.country, i.title, i.ai?.title_de, i.buyer, day(i.published), day(i.deadline), i.ai?.facts, i.ai?.score, i.source, i.link])];
    const csv = "\ufeff" + lines.map((r) => r.map(cell).join(";")).join("\r\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    a.download = `helm-radar-ausschreibungen-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  function renderSaved(q) {
    const items = filterItems(Object.values(saved)).sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
    return `<section class="tile" style="--tc:var(--star)">
      <header class="tile-head">
        <span class="tile-icon">⭐</span>
        <h2>Merkliste</h2>
        <span class="count">${items.length} gemerkt</span>
      </header>
      ${items.length ? `<ol class="items">${items.map((it) => it.topic === "ausschreibungen"
          ? tenderRow(it, q).replace('class="tender', 'class="item tender') : renderItem(it, q)).join("")}</ol>`
        : `<p class="empty">Noch nichts gemerkt. Tippe bei einer Meldung auf ☆, dann bleibt sie hier, auch wenn sie von der Seite verschwindet.</p>`}
    </section>`;
  }

  function renderGrid() {
    const q = search.value.trim();
    if (activeTab === "termine") {
      grid.classList.add("single");
      grid.innerHTML = `<section class="tile" style="--tc:var(--t-konkurrenz)">
        <header class="tile-head"><span class="tile-icon">📅</span><h2>Messen und Termine</h2>
          <span class="count">${upcomingFairs().length} Termine</span></header>
        <div class="fair-wrap">${fairList(50)}</div>
        <p class="empty small">Ein Klick auf den Namen öffnet die offizielle Messeseite. Termine bitte vor der Reiseplanung dort prüfen.</p>
      </section>`;
      return;
    }
    if (activeTab === "gemerkt" || activeTab === "ausschreibungen") {
      grid.classList.add("single");
      const topic = data.topics.find((t) => t.id === "ausschreibungen");
      grid.innerHTML = activeTab === "gemerkt" ? renderSaved(q) : renderTenderTable(topic, q);
      return;
    }
    const topics = activeTab === "alle" ? data.topics : data.topics.filter((t) => t.id === activeTab);
    grid.classList.toggle("single", activeTab !== "alle");
    grid.innerHTML = topics.map((topic) => {
      const items = filtered(topic);
      const showAll = expanded.has(topic.id) || activeTab !== "alle";
      const shown = showAll ? items : items.slice(0, PAGE);
      const isTender = topic.id === "ausschreibungen";
      const body = !shown.length
        ? `<p class="empty">${topic.items.length ? "Keine Treffer." : isTender ? "Gerade keine Ausschreibungen zu ballistischen Helmen." : "Noch keine Meldungen."}</p>`
        : isTender ? `<ol class="items">${shown.map((it) => tenderRow(it, q).replace('class="tender', 'class="item tender')).join("")}</ol>`
        : renderGroupedItems(shown, q);
      const count = items.length === topic.items.length ? `${items.length} Meldungen` : `${items.length} von ${topic.items.length}`;
      return `<section class="tile" style="--tc:${meta(topic.id).color}">
        <header class="tile-head">
          <span class="tile-icon">${meta(topic.id).icon}</span>
          <h2>${escapeHtml(topic.name)}</h2>
          <span class="count">${count}</span>
        </header>
        ${body}
        ${!showAll && items.length > PAGE ? `<button class="more" type="button" data-more="${topic.id}">Alle ${items.length} anzeigen</button>` : ""}
      </section>`;
    }).join("");
  }

  function renderSources() {
    const rows = [...data.sources].sort((a, b) => a.ok - b.ok || a.topic.localeCompare(b.topic));
    const ok = rows.filter((s) => s.ok).length;
    $("#sources summary").textContent = `Quellen-Status: ${ok} von ${rows.length} erreichbar`;
    const topicName = Object.fromEntries(data.topics.map((t) => [t.id, t.short || t.name]));
    $("#sources-body").innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Quelle</th><th>Thema</th><th>Ergebnis</th></tr></thead>
      <tbody>${rows.map((s) => `<tr>
        <td><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.name)}</a></td>
        <td>${escapeHtml(topicName[s.topic] || s.topic)}</td>
        <td class="${s.ok ? "" : "bad"}">${s.ok ? `${s.count} Meldungen${s.total > s.count ? ` (von ${s.total}, gefiltert)` : ""}` : escapeHtml(s.error || "Fehler")}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  function setTab(id) {
    activeTab = id;
    store.set(TAB_KEY, activeTab);
    history.replaceState(null, "", id === "alle" ? location.pathname + location.search : `#${id}`);
  }

  function render() { renderTabs(); renderKpis(); renderSince(); renderBriefing(); renderGrid(); }

  function toggleSave(link) {
    if (saved[link]) delete saved[link];
    else {
      const it = byLink.get(link);
      if (!it) return;
      saved[link] = { ...it, savedAt: Date.now() };
    }
    store.set(SAVED_KEY, JSON.stringify(saved));
    renderTabs();
    if (activeTab === "gemerkt") { renderGrid(); return; }
    document.querySelectorAll(`button[data-save="${CSS.escape(link)}"]`).forEach((b) => { b.outerHTML = starBtn({ link }); });
  }

  tabs.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    setTab(b.dataset.tab);
    render();
    window.scrollTo({ top: 0 });
  });
  document.addEventListener("click", (e) => {
    const s = e.target.closest("button[data-save]");
    if (s) { e.preventDefault(); toggleSave(s.dataset.save); return; }
    const c = e.target.closest("button[data-country]");
    if (c) { tenderCountry = c.dataset.country; renderGrid(); return; }
    if (e.target.closest("button[data-export]")) { exportTenders(); return; }
    const k = e.target.closest("button[data-kpi]");
    if (k) { setTab(k.dataset.kpi); render(); window.scrollTo({ top: 0 }); return; }
    const n = e.target.closest("button[data-since]");
    if (n) {
      setTab(n.dataset.since);
      onlyNew.checked = true;
      render();
      window.scrollTo({ top: 0 });
      return;
    }
    const b = e.target.closest("button[data-more]");
    if (b) { expanded.add(b.dataset.more); renderGrid(); }
  });
  const refresh = () => { renderKpis(); renderSince(); renderBriefing(); renderGrid(); };
  addEventListener("hashchange", () => { activeTab = decodeURIComponent(location.hash.slice(1)) || "alle"; if (data) render(); });
  search.addEventListener("input", refresh);
  onlyNew.addEventListener("change", refresh);
  deutsch.checked = store.get(LANG_KEY) !== "0";
  deutsch.addEventListener("change", () => { store.set(LANG_KEY, deutsch.checked ? "1" : "0"); refresh(); });

  // Messetermine (gepflegte Liste im Repo); fehlt sie, bleibt der Bereich einfach leer
  const fairsLoaded = fetch(`messen.json?t=${Date.now()}`).then((r) => r.ok ? r.json() : []).catch(() => [])
    .then((f) => { fairs = Array.isArray(f) ? f : []; });

  Promise.all([fetch(`data/news.json?t=${Date.now()}`), fairsLoaded]).then(([r]) => r)
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((json) => {
      data = json;
      if (!["gemerkt", "termine"].includes(activeTab) && !data.topics.some((t) => t.id === activeTab)) activeTab = "alle";
      for (const t of data.topics) for (const it of t.items) byLink.set(it.link, it);
      // Gemerkte Meldungen mit dem aktuellen Stand auffrischen (z. B. neue KI-Zusammenfassung)
      for (const link of Object.keys(saved)) if (byLink.has(link)) saved[link] = { ...byLink.get(link), savedAt: saved[link].savedAt };
      const gen = new Date(data.generated);
      $("#updated").textContent = `Aktualisiert ${relTime(data.generated)} (${gen.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" })})`;
      if (!lastVisit) onlyNew.parentElement.hidden = true;
      render();
      renderSources();
      // Besuch erst beim Verlassen merken, damit "neu" während des Lesens sichtbar bleibt
      addEventListener("pagehide", () => store.set(LAST_VISIT_KEY, data.generated));
    })
    .catch((err) => {
      $("#updated").textContent = `Meldungen konnten nicht geladen werden (${err.message}).`;
    });
})();
