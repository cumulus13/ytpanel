(function () {
  var b = document.getElementById("theme");
  if (!b) return;
  b.addEventListener("click", function () {
    var cur = document.documentElement.dataset.theme ||
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    var next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("ytp-theme", next); } catch (e) {}
  });
})();
