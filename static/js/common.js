// Smart Doorbell AI - Common Utilities

window.api = async function api(url, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  // If sending FormData or already stringified, adjust
  const fetchOptions = { ...options, headers };
  if (options.body && typeof options.body !== 'string' && !(options.body instanceof FormData)) {
    fetchOptions.body = JSON.stringify(options.body);
  }

  let response;
  try {
    response = await fetch(url, fetchOptions);
  } catch (networkErr) {
    const err = new Error('Không thể kết nối đến máy chủ. Kiểm tra lại kết nối mạng.');
    err.status = 0;
    err.original = networkErr;
    throw err;
  }

  const body = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body?.error || body?.reason || `Yêu cầu thất bại (Mã lỗi ${response.status})`;
    const err = new Error(message);
    err.status = response.status;
    err.body = body;
    throw err;
  }
  return body;
};

window.toast = function toast(message, kind = 'ok', duration = 3600) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = message;
  el.className = `show ${kind}`;
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => {
    el.className = '';
  }, duration);
};

window.escapeHtml = function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;'
  }[c]));
};

window.formatDate = function formatDate(val) {
  if (!val) return '—';
  try {
    const text = String(val).trim();
    // SQLite CURRENT_TIMESTAMP is UTC but has no explicit timezone suffix.
    const normalized = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(text)
      ? `${text.replace(' ', 'T')}Z`
      : text;
    const d = new Date(normalized);
    if (isNaN(d.getTime())) return String(val);
    const pad = n => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())} ${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`;
  } catch (_) {
    return String(val);
  }
};

function setPageInert(inert) {
  const header = document.querySelector('.topbar');
  const main = document.getElementById('mainContent');
  if (header) header.inert = inert;
  if (main) main.inert = inert;
}

function keepFocusInsideModal(event, modal) {
  if (event.key !== 'Tab') return;
  const focusable = Array.from(modal.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )).filter(element => element.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

// Accessible Modal Dialog Confirmation
window.confirmDialog = function confirmDialog({
  title = 'Xác nhận',
  message = 'Bạn có chắc chắn muốn thực hiện thao tác này?',
  confirmText = 'Xác nhận',
  cancelText = 'Hủy bỏ',
  danger = false
} = {}) {
  return new Promise((resolve) => {
    const modal = document.getElementById('confirmModal');
    const titleEl = document.getElementById('confirmTitle');
    const messageEl = document.getElementById('confirmMessage');
    const okBtn = document.getElementById('confirmOk');
    const cancelBtn = document.getElementById('confirmCancel');
    const closeBtn = document.getElementById('confirmClose');

    if (!modal) {
      resolve(window.confirm(message));
      return;
    }

    const prevActive = document.activeElement;

    titleEl.textContent = title;
    messageEl.innerHTML = typeof message === 'string' ? `<p>${escapeHtml(message)}</p>` : '';
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;

    if (danger) {
      okBtn.className = 'btn btn-danger';
    } else {
      okBtn.className = 'btn btn-primary';
    }

    const cleanup = () => {
      modal.classList.remove('open');
      modal.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('modal-open');
      setPageInert(false);
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      closeBtn.removeEventListener('click', onCancel);
      window.removeEventListener('keydown', onKeyDown);
      if (prevActive && typeof prevActive.focus === 'function') {
        prevActive.focus();
      }
    };

    const onOk = () => {
      cleanup();
      resolve(true);
    };

    const onCancel = () => {
      cleanup();
      resolve(false);
    };

    const onKeyDown = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
      } else {
        keepFocusInsideModal(e, modal);
      }
    };

    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    closeBtn.addEventListener('click', onCancel);
    window.addEventListener('keydown', onKeyDown);

    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
    setPageInert(true);
    okBtn.focus();
  });
};

// Accessible Lightbox Viewer
window.openLightbox = function openLightbox({
  title = 'Ảnh sự kiện',
  subtitle = '',
  imageUrl = '',
  downloadUrl = '',
  metaItems = []
} = {}) {
  const modal = document.getElementById('lightboxModal');
  const titleEl = document.getElementById('lightboxTitle');
  const subtitleEl = document.getElementById('lightboxSubtitle');
  const imgEl = document.getElementById('lightboxImg');
  const metaEl = document.getElementById('lightboxMeta');
  const downloadLink = document.getElementById('lightboxDownload');
  const doneBtn = document.getElementById('lightboxDone');
  const closeBtn = document.getElementById('lightboxClose');

  if (!modal) return;
  const prevActive = document.activeElement;

  titleEl.textContent = title;
  subtitleEl.textContent = subtitle;
  imgEl.src = imageUrl;
  downloadLink.href = downloadUrl || imageUrl;

  metaEl.innerHTML = metaItems.length ? metaItems.map(item => `
    <div class="lightbox-meta-row">
      <span class="lightbox-meta-label">${escapeHtml(item.label)}</span>
      <span class="lightbox-meta-value ${item.className || ''}">${escapeHtml(item.value)}</span>
    </div>
  `).join('') : '';

  const cleanup = () => {
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('modal-open');
    setPageInert(false);
    imgEl.src = '';
    doneBtn.removeEventListener('click', onClose);
    closeBtn.removeEventListener('click', onClose);
    window.removeEventListener('keydown', onKeyDown);
    if (prevActive && typeof prevActive.focus === 'function') {
      prevActive.focus();
    }
  };

  const onClose = () => cleanup();
  const onKeyDown = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      cleanup();
    } else {
      keepFocusInsideModal(e, modal);
    }
  };

  doneBtn.addEventListener('click', onClose);
  closeBtn.addEventListener('click', onClose);
  window.addEventListener('keydown', onKeyDown);

  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
  setPageInert(true);
  doneBtn.focus();
};

// Realtime Clock Updater
(function initTopClock() {
  const clockEl = document.getElementById('topClock');
  if (!clockEl) return;
  const update = () => {
    const d = new Date();
    const pad = n => String(n).padStart(2, '0');
    clockEl.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
  update();
  setInterval(update, 1000);
})();

