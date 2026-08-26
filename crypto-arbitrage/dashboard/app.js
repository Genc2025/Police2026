const $ = (id) => document.getElementById(id);

const state = {
  asset: "USDT",
  busy: false,
};

function fmtNumber(value, digits = 4) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

function fmtMoney(value, asset = state.asset, digits = 4) {
  const number = Number(value);
  if (!Number.isFinite(number)) return `— ${asset}`;
  return `${number.toFixed(digits)} ${asset}`;
}

function fmtPct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number >= 0 ? "+" : ""}${number.toFixed(4)}%`;
}

function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function pnlClass(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "";
  return number > 0 ? "positive" : "negative";
}

async function getJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function renderSummary(summary) {
  state.asset = summary.capital_asset || "USDT";
  $("modeBadge").textContent = summary.mode || "PAPER";
  $("modeBadge").className = `badge ${summary.mode === "LIVE" ? "badge-live" : "badge-paper"}`;
  $("paperBalance").textContent = fmtMoney(summary.paper_balance, state.asset, 2);
  $("startingCapital").textContent = `Start: ${fmtMoney(summary.starting_capital, state.asset, 2)}`;
  $("netPnl").textContent = fmtMoney(summary.net_pnl, state.asset, 4);
  $("netPnl").className = pnlClass(summary.net_pnl);
  $("pnlStatus").textContent = Number(summary.net_pnl) > 0 ? "Në fitim" : Number(summary.net_pnl) < 0 ? "Në humbje" : "Pa trade";
  $("paperTrades").textContent = fmtNumber(summary.paper_trades, 0);
  $("wins").textContent = fmtNumber(summary.wins, 0);
  $("losses").textContent = fmtNumber(summary.losses, 0);
  $("totalScans").textContent = fmtNumber(summary.total_scans, 0);
  $("candidateCount").textContent = fmtNumber(summary.total_candidates, 0);
  $("dbName").textContent = `DB: ${summary.scanner_db || "—"}`;
  $("lastUpdated").textContent = summary.latest_observed_at ? `Market: ${fmtTime(summary.latest_observed_at)}` : "Pa data";
}

function renderMarket(rows) {
  const body = $("marketRows");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">Ende nuk ka market snapshots.</td></tr>';
    return;
  }
  body.innerHTML = rows.map((row) => {
    const bid = Number(row.bid);
    const ask = Number(row.ask);
    const spread = bid > 0 ? ((ask / bid) - 1) * 100 : 0;
    return `
      <tr>
        <td><b>${row.pair}</b></td>
        <td class="exchange">${row.exchange}</td>
        <td class="num">${fmtNumber(row.bid, 8)}</td>
        <td class="num">${fmtNumber(row.ask, 8)}</td>
        <td class="num">${fmtPct(spread)}</td>
        <td class="num">${fmtNumber(row.latency_ms, 0)} ms</td>
      </tr>`;
  }).join("");
}

function renderOpportunities(rows) {
  const body = $("opportunityRows");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">Ende nuk ka routes të analizuara.</td></tr>';
    return;
  }
  body.innerHTML = rows.slice(0, 40).map((row) => {
    const qualifies = Number(row.qualifies) === 1;
    const netClass = pnlClass(row.net_profit_quote);
    return `
      <tr>
        <td>${fmtTime(row.observed_at)}</td>
        <td><b>${row.pair}</b></td>
        <td><div class="route"><b>${row.buy_exchange}</b><span>@</span>${fmtNumber(row.buy_ask, 8)}</div></td>
        <td><div class="route"><b>${row.sell_exchange}</b><span>@</span>${fmtNumber(row.sell_bid, 8)}</div></td>
        <td class="num">${fmtPct(row.gross_spread_pct)}</td>
        <td class="num">${fmtPct(row.estimated_cost_pct)}</td>
        <td class="num ${netClass}">${fmtMoney(row.net_profit_quote, row.quote_asset || state.asset, 4)}</td>
        <td><span class="status-chip ${qualifies ? "status-trade" : "status-no"}">${qualifies ? "PAPER TRADE" : "NO TRADE"}</span></td>
      </tr>`;
  }).join("");
}

function renderTrades(rows) {
  const body = $("tradeRows");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="10" class="empty">Ende nuk ka paper trade që ka kaluar pragun. Kjo është normale kur spread-i nuk mbulon fee-t.</td></tr>';
    return;
  }
  body.innerHTML = rows.map((row) => `
    <tr>
      <td>${fmtTime(row.observed_at)}</td>
      <td class="mode-paper">${row.mode || "PAPER"}</td>
      <td><b>${row.pair}</b></td>
      <td class="exchange">${row.buy_exchange}</td>
      <td class="num">${fmtNumber(row.buy_ask, 8)}</td>
      <td class="exchange">${row.sell_exchange}</td>
      <td class="num">${fmtNumber(row.sell_bid, 8)}</td>
      <td class="num">${fmtMoney(row.capital_quote, row.quote_asset || state.asset, 2)}</td>
      <td class="num">${fmtPct(row.gross_spread_pct)}</td>
      <td class="num ${pnlClass(row.net_profit_quote)}"><b>${fmtMoney(row.net_profit_quote, row.quote_asset || state.asset, 4)}</b></td>
    </tr>`).join("");
}

async function refresh() {
  if (state.busy) return;
  state.busy = true;
  const badge = $("connectionBadge");
  try {
    const [summary, market, opportunities, trades] = await Promise.all([
      getJson("/api/summary"),
      getJson("/api/market?limit=100"),
      getJson("/api/opportunities?limit=80"),
      getJson("/api/trades?limit=200"),
    ]);
    renderSummary(summary);
    renderMarket(market);
    renderOpportunities(opportunities);
    renderTrades(trades);
    badge.textContent = summary.database_ready ? "CONNECTED" : "WAITING FOR DB";
    badge.className = `badge ${summary.database_ready ? "badge-ok" : "badge-wait"}`;
  } catch (error) {
    console.error(error);
    badge.textContent = "OFFLINE";
    badge.className = "badge badge-error";
  } finally {
    state.busy = false;
  }
}

$("refreshBtn").addEventListener("click", refresh);
refresh();
setInterval(refresh, 2000);
