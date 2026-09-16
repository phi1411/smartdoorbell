// Smart Doorbell AI - History Logic

const EVENT_TYPE_MAP = {
  DOORBELL: { label: 'Bấm chuông', icon: '🔔', badgeClass: 'badge-success' },
  UNKNOWN_LOITERING: { label: 'Người chưa xác định lưu lại thời gian dài', icon: '⚠️', badgeClass: 'badge-warn' },
  SPOOF_ATTEMPT: { label: 'Cảnh báo giả mạo', icon: '🚨', badgeClass: 'badge-danger' }
};

const LIVENESS_MAP = {
  real: { label: 'REAL', badgeClass: 'badge-success' },
  pending: { label: 'PENDING', badgeClass: 'badge-warn' },
  fake: { label: 'FAKE', badgeClass: 'badge-danger' },
  disabled: { label: 'DISABLED', badgeClass: 'badge-neutral' }
};

let currentEvents = [];

// Tải lịch sử sự kiện dựa trên bộ lọc
async function loadHistory() {
  const form = document.getElementById('filters');
  const tableBody = document.getElementById('historyRows');
  const cardsView = document.getElementById('historyCards');
  const exportLink = document.getElementById('exportLink');

  // Hiển thị skeleton loader khi đang tải
  tableBody.innerHTML = `
    <tr class="skeleton-row"><td colspan="7"></td></tr>
    <tr class="skeleton-row"><td colspan="7"></td></tr>
    <tr class="skeleton-row"><td colspan="7"></td></tr>
  `;
  if (cardsView) cardsView.innerHTML = '';

  const formData = new FormData(form);
  const params = new URLSearchParams();

  for (const [key, val] of formData.entries()) {
    if (val && String(val).trim()) {
      params.set(key, String(val).trim());
    }
  }

  // Luôn đồng bộ liên kết xuất file CSV với bộ lọc hiện tại
  const queryString = params.toString();
  exportLink.href = `/api/events?${queryString ? queryString + '&' : ''}format=csv`;

  try {
    const res = await api(`/api/events?${queryString}`);
    currentEvents = res.items || [];

    if (!currentEvents.length) {
      const emptyHtml = `
        <div class="empty-state">
          <div class="empty-state-icon">📋</div>
          <div class="empty-state-title">Không tìm thấy sự kiện nào</div>
          <p class="empty-state-desc">Hãy thử thay đổi tiêu chí lọc hoặc chọn ngày khác để xem nhật ký.</p>
        </div>
      `;
      tableBody.innerHTML = `<tr><td colspan="7">${emptyHtml}</td></tr>`;
      if (cardsView) cardsView.innerHTML = emptyHtml;
      return;
    }

    // 1. Render Desktop Table Rows
    tableBody.innerHTML = currentEvents.map(e => {
      const typeInfo = EVENT_TYPE_MAP[e.event_type] || { label: e.event_type, icon: '📌', badgeClass: 'badge-neutral' };
      const liveKey = (e.liveness_status || '').toLowerCase();
      const liveInfo = LIVENESS_MAP[liveKey] || { label: e.liveness_status || '—', badgeClass: 'badge-neutral' };

      const nameHtml = e.recognized_name
        ? `<strong class="text-emerald">👤 ${escapeHtml(e.recognized_name)}</strong>`
        : (e.recognition_status === 'unknown'
            ? `<span class="text-amber">Người chưa xác định</span>`
            : (e.recognition_status === 'multiple_faces' ? `<span class="text-amber">Nhiều khuôn mặt</span>` : `<span class="text-dim">Không rõ</span>`));

      const confidenceText = (e.confidence && e.confidence > 0) ? `${Math.round(e.confidence * 100)}%` : '—';
      const tgText = e.telegram_sent
        ? `<span class="text-emerald font-mono">✓ Đã gửi</span>`
        : `<span class="text-muted">Lưu nội bộ</span>`;

      const thumbHtml = e.image_path ? `
        <button type="button" class="event-thumb-btn" onclick="openEventLightbox(${e.id})" title="Xem ảnh phóng to" aria-label="Xem ảnh sự kiện #${e.id}">
          <img src="/api/events/${e.id}/image" alt="Ảnh sự kiện" class="event-thumb-img" loading="lazy">
        </button>
      ` : '<span class="text-dim">—</span>';

      return `
        <tr data-id="${e.id}">
          <td class="font-mono text-muted">${formatDate(e.created_at)}</td>
          <td><span class="badge ${typeInfo.badgeClass}">${typeInfo.icon} ${escapeHtml(typeInfo.label)}</span></td>
          <td>${nameHtml}</td>
          <td class="font-mono">${confidenceText}</td>
          <td><span class="badge ${liveInfo.badgeClass}">${escapeHtml(liveInfo.label)}</span></td>
          <td>${tgText}</td>
          <td>${thumbHtml}</td>
        </tr>
      `;
    }).join('');

    // 2. Render Mobile Cards View
    if (cardsView) {
      cardsView.innerHTML = currentEvents.map(e => {
        const typeInfo = EVENT_TYPE_MAP[e.event_type] || { label: e.event_type, icon: '📌', badgeClass: 'badge-neutral' };
        const liveKey = (e.liveness_status || '').toLowerCase();
        const liveInfo = LIVENESS_MAP[liveKey] || { label: e.liveness_status || '—', badgeClass: 'badge-neutral' };
        const nameText = e.recognized_name || (e.recognition_status === 'unknown' ? 'Người chưa xác định' : 'Không rõ');

        const thumbBtn = e.image_path ? `
          <button type="button" class="btn btn-secondary btn-sm" onclick="openEventLightbox(${e.id})">
            Xem ảnh
          </button>
        ` : '';

        return `
          <div class="history-card-item">
            <div class="history-card-header">
              <span class="badge ${typeInfo.badgeClass}">${typeInfo.icon} ${escapeHtml(typeInfo.label)}</span>
              <span class="font-mono text-muted" style="font-size: 0.78rem;">${formatDate(e.created_at)}</span>
            </div>
            <div class="history-card-body">
              <div><span class="text-dim">Đối tượng:</span> <strong>${escapeHtml(nameText)}</strong></div>
              <div><span class="text-dim">Liveness:</span> <span class="badge ${liveInfo.badgeClass}">${escapeHtml(liveInfo.label)}</span></div>
              <div><span class="text-dim">Telegram:</span> ${e.telegram_sent ? '<span class="text-emerald">✓ Đã gửi</span>' : '<span class="text-muted">Chưa gửi</span>'}</div>
              <div><span class="text-dim">Mã sự kiện:</span> <span class="font-mono">#${e.id}</span></div>
            </div>
            ${thumbBtn ? `<div class="history-card-thumb">${thumbBtn}</div>` : ''}
          </div>
        `;
      }).join('');
    }
  } catch (err) {
    tableBody.innerHTML = `
      <tr><td colspan="7">
        <div class="empty-state">
          <div class="empty-state-title text-red">Lỗi tải dữ liệu lịch sử</div>
          <p class="empty-state-desc">${escapeHtml(err.message)}</p>
          <button type="button" class="btn btn-secondary btn-sm" onclick="loadHistory()">Thử lại</button>
        </div>
      </td></tr>
    `;
  }
}

// Xem ảnh sự kiện trong Lightbox Modal
window.openEventLightbox = async function openEventLightbox(eventId) {
  try {
    const event = currentEvents.find(e => e.id === eventId) || await api(`/api/events/${eventId}`);
    if (!event) return;

    const typeInfo = EVENT_TYPE_MAP[event.event_type] || { label: event.event_type };
    const metaItems = [
      { label: 'Mã sự kiện', value: `#${event.id}` },
      { label: 'Loại sự kiện', value: `${typeInfo.label} (${event.event_type})` },
      { label: 'Thời gian ghi nhận', value: formatDate(event.created_at) },
      { label: 'Đối tượng nhận diện', value: event.recognized_name || (event.recognition_status ? event.recognition_status.toUpperCase() : 'Chưa xác định') },
      { label: 'Trạng thái Liveness', value: (event.liveness_status || '—').toUpperCase() },
      { label: 'Độ tin cậy nhận diện', value: event.confidence ? `${Math.round(event.confidence * 100)}%` : '—' },
      {
        label: 'Thông báo Telegram',
        value: event.telegram_sent ? 'Đã gửi ảnh qua Telegram' : 'Chỉ lưu trong bộ nhớ máy',
        className: event.telegram_sent ? 'text-emerald' : 'text-muted'
      }
    ];

    openLightbox({
      title: `Sự kiện #${event.id} · ${typeInfo.label}`,
      subtitle: formatDate(event.created_at),
      imageUrl: `/api/events/${event.id}/image`,
      downloadUrl: `/api/events/${event.id}/image`,
      metaItems
    });
  } catch (err) {
    toast('Không thể mở ảnh sự kiện', 'error');
  }
};

// Đăng ký sự kiện biểu mẫu
document.getElementById('filters').addEventListener('submit', (e) => {
  e.preventDefault();
  loadHistory();
});

document.getElementById('filterResetBtn').addEventListener('click', () => {
  const form = document.getElementById('filters');
  form.reset();
  loadHistory();
});

// Tải dữ liệu ban đầu khi mở trang
loadHistory();
