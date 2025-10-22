// global-search.js
// Finds #search and #searchButton on the current page.
// Clicking Search (or pressing Enter) navigates to courses.html?q=...
(function () {
  const input = document.getElementById('search');
  const btn   = document.getElementById('searchButton');
  if (!input || !btn) return;

  function go() {
    const q = encodeURIComponent(input.value.trim());
    const dest = `courses.html${q ? `?q=${q}` : ''}`;
    window.location.href = dest;
  }

  btn.addEventListener('click', go);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') go();
  });
})();

/*<!-- 🔎 Paste inside your page header/toolbar -->
<div class="toolbar">
  <div class="search">
    <span>🔎</span>
    <input id="search" placeholder="Search course or requirements" />
  </div>
  <button class="btn btn-primary" id="searchButton">Search</button>
</div>

<!-- 🚦 Place before closing </body> -->
<script src="global-search.js"></script>
*/