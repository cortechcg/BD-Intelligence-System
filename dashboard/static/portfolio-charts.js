/* Portfolio charts. Chart.js (pinned, SRI-checked) draws the same numbers the
   page's tables print; the tables stay as the text fallback. Every colour is
   read from the stylesheet's tokens so the charts can never drift from the
   palette. Nothing here invents a number: series come from #chart-data,
   which the server built from the same queries as the page. */
(function () {
  "use strict";
  var node = document.getElementById("chart-data");
  if (!node || typeof Chart === "undefined") return;
  var data = JSON.parse(node.textContent);

  var css = getComputedStyle(document.documentElement);
  function tok(name) { return css.getPropertyValue(name).trim(); }
  var forest = tok("--color-forest"), forestMid = tok("--color-forest-mid"), moss = tok("--color-moss");
  var hairline = tok("--color-hairline"), track = tok("--color-track"), brass = tok("--color-brass");
  var ramp = [tok("--ramp-1"), tok("--ramp-2"), tok("--ramp-3"), tok("--ramp-4")];
  var ui = css.getPropertyValue("--ui").trim() || "sans-serif";
  var mono = css.getPropertyValue("--mono").trim() || "monospace";

  Chart.defaults.font.family = ui;
  Chart.defaults.font.size = 13;
  Chart.defaults.color = moss;
  Chart.defaults.animation = false;
  Chart.defaults.plugins.legend.labels.boxWidth = 10;
  Chart.defaults.plugins.legend.labels.boxHeight = 10;
  Chart.defaults.plugins.tooltip.backgroundColor = forest;
  Chart.defaults.plugins.tooltip.titleColor = tok("--on-fill");
  Chart.defaults.plugins.tooltip.bodyColor = tok("--on-fill");
  Chart.defaults.plugins.tooltip.titleFont = { family: ui, weight: "500" };
  Chart.defaults.plugins.tooltip.bodyFont = { family: mono };

  /* Value labels at the end of each bar — the numbers are readable, not a vibe. */
  var valueLabels = {
    id: "valueLabels",
    afterDatasetsDraw: function (chart, _args, opts) {
      var ctx = chart.ctx; ctx.save();
      ctx.font = "500 13px " + ui; ctx.fillStyle = forest; ctx.textBaseline = "middle";
      var horizontal = chart.options.indexAxis === "y";
      chart.data.datasets.forEach(function (ds, di) {
        if (ds.hidden) return;
        var meta = chart.getDatasetMeta(di);
        if (meta.hidden) return;
        meta.data.forEach(function (el, i) {
          var v = ds.data[i];
          if (v === null || v === undefined) return;
          var text = opts && opts.format ? opts.format(v) : String(v);
          if (horizontal) { ctx.textAlign = "left"; ctx.fillText(text, el.x + 8, el.y); }
          else { ctx.textAlign = "center"; ctx.fillText(text, el.x, el.y - 10); }
        });
      });
      ctx.restore();
    }
  };

  var grid = { color: hairline, drawTicks: false };
  var axisTitle = function (text) { return { display: true, text: text, color: moss, font: { size: 12, weight: "500" }, padding: { top: 8 } }; };

  function bars(canvasId, labels, counts, opts) {
    var el = document.getElementById(canvasId);
    if (!el) return;
    var max = Math.max.apply(null, counts.concat([1]));
    new Chart(el, {
      type: "bar",
      data: { labels: labels, datasets: [{ data: counts, backgroundColor: opts.colors || forest, borderRadius: 2, barThickness: 14, maxBarThickness: 18 }] },
      plugins: [valueLabels],
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false,
        layout: { padding: { right: 40, left: 8 } },
        scales: {
          x: { beginAtZero: true, suggestedMax: Math.ceil(max * 1.15), grid: grid, border: { color: hairline },
               ticks: { precision: 0, font: { family: mono, size: 11 } }, title: axisTitle(opts.xTitle) },
          y: { grid: { display: false }, border: { display: false }, ticks: { color: forest, font: { size: 13 }, autoSkip: false,
               callback: function (v) { var l = String(this.getLabelForValue(v)); return l.length > 30 ? l.slice(0, 29) + "…" : l; } } }
        },
        plugins: {
          legend: { display: false },
          title: opts.title ? { display: true, text: opts.title, align: "start", color: forest, font: { size: 13, weight: "500" }, padding: { bottom: 2 } } : { display: false },
          subtitle: opts.subtitle ? { display: true, text: opts.subtitle, align: "start", color: brass, font: { size: 12, weight: "500" }, padding: { bottom: 16 } } : { display: false },
          tooltip: { callbacks: { title: function (items) { return items[0].label; }, label: function (c) { return c.parsed.x + (c.parsed.x === 1 ? " row" : " rows"); } } }
        }
      }
    });
  }

  /* 1. Stage funnel — six stages; agent stages in forest, human stages in the mid green. */
  if (data.stage) {
    var stageColors = data.stage.labels.map(function (_l, i) { return i < data.stage.agent_stages ? forest : forestMid; });
    bars("chart-stage", data.stage.labels, data.stage.counts, {
      colors: stageColors, xTitle: "ledger rows at this stage (n = " + data.stage.ledger_rows + ")"
    });
  }

  /* 2. Spend per day — completed vs stopped runs, stacked. USD, four decimals in tooltips. */
  if (data.spend && data.spend.labels.length) {
    var el = document.getElementById("chart-spend");
    if (el) {
      var usd = function (v) { return v.toFixed(2); };
      new Chart(el, {
        type: "bar",
        data: {
          labels: data.spend.labels,
          datasets: [
            { label: "Runs that completed", data: data.spend.completed, backgroundColor: forest, borderRadius: 2, maxBarThickness: 36, stack: "usd" },
            { label: "Runs that stopped (failed, cancelled, cap)", data: data.spend.stopped, backgroundColor: forestMid, borderRadius: 2, maxBarThickness: 36, stack: "usd" }
          ]
        },
        plugins: [{
          id: "stackTotals",
          afterDatasetsDraw: function (chart) {
            var ctx = chart.ctx; ctx.save(); ctx.font = "500 13px " + ui; ctx.fillStyle = forest; ctx.textAlign = "center"; ctx.textBaseline = "bottom";
            var meta1 = chart.getDatasetMeta(1);
            chart.data.labels.forEach(function (_l, i) {
              var total = (data.spend.completed[i] || 0) + (data.spend.stopped[i] || 0);
              var top = meta1.data[i];
              ctx.fillText(total.toFixed(2) + " USD", top.x, top.y - 6);
            });
            ctx.restore();
          }
        }],
        options: {
          responsive: true, maintainAspectRatio: false,
          layout: { padding: { top: 24 } },
          scales: {
            x: { stacked: true, grid: { display: false }, border: { color: hairline }, ticks: { font: { family: mono, size: 11 } }, title: axisTitle("day requested (UTC)") },
            y: { stacked: true, beginAtZero: true, grid: grid, border: { display: false }, ticks: { font: { family: mono, size: 11 }, callback: function (v) { return v.toFixed(2); } }, title: axisTitle("USD") }
          },
          plugins: {
            legend: { position: "top", align: "start", labels: { color: forest } },
            tooltip: { callbacks: {
              label: function (c) { return c.dataset.label + ": " + c.parsed.y.toFixed(4) + " USD"; },
              afterBody: function (items) { var i = items[0].dataIndex; return data.spend.runs[i] + " run" + (data.spend.runs[i] === 1 ? "" : "s") + (data.spend.unknown[i] ? ", " + data.spend.unknown[i] + " with no recorded spend" : ""); }
            } }
          }
        }
      });
    }
  }

  /* 3. Market digest — the coverage clause is the chart's subtitle, in brass,
        because it is the one thing a reader must not miss: rows without a
        label are UNKNOWN, not zero. */
  [["chart-themes-30", data.themes_30, "opportunities carrying this theme, last 30 days"],
   ["chart-themes-90", data.themes_90, "opportunities carrying this theme, last 90 days"],
   ["chart-geo-30", data.geography_30, "opportunities naming this location, last 30 days"]].forEach(function (spec) {
    var w = spec[1];
    if (!w) return;
    bars(spec[0], w.labels, w.counts, {
      colors: ramp[1], xTitle: spec[2],
      title: w.sample + (w.total_labels > w.shown ? " · top " + w.shown + " of " + w.total_labels + " labels" : ""),
      subtitle: w.coverage
    });
  });
})();
