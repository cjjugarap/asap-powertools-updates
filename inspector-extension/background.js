// When the extension icon is clicked, inject the inspector into the active tab.
chrome.action.onClicked.addListener((tab) => {
  chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["inspector.js"]
  });
});
