// Prefix day headings with "Today" / "Tomorrow". Done in the browser, not at
// build time, because the site is static: a page built late yesterday would
// otherwise call yesterday "Today". Day headings carry their ISO date as id.
(function () {
  // en-CA formats as YYYY-MM-DD; dates are DC-local, like the listings.
  var today = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit'
  }).format(new Date());
  var p = today.split('-').map(Number);
  var tomorrow = new Date(Date.UTC(p[0], p[1] - 1, p[2] + 1)).toISOString().slice(0, 10);
  var labels = {};
  labels[today] = 'Today';
  labels[tomorrow] = 'Tomorrow';

  document.querySelectorAll('h2.day-heading[id]').forEach(function (h) {
    var label = labels[h.id];
    if (!label) return;
    var span = document.createElement('span');
    span.className = 'day-relative';
    span.textContent = label + ' · ';
    h.insertBefore(span, h.firstChild);
  });
})();
