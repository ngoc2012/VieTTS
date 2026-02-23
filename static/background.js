// Opens the popup as a standalone window that stays open on focus loss.
let windowId = null;

chrome.action.onClicked.addListener(() => {
  // If window already exists, focus it instead of opening a new one
  if (windowId !== null) {
    chrome.windows.get(windowId, (win) => {
      if (chrome.runtime.lastError || !win) {
        windowId = null;
        openWindow();
      } else {
        chrome.windows.update(windowId, { focused: true });
      }
    });
  } else {
    openWindow();
  }
});

function openWindow() {
  chrome.windows.create({
    url: chrome.runtime.getURL('popup.html'),
    type: 'popup',
    width: 540,
    height: 700,
    focused: true,
  }, (win) => {
    windowId = win.id;
  });
}

chrome.windows.onRemoved.addListener((id) => {
  if (id === windowId) windowId = null;
});
