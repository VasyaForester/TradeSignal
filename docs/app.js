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

function renderMarketBrief(brief) {
  const valueEl = document.querySelector("#imoex-value");
  const changeEl = document.querySelector("#imoex-change");
  const stanceEl = document.querySelector("#brief-stance");
  if (!brief || !Number.isFinite(brief.value) || brief.value <= 0) {
    if (valueEl) valueEl.textContent = "—";
    if (changeEl) {
      changeEl.textContent = "нет котировки";
      changeEl.className = "flat";
    }
    if (stanceEl) stanceEl.textContent = "Индекс МосБиржи сейчас недоступен. Оценка рынка появится после следующего снимка.";
    document.querySelector("#brief-why").textContent = "Котировка IMOEX не получена.";
    document.querySelector("#brief-outlook").textContent = "Без индекса нельзя отделить широкий рынок от движения отдельных бумаг.";
    document.querySelector("#brief-longs").textContent = "Смотрите блок лучших идей на 12 месяцев.";
    return;
  }
  const change = Number(brief.dayChange);
  valueEl.textContent = rub.format(brief.value);
  changeEl.textContent = pct(change);
  changeEl.className = change > 0.15 ? "up" : change < -0.15 ? "down" : "flat";
  const stanceLabel = brief.stance === "растет"
    ? "Рынок растет"
    : brief.stance === "падает"
      ? "Рынок падает"
      : "Рынок в боковике";
  stanceEl.textContent = `${stanceLabel} за сутки. Горизонт: ${brief.horizon || "краткосрочно"}. Лонги: ${brief.longVerdict || "точечно"}.`;
  document.querySelector("#brief-why").textContent = brief.why || "";
  document.querySelector("#brief-outlook").textContent = brief.outlook || "";
  document.querySelector("#brief-longs").textContent = brief.longAdvice || "";
  const tickers = document.querySelector("#brief-tickers");
  tickers.innerHTML = (brief.longTickers || [])
    .map((item) => `<span>#${escapeHtml(item.secid)}</span>`)
    .join("");
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
        ${Number.isFinite(item.impactEstimatePct) ? `
          <div class="signal-impact">
            <span>сценарий ${pct(item.impactEstimatePct)}</span>
            <span>уверенность ${item.impactConfidence}%</span>
            <span>entity ${item.entityConfidence}%</span>
            ${item.eventType ? `<span>${escapeHtml(item.eventType)}</span>` : ""}
            ${Number.isFinite(item.signalScore) ? `<span>score ${rub.format(item.signalScore)}</span>` : ""}
          </div>
        ` : ""}
        <div class="signal-tags">${(item.hashtags || []).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</div>
      </div>
      <div class="urgent-source">
        <b>${item.strength}/100</b>
        <a href="${escapeHtml(item.source.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source.publisher)}${item.sourceCount > 1 ? ` +${item.sourceCount - 1}` : ""} ↗</a>
      </div>
    </article>
  `).join("");
}

function renderScalp(items) {
  const list = document.querySelector("#scalp-list");
  const counter = document.querySelector("#scalp-count");
  if (counter) counter.textContent = items.length;
  if (!list) return;
  if (!items.length) {
    list.innerHTML = `<div class="empty-state">Сейчас нет явных кандидатов на краткосрочный отскок. Ждем аномальную просадку относительно рынка.</div>`;
    return;
  }
  list.innerHTML = items.map((item) => `
    <article class="urgent-card scalp-card">
      <span class="action buy">ОТСКОК</span>
      <div class="urgent-copy">
        <h3>${escapeHtml(item.ticker)} · ${escapeHtml(item.title)}</h3>
        <p>${escapeHtml(item.summary)}</p>
        <div class="signal-impact">
          <span>сессия ${pct(item.dayChange)}</span>
          ${Number.isFinite(item.marketChange) ? `<span>рынок ${pct(item.marketChange)}</span>` : ""}
          ${Number.isFinite(item.excessDrop) ? `<span>хуже рынка на ${rub.format(item.excessDrop)} п.п.</span>` : ""}
          ${item.horizon ? `<span>${escapeHtml(item.horizon)}</span>` : ""}
          ${Number.isFinite(item.price) ? `<span>${price(item.price)}</span>` : ""}
        </div>
        <div class="signal-tags">${(item.hashtags || []).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</div>
      </div>
      <div class="urgent-source">
        <b>${item.strength}/100</b>
        <a href="${escapeHtml(item.source.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source.publisher)} ↗</a>
      </div>
    </article>
  `).join("");
}

function typeNote(type) {
  if (type === "stocks") {
    return state.data.stockModel
      || "Таргет считает модель TradeSignal по цене, импульсу, качеству бизнеса и свежим новостям. Наведите на строку, чтобы увидеть тезис, риск и драйверы.";
  }
  if (type === "bonds") {
    return "Сценарная доходность включает YTM и возможное изменение цены при снижении ставки. Продажа до погашения может дать убыток.";
  }
  return state.data.fundModel;
}

function driverSummary(item) {
  const drivers = item.targetDrivers || {};
  const parts = [
    ["импульс", drivers.impulse],
    ["фундамент", drivers.fundamental],
    ["новости", drivers.news],
    ["макро", drivers.macro],
  ].filter(([, value]) => Number.isFinite(value) && value !== 0);
  if (!parts.length) return "";
  return parts.map(([label, value]) => `${label} ${pct(value)}`).join(" · ");
}

function subtitle(item, type) {
  if (type === "stocks") return `модель ${price(item.targetPrice)} · дивиденд ${price(item.dividend12m)}`;
  if (type === "bonds") return `${item.kind} · погашение ${item.maturity}`;
  const category = item.categoryLabel ? `${item.categoryLabel} · ` : "";
  return `${category}3 мес. ${pct(item.return3m)} · 12 мес. ${pct(item.return12m)}`;
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
        <div><b>Почему в списке</b>${escapeHtml(item.thesis)}${type === "stocks" && driverSummary(item) ? `<span class="driver-line">${escapeHtml(driverSummary(item))}</span>` : ""}</div>
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

function renderPipeline(metrics) {
  const container = document.querySelector("#pipeline-metrics");
  if (!metrics) {
    container.innerHTML = `<div class="empty-state">Метрики появятся после следующего обновления данных.</div>`;
    return;
  }
  const latency = metrics.latencyMs >= 1000
    ? `${rub.format(metrics.latencyMs / 1000)} с`
    : `${metrics.latencyMs} мс`;
  const values = [
    [metrics.fetched, "получено"],
    [metrics.signals, "сигналов"],
    [metrics.duplicatesMerged, "дублей слито"],
    [`${Math.round(metrics.dedupRate * 100)}%`, "dedup-rate"],
    [`${Math.round(metrics.entityLinkPrecision * 100)}%`, "entity precision"],
    [`${Math.round(metrics.entityLinkRecall * 100)}%`, "entity recall"],
    [latency, "время обработки"],
  ];
  container.innerHTML = values.map(([value, label]) => `
    <div class="pipeline-metric"><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span></div>
  `).join("");
}

function render(data) {
  state.data = data;
  document.querySelector("#updated-at").textContent = freshnessLabel(data.generatedAt);
  document.querySelector("#rate-now").textContent = `${rub.format(data.macro.currentKeyRate)}%`;
  document.querySelector("#rate-target").textContent = `${rub.format(data.macro.forecastKeyRate12m)}%`;
  document.querySelector("#macro-note").textContent = data.macro.note;
  document.querySelector("#fund-method").textContent = data.fundModel;
  const stockMethod = document.querySelector("#stock-method");
  if (stockMethod) stockMethod.textContent = data.stockModel || typeNote("stocks");
  document.querySelector("#disclaimer").textContent = data.disclaimer;
  renderMarketBrief(data.marketBrief);
  renderUrgent(data.urgent || []);
  renderScalp(data.scalp || []);
  renderHealth(data.sourceHealth || []);
  renderPipeline(data.pipelineMetrics);
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
