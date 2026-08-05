const state = { data: null, type: "stocks" };

const rub = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const pct = (value) => `${value >= 0 ? "+" : ""}${rub.format(value)}%`;
const price = (value) => `${rub.format(value)} ₽`;
const escapeHtml = (value = "") => String(value).replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
}[char]));

function freshnessLabel(dateString) {
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) return "дата не указана";
  return date.toLocaleString("ru-RU", {
    day: "numeric", month: "long", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Moscow"
  });
}

function renderUrgent(items) {
  const list = document.querySelector("#urgent-list");
  document.querySelector("#signal-count").textContent = items.length;
  if (!items.length) {
    list.innerHTML = `<div class="empty-state">Сильных краткосрочных событий сейчас не обнаружено. Это не означает отсутствия рыночного риска.</div>`;
    return;
  }
  list.innerHTML = items.map((item) => `
    <article class="urgent-card">
      <span class="action ${item.action.toLowerCase()}">${item.action === "SELL" ? "СНИЗИТЬ" : "СМОТРЕТЬ"}</span>
      <div class="urgent-copy">
        <h3>${escapeHtml(item.ticker)} · ${escapeHtml(item.title)}</h3>
        <p>${escapeHtml(item.summary)}</p>
        <div class="signal-tags">${(item.hashtags || []).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</div>
      </div>
      <div class="urgent-source">
        <b>${item.strength}/100</b>
        <a href="${escapeHtml(item.source.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source.publisher)}${item.sourceCount > 1 ? ` +${item.sourceCount - 1}` : ""} ↗</a>
      </div>
    </article>
  `).join("");
}

function typeNote(type) {
  if (type === "stocks") {
    return "Ранжирование по полной ожидаемой доходности: целевая цена + прогнозный дивиденд. Наведите на строку, чтобы увидеть тезис и риск.";
  }
  if (type === "bonds") {
    return "Сценарная доходность включает YTM и возможное изменение цены при снижении ставки. Продажа до погашения может дать убыток.";
  }
  return state.data.fundModel;
}

function subtitle(item, type) {
  if (type === "stocks") return `таргет ${price(item.targetPrice)} · дивиденд ${price(item.dividend12m)}`;
  if (type === "bonds") return `${item.kind} · погашение ${item.maturity}`;
  return `3 мес. ${pct(item.return3m)} · 12 мес. ${pct(item.return12m)}`;
}

function secondaryMetric(item, type) {
  if (type === "stocks") return `<b>${price(item.price)}</b><small>сейчас · день ${pct(item.dayChange)}</small>`;
  if (type === "bonds") return `<b>${rub.format(item.price)}%</b><small>от номинала</small>`;
  return `<b>${price(item.price)}</b><small>день ${pct(item.dayChange)}</small>`;
}

function expectationMetric(item, type) {
  const label = type === "bonds" ? `YTM ${rub.format(item.yield)}%` : "модель · 12 мес.";
  return `<b class="expected">${pct(item.expectedReturn)}</b><small>${label}</small>`;
}

function renderRanking(type) {
  state.type = type;
  const items = state.data[type] || [];
  document.querySelector("#category-note").textContent = typeNote(type);
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.type === type));
  const ranking = document.querySelector("#ranking");
  if (!items.length) {
    ranking.innerHTML = `<div class="empty-state">Источник временно недоступен. Проверьте статус данных ниже.</div>`;
    return;
  }
  ranking.innerHTML = items.map((item, index) => `
    <article class="rank-card">
      <div class="instrument">
        <span class="rank-number">0${index + 1}</span>
        <span class="ticker-icon">${escapeHtml(item.secid.slice(0, 4))}</span>
        <span class="instrument-name">
          <b>${escapeHtml(item.name)}</b>
          <span>${escapeHtml(subtitle(item, type))}</span>
        </span>
      </div>
      <div class="metric">${secondaryMetric(item, type)}</div>
      <div class="metric">${expectationMetric(item, type)}</div>
      <div class="metric confidence">
        <div class="confidence-track"><i style="width:${item.confidence}%"></i></div>
        <b>${item.confidence}%</b>
      </div>
      <div class="details">
        <div><b>Почему в списке</b>${escapeHtml(item.thesis)}</div>
        <div><b>Ключевой риск</b>${escapeHtml(item.risks)}</div>
      </div>
    </article>
  `).join("");
}

function renderHealth(items) {
  document.querySelector("#source-health").innerHTML = items.map((item) => `
    <div class="source-item">
      <i class="source-dot ${escapeHtml(item.status)}"></i>
      <div>${escapeHtml(item.source)}<span>${escapeHtml(item.status === "ok" ? item.detail : item.status)}</span></div>
    </div>
  `).join("");
  const errors = items.filter((item) => item.status === "error").length;
  const stale = items.filter((item) => item.status === "stale").length;
  const label = errors ? `${errors} источника недоступны` : stale ? "Часть данных из прошлого снимка" : "Источники отвечают";
  document.querySelector("#market-state").textContent = label;
}

function render(data) {
  state.data = data;
  document.querySelector("#updated-at").textContent = freshnessLabel(data.generatedAt);
  document.querySelector("#rate-now").textContent = `${rub.format(data.macro.currentKeyRate)}%`;
  document.querySelector("#rate-target").textContent = `${rub.format(data.macro.forecastKeyRate12m)}%`;
  document.querySelector("#macro-note").textContent = data.macro.note;
  document.querySelector("#fund-method").textContent = data.fundModel;
  document.querySelector("#disclaimer").textContent = data.disclaimer;
  renderUrgent(data.urgent || []);
  renderHealth(data.sourceHealth || []);
  renderRanking(state.type);
}

async function init() {
  try {
    const response = await fetch(`data/market-data.json?v=${Date.now()}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    document.querySelector("#market-state").textContent = "Ошибка загрузки снимка";
    document.querySelector("#ranking").innerHTML = `<div class="empty-state">Не удалось прочитать data/market-data.json. Для локального просмотра запустите HTTP-сервер.</div>`;
    console.error(error);
  }
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => renderRanking(tab.dataset.type)));
const dialog = document.querySelector("#method-dialog");
document.querySelector("#method-button").addEventListener("click", () => dialog.showModal());
document.querySelector(".dialog-close").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});

init();
