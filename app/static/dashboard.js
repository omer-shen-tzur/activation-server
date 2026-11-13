"use strict";

// Model names come from devices and are untrusted. They are only ever put on
// the page with textContent and new Option(), and Chart.js draws them on a
// canvas. Never build markup from strings here.

const $ = (id) => document.getElementById(id);
const charts = {};
let latestLoad = 0;

class ApiError extends Error {}

async function errorMessage(response) {
  // 422 from our own checks: {"detail": "text"}. 422 from FastAPI's query
  // validation: {"detail": [{"loc": [...], "msg": "text"}, ...]}.
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail.map((e) => `${e.loc.join(".")}: ${e.msg}`).join("; ");
    }
  } catch (e) {
    // not JSON: fall through
  }
  return "Invalid request (422)";
}

async function fetchStats(params) {
  const query = new URLSearchParams(params).toString();
  let response;
  try {
    // Same origin: the browser adds the Basic auth credentials itself.
    response = await fetch("/api/v1/stats" + (query ? "?" + query : ""));
  } catch (e) {
    throw new ApiError("Cannot reach the server. Check the connection and press Apply.");
  }
  if (response.status === 401) throw new ApiError("Session expired, reload the page");
  if (response.status === 422) throw new ApiError(await errorMessage(response));
  if (!response.ok) throw new ApiError(`Server error (${response.status})`);
  try {
    return await response.json();
  } catch (e) {
    throw new ApiError("The server sent an unreadable answer.");
  }
}

function fillModelOptions(byModel, selected) {
  const select = $("model");
  select.replaceChildren(new Option("All models", ""));
  const names = byModel.map((m) => m.model);
  // Keep the selection even if this range has no activations for the model.
  if (selected && !names.includes(selected)) names.push(selected);
  for (const name of names) select.add(new Option(name, name));
  select.value = selected;
}

function drawChart(key, canvasId, config) {
  if (charts[key]) charts[key].destroy();
  charts[key] = new Chart($(canvasId), config);
}

function render(all, filtered) {
  const days = filtered.daily.map((d) => d.day);

  $("range-label").textContent = filtered.range.model
    ? `In range (${filtered.range.model})`
    : "In range";
  $("range-activated").textContent = filtered.totals_in_range.activated;
  $("range-duplicates").textContent = filtered.totals_in_range.duplicates;
  $("all-activated").textContent = all.all_time.activated;
  $("all-duplicates").textContent = all.all_time.duplicates;

  drawChart("daily", "chart-daily", {
    type: "line",
    data: {
      labels: days,
      datasets: [
        { label: "Activated", data: filtered.daily.map((d) => d.activated), borderColor: "#1f77b4" },
        { label: "Repeat requests (not counted)", data: filtered.daily.map((d) => d.duplicates), borderColor: "#ff7f0e" },
      ],
    },
    options: { maintainAspectRatio: false, scales: { y: { beginAtZero: true } } },
  });

  // Starts from the total before the range, so the line is the running
  // all-time count, not a count that restarts at 0 on the first day.
  let running = filtered.activated_before_range;
  const cumulative = filtered.daily.map((d) => (running += d.activated));
  drawChart("cumulative", "chart-cumulative", {
    type: "line",
    data: { labels: days, datasets: [{ label: "Cumulative activated", data: cumulative, borderColor: "#2ca02c" }] },
    options: { maintainAspectRatio: false },
  });

  drawChart("models", "chart-models", {
    type: "bar",
    data: {
      labels: all.by_model.map((m) => m.model),
      datasets: [{ label: "Activated", data: all.by_model.map((m) => m.activated), backgroundColor: "#1f77b4" }],
    },
    options: { maintainAspectRatio: false, scales: { y: { beginAtZero: true } } },
  });
}

async function load() {
  const thisLoad = ++latestLoad;
  $("error").textContent = "";
  try {
    if (typeof Chart === "undefined") {
      throw new ApiError("Chart.js did not load (the page needs internet access to cdn.jsdelivr.net).");
    }
    // Empty date inputs mean "server default": the first load sends none.
    const params = {};
    if ($("from").value) params.from = $("from").value;
    if ($("to").value) params.to = $("to").value;
    const selected = $("model").value;

    const all = await fetchStats(params);
    let filtered = all;
    if (selected) {
      // Same dates as the unfiltered answer, even if the server picked them.
      filtered = await fetchStats({ from: all.range.from, to: all.range.to, model: selected });
    }
    if (thisLoad !== latestLoad) return; // a newer Apply is in flight

    $("from").value = all.range.from;
    $("to").value = all.range.to;
    fillModelOptions(all.by_model, selected);
    render(all, filtered);
  } catch (e) {
    if (thisLoad !== latestLoad) return;
    $("error").textContent = e instanceof ApiError ? e.message : "Unexpected error: " + e.message;
  }
}

$("apply").addEventListener("click", load);
load();
