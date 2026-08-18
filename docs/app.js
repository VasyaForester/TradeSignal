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

function labeledChange(value) {
  if (!Number.isFinite(value)) return { text: "нет хода", cls: "flat" };
  if (value > 0.15) return { text: `рост ${pct(value)}`, cls: "up" };
  if (value < -0.15) return { text: `падение ${pct(value)}`, cls: "down" };
  return { text: `без изменений ${pct(value)}`, cls: "flat" };
}

const REACTION_LABEL = {
  confirmed: "подтверждено",
  underreaction: "слабая реакция",
  overreaction: "переоценка",
  anomaly_down: "позитив, цена вниз",
  anomaly_up: "негатив, цена вверх",
  divergence: "расхождение",
  none: "нет хода",
};

function emptyBlock(text) {
  return `<div class="empty-state">${escapeHtml(text)}</div>`;
}

function renderTape(tape) {
  const root = document.querySelector("#market-tape");
  if (!root) return;
  const items = [
    ["IMOEX", tape?.imoex],
    ["USD/RUB", tape?.usd],
    ["RGBI", tape?.rgbi],
  ];
  root.innerHTML = items.map(([label, quote]) => {
    const value = Number(quote?.value);
    const change = labeledChange(Number(quote?.dayChange));
    return `
      <article class="tape-item">
        <span>${escapeHtml(label)}</span>
        <b>${Number.isFinite(value) && value > 0 ? rub.format(value) : "—"}</b>
        <small class="${change.cls}">${escapeHtml(change.text)}</small>
      </article>
    `;
  }).join("");
}

function renderPulse(pulse, regime) {
  const root = document.querySelector("#pulse-grid");
  if (!root) return;
  const catalyst = pulse?.catalyst;
  const mover = pulse?.mover;
  const anomaly = pulse?.anomaly;
  const cards = [
    {
      eyebrow: "КАТАЛИЗАТОР",
      title: catalyst ? `${catalyst.ticker} · ${catalyst.title}` : "Нет сильного события",
      note: catalyst
        ? `сценарий ${pct(catalyst.expectedImpactPct)} · факт ${pct(catalyst.marketReactionPct)} · ${REACTION_LABEL[catalyst.reaction] || catalyst.reaction}`
        : "Срочная лента пуста или события слабые.",
    },
    {
      eyebrow: "ДВИЖЕНИЕ",
      title: mover ? `${mover.ticker} ${pct(mover.dayChange)}` : "Нет котировок",
      note: mover ? (mover.name || "Лидеры дня по абсолютному ходу.") : "Вселенная акций недоступна.",
    },
    {
      eyebrow: "АНОМАЛИЯ",
      title: anomaly ? `${anomaly.ticker} · ${anomaly.label}` : "Расхождений нет",
      note: anomaly
        ? `сценарий ${pct(anomaly.expectedImpactPct)} · факт ${pct(anomaly.marketReactionPct)}`
        : "Новости и цена смотрят в одну сторону.",
    },
  ];
  const regimeNote = regime
    ? `<p class="pulse-regime">Режим: ${escapeHtml(regime.label || regime.id)}. Лучше: ${(regime.best || []).join(", ")}. Избегать: ${(regime.avoid || []).join(", ")}.</p>`
    : "";
  root.innerHTML = cards.map((card) => `
    <article class="pulse-card">
      <p class="eyebrow">${escapeHtml(card.eyebrow)}</p>
      <h3>${escapeHtml(card.title)}</h3>
      <p>${escapeHtml(card.note)}</p>
    </article>
  `).join("") + regimeNote;
}

function renderDelta(delta) {
  const root = document.querySelector("#delta-list");
  if (!root) return;
  if (!delta) {
    root.innerHTML = emptyBlock("Сравнение со прошлым снимком появится после второго обновления.");
    return;
  }
  const lines = [];
  if (delta.previousAt) {
    lines.push(`Предыдущий снимок: ${freshnessLabel(delta.previousAt)}. IMOEX ${pct(delta.imoexFrom)} → ${pct(delta.imoexTo)}.`);
  }
  (delta.newSignals || []).forEach((item) => {
    lines.push(`Новый сигнал: ${item.ticker} · ${item.title}`);
  });
  (delta.flips || []).forEach((item) => {
    lines.push(`Смена действия: ${item.ticker} ${item.from} → ${item.to}`);
  });
  (delta.confidenceChanges || []).forEach((item) => {
    lines.push(`Уверенность ${item.ticker}: ${item.from}% → ${item.to}%`);
  });
  (delta.enteredTop10 || []).forEach((ticker) => lines.push(`Вошёл в топ-10: ${ticker}`));
  (delta.leftTop10 || []).forEach((ticker) => lines.push(`Вышел из топ-10: ${ticker}`));
  root.innerHTML = lines.length
    ? lines.map((line) => `<p>${escapeHtml(line)}</p>`).join("")
    : emptyBlock("С прошлого снимка состав сигналов не изменился.");
}

function renderDrivers(drivers) {
  const root = document.querySelector("#driver-list");
  if (!root) return;
  if (!drivers?.length) {
    root.innerHTML = emptyBlock("Явной цепочки «новость → бумага → сектор» сейчас нет.");
    return;
  }
  root.innerHTML = drivers.map((item) => `
    <article class="driver-card">
      <h3>${escapeHtml(item.title)}</h3>
      <p>${escapeHtml(item.note || "")}</p>
      <div class="driver-sides">
        ${(item.positive || []).length ? `<span class="up">в плюс: ${(item.positive || []).map(escapeHtml).join(", ")}</span>` : ""}
        ${(item.negative || []).length ? `<span class="down">в минус: ${(item.negative || []).map(escapeHtml).join(", ")}</span>` : ""}
      </div>
    </article>
  `).join("");
}

function renderCatalysts(items) {
  const root = document.querySelector("#catalyst-list");
  if (!root) return;
  if (!items?.length) {
    root.innerHTML = emptyBlock("Нет событий, по которым можно сравнить сценарий и фактический ход.");
    return;
  }
  root.innerHTML = items.map((item) => `
    <article class="urgent-card">
      <span class="action ${(item.action || "BUY").toLowerCase()}">${item.action === "SELL" ? "СНИЗИТЬ" : "СМОТРЕТЬ"}</span>
      <div class="urgent-copy">
        <h3>${escapeHtml(item.ticker)} · ${escapeHtml(item.title)}</h3>
        <p>${escapeHtml(item.summary || "")}</p>
        <div class="signal-impact">
          <span>сценарий ${pct(item.expectedImpactPct)}</span>
          <span>факт ${pct(item.marketReactionPct)}</span>
          <span>${escapeHtml(REACTION_LABEL[item.reaction] || item.reaction)}</span>
          ${item.horizon ? `<span>${escapeHtml(item.horizon)}</span>` : ""}
          ${item.official ? "<span>официальный источник</span>" : ""}
        </div>
      </div>
      <div class="urgent-source">
        <b>${escapeHtml(String(item.strength))}</b>
        <a href="${escapeHtml(item.source?.url || "#")}" target="_blank" rel="noreferrer">${escapeHtml(item.source?.publisher || "источник")} ↗</a>
      </div>
    </article>
  `).join("");
}

function renderAnomalies(items) {
  const root = document.querySelector("#anomaly-list");
  if (!root) return;
  if (!items?.length) {
    root.innerHTML = emptyBlock("Странной реакции рынка на новости сейчас не видно.");
    return;
  }
  root.innerHTML = items.map((item) => `
    <article class="urgent-card">
      <span class="action ${item.kind === "anomaly_up" || item.kind === "underreaction" ? "buy" : "sell"}">${escapeHtml(String(item.kind || "mover").replace(/_/g, " "))}</span>
      <div class="urgent-copy">
        <h3>${escapeHtml(item.ticker)} · ${escapeHtml(item.title)}</h3>
        <p>${escapeHtml(item.label || "")}</p>
        <div class="signal-impact">
          <span>сценарий ${pct(item.expectedImpactPct)}</span>
          <span>факт ${pct(item.marketReactionPct)}</span>
        </div>
      </div>
    </article>
  `).join("");
}

function renderSectors(items) {
  const root = document.querySelector("#sector-list");
  if (!root) return;
  if (!items?.length) {
    root.innerHTML = emptyBlock("Сектора появятся после снимка акций.");
    return;
  }
  root.innerHTML = items.map((item) => {
    const change = labeledChange(Number(item.dayChange));
    const members = (item.members || []).slice(0, 4).map((row) => `#${row.secid}`).join(" ");
    return `
      <article class="sector-card">
        <div>
          <b>${escapeHtml(item.label || item.id)}</b>
          <span class="${change.cls}">${escapeHtml(change.text)}</span>
        </div>
        <p>${escapeHtml(item.why || "")}</p>
        <small>${escapeHtml(members)}</small>
      </article>
    `;
  }).join("");
}

function renderAccuracy(stats) {
  const root = document.querySelector("#accuracy-metrics");
  const note = document.querySelector("#accuracy-note");
  if (!root) return;
  if (!stats || !Number.isFinite(stats.n) || stats.n < 8) {
    root.innerHTML = "";
    if (note) {
      note.textContent = stats?.pending
        ? `В очереди ${stats.pending} сигналов. Hit rate покажем после 8 закрытых T+1д.`
        : "Hit rate появится, когда накопится история T+1д.";
    }
    return;
  }
  const values = [
    [`${Math.round(stats.hitRate * 100)}%`, "hit rate T+1д"],
    [pct(stats.avgReturn), "средний ход"],
    [stats.n, "закрытых сигналов"],
    [stats.highConfidenceHitRate != null ? `${Math.round(stats.highConfidenceHitRate * 100)}%` : "—", "высокая уверенность"],
  ];
  root.innerHTML = values.map(([value, label]) => `
    <div class="pipeline-metric"><b>${escapeHtml(String(value))}</b><span>${escapeHtml(label)}</span></div>
  `).join("");
  if (note) note.textContent = "Доля сигналов, у которых цена на следующий день пошла в сторону действия BUY/SELL.";
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
  const change = labeledChange(Number(brief.dayChange));
  valueEl.textContent = rub.format(brief.value);
  changeEl.textContent = change.text;
  changeEl.className = change.cls;
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
  if (type === "stocks") {
    const change = labeledChange(Number(item.dayChange));
    return `<b>${price(item.price)}</b><small>сейчас · ${escapeHtml(change.text)}</small>`;
  }
  if (type === "bonds") return `<b>${rub.format(item.price)}%</b><small>от номинала</small>`;
  const change = labeledChange(Number(item.dayChange));
  return `<b>${price(item.price)}</b><small>${escapeHtml(change.text)}</small>`;
}

function stanceBadge(item, type) {
  if (type !== "stocks" || !item.stance) return "";
  return `<span class="stance ${escapeHtml(item.stance.toLowerCase())}">${escapeHtml(item.stance)}</span>`;
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
          <b>${escapeHtml(item.name)} ${stanceBadge(item, type)}</b>
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
  renderTape(data.marketTape);
  renderMarketBrief(data.marketBrief);
  renderPulse(data.marketPulse, data.marketRegime);
  renderDelta(data.sinceLastUpdate);
  renderDrivers(data.drivers);
  renderCatalysts(data.catalysts);
  renderAnomalies(data.anomalies);
  renderUrgent(data.urgent || []);
  renderScalp(data.scalp || []);
  renderSectors(data.sectors);
  renderAccuracy(data.signalPerformance);
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
