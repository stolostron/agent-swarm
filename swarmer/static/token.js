function copyFromElement(elementId, btn) {
  const el = document.getElementById(elementId);
  if (!el || !btn) return;
  const text = el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' ? el.value : el.innerText;
  navigator.clipboard.writeText(text).then(() => {
    const originalText = btn.innerText;
    btn.innerText = '✓ Copied!';
    setTimeout(() => {
      btn.innerText = originalText;
    }, 2000);
  }).catch(err => {
    console.error('Failed to copy: ', err);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  const tokenBtn = document.getElementById('copy-token-btn');
  if (tokenBtn) {
    tokenBtn.addEventListener('click', () => copyFromElement('bearer-token-text', tokenBtn));
  }

  const jsonBtn = document.getElementById('copy-json-btn');
  if (jsonBtn) {
    jsonBtn.addEventListener('click', () => copyFromElement('mcp-json-text', jsonBtn));
  }

  const makeBtn = document.getElementById('copy-make-btn');
  if (makeBtn) {
    makeBtn.addEventListener('click', () => copyFromElement('make-cmd-text', makeBtn));
  }
});
