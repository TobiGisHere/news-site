(() => {
  const PAGE = 8;            // Meldungen pro Kachel vor "Mehr anzeigen"
  const LAST_VISIT_KEY = "news:lastVisit";
  const TAB_KEY = "news:tab";

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* egal */ } },
  };

  const $ = (sel) => document.querySelector(sel);
  const grid = $("#grid"), tabs = $("#tabs"), search = $("#search"), onlyNew = $("#only-new");
  const lastVisit = store.get(LAST_VISIT_KEY);
  let data = null;
  let activeTab = store.get(TAB_KEY) || "alle";
  const expanded = new Set();

  const rtf = new Intl.RelativeTimeFormat("de", { numeric: "auto" });
  function relTime(iso) {
    if (!iso) return "";
    const diff = (new Date(iso) - Date.now()) / 1000;
    const steps = [[60, "second"], [3600, "minute"], [86400, "hour"], [604800, "day"], [2629800, "week"], [Infinity, "month"]];
    const div = { second: 1, minute: 60, hour: 3600, day: 86400, week: 604800, month: 2629800 };
    for (const [limit, unit] of steps) {
      if (Math.abs(diff) < limit) return rtf.format(Math.round(diff / div[unit]), unit);
    }
  }
  const fmtDate = (iso) => new Date(iso).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });

  const isNew = (it) => lastVisit && it.published && it.published > lastVisit;

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function highlight(text, q) {
    const safe = escapeHtml(text);
    if (!q) return safe;
    const rx = new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
    return safe.replace(rx, "<mark>$1</mark>");
  }

  function filtered(topic) {
    const q = search.value.trim().toLowerCase();
    return topic.items.filter((it) =>
      (!onlyNew.checked || isNew(it)) &&
      (!q || `${it.title} ${it.summary} ${it.source}`.toLowerCase().includes(q)));
  }

  function renderTabs() {
    const btn = (id, label, badge) =>
      `<button type="button" data-tab="${id}" aria-pressed="${activeTab === id}">${escapeHtml(label)}${badge ? `<span class="badge">${badge} neu</span>` : ""}</button>`;
    const totalNew = data.topics.reduce((n, t) => n + t.items.filter(isNew).length, 0);
    tabs.innerHTML = btn("alle", "Alle", totalNew) +
      data.topics.map((t) => btn(t.id, t.short || t.name, t.items.filter(isNew).length)).join("");
  }

  function renderItem(it, q) {
    const meta = [
      isNew(it) ? `<span class="new-dot">● neu</span>` : "",
      `<span>${escapeHtml(it.source)}</span>`,
      it.published ? `<time datetime="${it.published}" title="${new Date(it.published).toLocaleString("de-DE")}">${relTime(it.published)}</time>` : "",
      it.deadline ? `<span class="deadline">Frist ${fmtDate(it.deadline)}</span>` : "",
    ].join("");
    return `<li>
      <a class="item-title" href="${escapeHtml(it.link)}" target="_blank" rel="noopener">${highlight(it.title, q)}</a>
      <div class="item-meta">${meta}</div>
      ${it.summary ? `<p class="item-sum">${highlight(it.summary, q)}</p>` : ""}
    </li>`;
  }

  function renderGrid() {
    const q = search.value.trim();
    const topics = activeTab === "alle" ? data.topics : data.topics.filter((t) => t.id === activeTab);
    grid.classList.toggle("single", activeTab !== "alle");
    grid.innerHTML = "";
    const tpl = $("#tile-tpl");
    for (const topic of topics) {
      const items = filtered(topic);
      const node = tpl.content.firstElementChild.cloneNode(true);
      node.classList.add(`prio-${topic.priority}`);
      node.querySelector("h2").textContent = topic.name;
      node.querySelector(".count").textContent = items.length === topic.items.length
        ? `${items.length} Meldungen` : `${items.length} von ${topic.items.length}`;
      const showAll = expanded.has(topic.id) || activeTab !== "alle";
      const shown = showAll ? items : items.slice(0, PAGE);
      const list = node.querySelector(".items");
      list.innerHTML = shown.length ? shown.map((it) => renderItem(it, q)).join("")
        : `<li class="empty">${topic.items.length ? "Keine Treffer." : "Noch keine Meldungen."}</li>`;
      const more = node.querySelector(".more");
      more.hidden = showAll || items.length <= PAGE;
      more.textContent = `Alle ${items.length} anzeigen`;
      more.addEventListener("click", () => { expanded.add(topic.id); renderGrid(); });
      grid.appendChild(node);
    }
  }

  function renderSources() {
    const rows = [...data.sources].sort((a, b) => a.ok - b.ok || a.topic.localeCompare(b.topic));
    const ok = rows.filter((s) => s.ok).length;
    $("#sources summary").textContent = `Quellen-Status: ${ok} von ${rows.length} erreichbar`;
    const topicName = Object.fromEntries(data.topics.map((t) => [t.id, t.name]));
    $("#sources-body").innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Quelle</th><th>Thema</th><th>Ergebnis</th></tr></thead>
      <tbody>${rows.map((s) => `<tr>
        <td><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.name)}</a></td>
        <td>${escapeHtml(topicName[s.topic] || s.topic)}</td>
        <td class="${s.ok ? "" : "bad"}">${s.ok ? `${s.count} Meldungen${s.total > s.count ? ` (von ${s.total}, gefiltert)` : ""}` : escapeHtml(s.error || "Fehler")}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  function render() { renderTabs(); renderGrid(); }

  tabs.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    activeTab = b.dataset.tab;
    store.set(TAB_KEY, activeTab);
    render();
    window.scrollTo({ top: 0 });
  });
  search.addEventListener("input", renderGrid);
  onlyNew.addEventListener("change", renderGrid);

  fetch(`data/news.json?t=${Date.now()}`)
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((json) => {
      data = json;
      if (!data.topics.some((t) => t.id === activeTab)) activeTab = "alle";
      const gen = new Date(data.generated);
      $("#updated").textContent = `Aktualisiert ${relTime(data.generated)} (${gen.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" })})` +
        (lastVisit ? ` · neu = seit deinem letzten Besuch` : "");
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
