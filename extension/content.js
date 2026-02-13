// Each injection toggles inspect mode on/off on the page.
(function () {
  function deactivate() {
    window.__vieneuInspectActive = false;
    document.querySelectorAll('.__vieneu-highlight').forEach(el =>
      el.classList.remove('__vieneu-highlight'));
    document.removeEventListener('mouseover', window.__vieneuOver, true);
    document.removeEventListener('mouseout', window.__vieneuOut, true);
    document.removeEventListener('click', window.__vieneuClick, true);
    const style = document.getElementById('__vieneu-inspect-css');
    if (style) style.remove();
  }

  // Toggle off if already active
  if (window.__vieneuInspectActive) {
    deactivate();
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

  window.__vieneuClick = function (e) {
    e.preventDefault();
    e.stopPropagation();
    const target = e.target;
    if (!target.parentElement) return;
    const tag = target.tagName;
    const texts = [];
    for (const sibling of target.parentElement.children) {
      if (sibling.tagName === tag) {
        const t = sibling.innerText.trim();
        if (t) texts.push(t);
      }
    }
    if (texts.length > 0) {
      chrome.storage.local.get('pendingTexts', (result) => {
        const pending = result.pendingTexts || [];
        pending.push(texts.join('\n'));
        chrome.storage.local.set({ pendingTexts: pending });
      });
    }
    deactivate();
  };

  document.addEventListener('mouseover', window.__vieneuOver, true);
  document.addEventListener('mouseout', window.__vieneuOut, true);
  document.addEventListener('click', window.__vieneuClick, true);
})();
