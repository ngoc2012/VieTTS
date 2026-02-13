// Each injection toggles inspect mode on/off on the page.
(function () {
  // Toggle off if already active
  if (window.__vieneuInspectActive) {
    window.__vieneuInspectActive = false;
    document.querySelectorAll('.__vieneu-highlight').forEach(el =>
      el.classList.remove('__vieneu-highlight'));
    document.removeEventListener('mouseover', window.__vieneuOver, true);
    document.removeEventListener('mouseout', window.__vieneuOut, true);
    const style = document.getElementById('__vieneu-inspect-css');
    if (style) style.remove();
    return;
  }

  // Toggle on
  window.__vieneuInspectActive = true;

  // Inject style
  const style = document.createElement('style');
  style.id = '__vieneu-inspect-css';
  style.textContent = '.__vieneu-highlight { outline: 2px solid red !important; }';
  document.head.appendChild(style);

  function clearHighlights() {
    document.querySelectorAll('.__vieneu-highlight').forEach(el =>
      el.classList.remove('__vieneu-highlight'));
  }

  window.__vieneuOver = function (e) {
    clearHighlights();
    const target = e.target;
    if (!target.parentElement) return;
    const tag = target.tagName;
    for (const sibling of target.parentElement.children) {
      if (sibling.tagName === tag) sibling.classList.add('__vieneu-highlight');
    }
  };

  window.__vieneuOut = function (e) {
    const related = e.relatedTarget;
    if (related && e.target.parentElement && e.target.parentElement.contains(related)) return;
    clearHighlights();
  };

  document.addEventListener('mouseover', window.__vieneuOver, true);
  document.addEventListener('mouseout', window.__vieneuOut, true);
})();
