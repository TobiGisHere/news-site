(() => {
  const PAGE = 8;            // Meldungen pro Kachel vor "Mehr anzeigen"
  const LAST_VISIT_KEY = "news:lastVisit";
  const TAB_KEY = "news:tab";
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
  const grid = $("#grid"), tabs = $("#tabs"), search = $("#search"), onlyNew = $("#only-new"), briefing = $("#briefing");
  const lastVisit = store.get(LAST_VISIT_KEY);
  let data = null;
  let activeTab = store.get(TAB_KEY) || "alle";
  const expanded = new Set();

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

  function filtered(topic) {
    const q = search.value.trim().toLowerCase();
    return topic.items.filter((it) =>
      (!onlyNew.checked || isNew(it)) &&
      (!q || `${it.title} ${it.summary} ${it.source} ${it.buyer || ""}`.toLowerCase().includes(q)));
  }

  // ------------------------------------------------------------ Reiter

  function renderTabs() {
    const btn = (id, label, badge, color) =>
      `<button type="button" data-tab="${id}" aria-pressed="${activeTab === id}" style="--tc:${color}">${label}${badge ? `<span class="badge">${badge} neu</span>` : ""}</button>`;
    const totalNew = data.topics.reduce((n, t) => n + t.items.filter(isNew).length, 0);
    tabs.innerHTML = btn("alle", "Überblick", totalNew, "var(--text)") +
      data.topics.map((t) => btn(t.id, `${meta(t.id).icon} ${escapeHtml(t.short || t.name)}`, t.items.filter(isNew).length, meta(t.id).color)).join("");
  }

  // ------------------------------------------------------------ Briefing

  function tenderRow(it, q = "") {
    const due = dueInfo(it.deadline);
    return `<li class="tender ${due?.expired ? "expired" : ""}">
      <span class="flag" title="${escapeHtml(it.country || "")}">${flag(it.country)}</span>
      <div class="item-main">
        <a class="item-title" href="${escapeHtml(it.link)}" target="_blank" rel="noopener">${highlight(it.title, q)}</a>
        <div class="item-meta">
          ${isNew(it) ? `<span class="new-dot">● neu</span>` : ""}
          ${it.buyer ? `<span>${highlight(it.buyer, q)}</span>` : `<span class="src">${favicon(it.domain)}${escapeHtml(it.source)}</span>`}
          ${it.published ? `<span>veröffentlicht ${relTime(it.published)}</span>` : ""}
        </div>
      </div>
      ${due ? `<span class="due ${due.cls}" title="${escapeHtml(due.title || "")}">${due.text}</span>` : ""}
    </li>`;
  }

  function renderBriefing() {
    const show = activeTab === "alle" && !search.value.trim() && !onlyNew.checked;
    briefing.hidden = !show;
    if (!show) return;

    const twoDays = Date.now() - 2 * 86400000;
    const leads = data.topics.filter((t) => t.id !== "ausschreibungen").map((t) => {
      const recent = t.items.filter((i) => i.published && new Date(i.published) > twoDays);
      return { topic: t, item: recent.find((i) => i.image) || recent[0] || t.items[0] };
    }).filter((l) => l.item);

    const tenders = data.topics.find((t) => t.id === "ausschreibungen")?.items || [];
    const open = tenders.filter((i) => i.deadline && new Date(i.deadline) > Date.now())
      .sort((a, b) => a.deadline.localeCompare(b.deadline));
    const list = (open.length ? open : tenders).slice(0, 5);

    briefing.innerHTML = `
      <div>
        <h2 class="section-title">Top-Meldungen</h2>
        <div class="leads">${leads.map(({ topic, item }) => `
          <a class="lead" href="${escapeHtml(item.link)}" target="_blank" rel="noopener" style="--tc:${meta(topic.id).color}">
            ${item.image ? img(item.image, "lead-img") : `<div class="lead-img">${meta(topic.id).icon}</div>`}
            <div class="lead-body">
              <span class="chip">${meta(topic.id).icon} ${escapeHtml(topic.short || topic.name)}</span>
              <span class="item-title">${escapeHtml(item.title)}</span>
              <span class="item-meta"><span class="src">${favicon(item.domain)}${escapeHtml(item.source)}</span>${item.published ? `<span>${relTime(item.published)}</span>` : ""}</span>
            </div>
          </a>`).join("")}
        </div>
      </div>
      <div class="deadlines">
        <h2 class="section-title">🪖 ${open.length ? "Offene Ausschreibungen" : "Neueste Ausschreibungen"}</h2>
        ${list.length ? `<ol>${list.map((it) => tenderRow(it)).join("")}</ol>`
          : `<p class="empty">Gerade keine Ausschreibungen zu ballistischen Helmen.</p>`}
      </div>`;
  }

  // ------------------------------------------------------------ Kacheln

  function renderItem(it, q) {
    return `<li class="item">
      <div class="item-main">
        <a class="item-title" href="${escapeHtml(it.link)}" target="_blank" rel="noopener">${highlight(it.title, q)}</a>
        <div class="item-meta">
          ${isNew(it) ? `<span class="new-dot">● neu</span>` : ""}
          ${it.kind === "paper" ? `<span class="paper">📄 Fachartikel</span>` : ""}
          <span class="src">${favicon(it.domain)}${escapeHtml(it.source)}</span>
          ${it.published ? `<time datetime="${it.published}" title="${new Date(it.published).toLocaleString("de-DE")}">${relTime(it.published)}</time>` : ""}
        </div>
        ${it.summary ? `<p class="item-sum">${highlight(it.summary, q)}</p>` : ""}
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

  function renderGrid() {
    const q = search.value.trim();
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

  function render() { renderTabs(); renderBriefing(); renderGrid(); }

  tabs.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    activeTab = b.dataset.tab;
    store.set(TAB_KEY, activeTab);
    render();
    window.scrollTo({ top: 0 });
  });
  grid.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-more]");
    if (!b) return;
    expanded.add(b.dataset.more);
    renderGrid();
  });
  search.addEventListener("input", () => { renderBriefing(); renderGrid(); });
  onlyNew.addEventListener("change", () => { renderBriefing(); renderGrid(); });

  fetch(`data/news.json?t=${Date.now()}`)
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((json) => {
      data = json;
      if (!data.topics.some((t) => t.id === activeTab)) activeTab = "alle";
      const gen = new Date(data.generated);
      $("#updated").textContent = `Aktualisiert ${relTime(data.generated)} (${gen.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" })})` +
        (lastVisit ? " · neu = seit deinem letzten Besuch" : "");
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
