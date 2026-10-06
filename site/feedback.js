// Feedback-Knopf: kleines Formular, das über Web3Forms (kostenlos) per Mail bei Tobi ankommt.
// Die Empfängeradresse steckt nur im Web3Forms-Konto, nicht in dieser Seite.
(() => {
  const key = window.FEEDBACK_KEY;
  const btn = document.getElementById("feedback-btn");
  const dlg = document.getElementById("feedback");
  if (!key || !btn || !dlg || !dlg.showModal) return;
  btn.hidden = false;
  document.querySelectorAll("[data-fb-hint]").forEach((el) => (el.hidden = false));

  const form = dlg.querySelector("form");
  const status = dlg.querySelector(".fb-status");
  const send = form.querySelector('button[type="submit"]');
  const open = () => { status.textContent = ""; status.className = "fb-status"; dlg.showModal(); form.message.focus(); };
  btn.addEventListener("click", open);
  document.querySelectorAll("[data-feedback]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); open(); }));
  dlg.querySelector(".fb-close").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const message = form.message.value.trim();
    if (!message) return form.message.focus();
    if (form.botcheck.checked) return;   // Spam-Falle, für Menschen unsichtbar
    const name = form.name.value.trim();
    send.disabled = true;
    status.className = "fb-status";
    status.textContent = "Wird gesendet …";
    try {
      const res = await fetch("https://api.web3forms.com/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          access_key: key,
          subject: `Helm-Radar Feedback${name ? ` von ${name}` : ""}`,
          from_name: "Helm-Radar",
          Name: name || "(anonym)",
          Feedback: message,
          Ansicht: location.hash || "#alle",
          Gerät: matchMedia("(max-width: 700px)").matches ? "Handy" : "Computer",
        }),
      });
      const out = await res.json().catch(() => ({}));
      if (!res.ok || !out.success) throw new Error(out.message || `Fehler ${res.status}`);
      form.reset();
      status.className = "fb-status ok";
      status.textContent = "Danke! Dein Feedback ist angekommen.";
      setTimeout(() => dlg.open && dlg.close(), 2200);
    } catch (err) {
      status.className = "fb-status err";
      status.textContent = `Senden hat nicht geklappt (${err.message}). Bitte später noch einmal versuchen.`;
    } finally {
      send.disabled = false;
    }
  });
})();
