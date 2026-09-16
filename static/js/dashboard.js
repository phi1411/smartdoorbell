// Smart Doorbell AI - Dashboard Logic

const EVENT_LABELS = {
  DOORBELL: 'Bấm chuông cửa',
  UNKNOWN_LOITERING: 'Người chưa xác định lưu lại thời gian dài',
  SPOOF_ATTEMPT: 'Cảnh báo giả mạo'
};

const LIVENESS_TEXTS = {
  real: 'REAL (Người thật)',
  pending: 'PENDING (Hãy chớp mắt)',
  fake: 'FAKE (Cảnh báo giả mạo)',
  disabled: 'Đã tắt anti-spoof',
  not_applicable: 'Không áp dụng'
};

let unknownAlertSeconds = 30.0;
let doorbellCooldownSeconds = 5.0;
let lastEventsHash = '';
let doorbellCooldownTimer = null;
let unknownWarningSaving = false;

// Lấy threshold từ settings một lần
async function loadSystemSettings() {
  try {
    const s = await api('/api/settings');
    if (s && s.unknown_alert_seconds) {
      unknownAlertSeconds = parseFloat(s.unknown_alert_seconds);
    }
    if (s && s.doorbell_cooldown_seconds) {
      doorbellCooldownSeconds = parseFloat(s.doorbell_cooldown_seconds);
    }
  } catch (_) {}
}

function renderStatusDot(status) {
  // status: 'online' | 'warn' | 'offline'
  return `<span class="status-dot ${status}"></span>`;
}

// Cập nhật trạng thái hệ thống và Camera HUD
async function refreshStatus() {
  try {
    const s = await api('/api/status');
    const r = s.recognition?.current || {};
    const cam = s.camera || {};
    const tg = s.telegram || {};
    const unk = s.unknown || {};
    const unknownEnabled = unk.enabled !== false;

    // 1. Trạng thái Camera
    const cameraBadge = document.getElementById('cameraBadge');
    const overlayCameraStatus = document.getElementById('overlayCameraStatus');
    const overlayCameraText = document.getElementById('overlayCameraText');
    const cameraOfflineView = document.getElementById('cameraOfflineView');
    const cameraOfflineMsg = document.getElementById('cameraOfflineMsg');
    const cameraStatus = document.getElementById('cameraStatus');

    if (cam.connected) {
      cameraBadge.className = 'badge badge-success';
      cameraBadge.textContent = 'LIVE';
      overlayCameraStatus.className = 'camera-status-pill live';
      overlayCameraText.textContent = 'LIVE';
      cameraOfflineView.classList.remove('show');
      cameraStatus.innerHTML = `${renderStatusDot('online')} Kết nối bình thường`;
    } else {
      cameraBadge.className = 'badge badge-danger';
      cameraBadge.textContent = 'OFFLINE';
      overlayCameraStatus.className = 'camera-status-pill offline';
      overlayCameraText.textContent = 'OFFLINE';
      cameraOfflineView.classList.add('show');
      if (cam.error) {
        cameraOfflineMsg.textContent = `Lỗi thiết bị: ${cam.error}. Hệ thống đang tự động thử kết nối lại...`;
      }
      cameraStatus.innerHTML = `${renderStatusDot('offline')} Mất kết nối`;
    }

    // 2. Nhận diện khuôn mặt & Liveness
    const currentIdentity = document.getElementById('currentIdentity');
    const livenessStatus = document.getElementById('livenessStatus');
    const confidenceScore = document.getElementById('confidenceScore');
    const overlayLiveness = document.getElementById('overlayLiveness');

    // Danh tính từng người trong khung hình
    const detectedFaces = Array.isArray(r.faces) ? r.faces : [];
    if ((r.face_count || detectedFaces.length) > 1) {
      const faceCount = r.face_count || detectedFaces.length;
      const knownNames = [...new Set(detectedFaces
        .filter(face => face.status === 'known' && face.name)
        .map(face => face.name))];
      const parts = [];
      if (knownNames.length) parts.push(`Đã biết: ${knownNames.map(escapeHtml).join(', ')}`);
      if (r.unknown_count) parts.push(`Chưa xác định: ${r.unknown_count}`);
      if (r.fake_count) parts.push(`Nghi giả mạo: ${r.fake_count}`);
      currentIdentity.innerHTML = `<span class="text-amber">👥 ${faceCount} người${parts.length ? ` · ${parts.join(' · ')}` : ''}</span>`;
    } else if (r.status === 'known') {
      currentIdentity.innerHTML = `<span class="text-emerald">👤 ${escapeHtml(r.name || 'Người quen')}</span>`;
    } else if (r.status === 'unknown') {
      currentIdentity.innerHTML = `<span class="text-amber">⚠️ Người chưa xác định</span>`;
    } else if (r.status === 'multiple_faces') {
      currentIdentity.innerHTML = `<span class="text-amber">👥 Nhiều khuôn mặt trong khung</span>`;
    } else {
      currentIdentity.innerHTML = `<span class="text-dim">Chưa phát hiện khuôn mặt</span>`;
    }

    // Liveness Badge & Pill
    const liveKey = (r.liveness_status || 'disabled').toLowerCase();
    overlayLiveness.className = `camera-liveness-pill ${liveKey}`;
    overlayLiveness.textContent = (r.liveness_status || '—').toUpperCase();
    livenessStatus.textContent = LIVENESS_TEXTS[liveKey] || r.liveness_status || '—';
    if (liveKey === 'real') {
      livenessStatus.className = 'telemetry-value text-emerald';
    } else if (liveKey === 'fake') {
      livenessStatus.className = 'telemetry-value text-red';
    } else if (liveKey === 'pending') {
      livenessStatus.className = 'telemetry-value text-amber';
    } else {
      livenessStatus.className = 'telemetry-value text-muted';
    }

    // Backend trả confidence chuẩn hóa 0..1 (không phải raw LBPH distance).
    if ((r.status === 'known' || r.status === 'multiple_faces')
        && typeof r.confidence === 'number' && r.confidence > 0) {
      confidenceScore.textContent = `${Math.round(r.confidence * 100)}%`;
    } else {
      confidenceScore.textContent = '—';
    }

    // 3. Panel Trạng thái Hệ thống
    const recognitionStatus = document.getElementById('recognitionStatus');
    if (s.recognition?.running) {
      const trainedNote = s.recognition.trained ? 'Đã huấn luyện' : 'Chưa có mẫu';
      recognitionStatus.innerHTML = `${renderStatusDot('online')} Sẵn sàng (${trainedNote})`;
    } else {
      recognitionStatus.innerHTML = `${renderStatusDot('offline')} Không khả dụng`;
    }

    const telegramStatus = document.getElementById('telegramStatus');
    if (!tg.configured) {
      telegramStatus.innerHTML = `${renderStatusDot('offline')} <span class="text-dim">Chưa cấu hình (.env)</span>`;
    } else if (!tg.enabled) {
      telegramStatus.innerHTML = `${renderStatusDot('warn')} <span class="text-amber">Tạm tắt trong Cài đặt</span>`;
    } else if (tg.online) {
      telegramStatus.innerHTML = `${renderStatusDot('online')} <span class="text-emerald">Trực tuyến</span>`;
    } else {
      const errNote = tg.last_error ? ` (${escapeHtml(tg.last_error)})` : '';
      telegramStatus.innerHTML = `${renderStatusDot('offline')} <span class="text-red">Ngoại tuyến${errNote}</span>`;
    }

    const antiStatus = document.getElementById('antiStatus');
    if (s.anti_spoof?.enabled) {
      antiStatus.innerHTML = `<span class="text-emerald">Kích hoạt</span>`;
    } else {
      antiStatus.innerHTML = `<span class="text-dim">Đã tắt</span>`;
    }

    const registeredCount = document.getElementById('registeredFacesCount');
    registeredCount.textContent = `${s.registered_faces || 0} người`;

    const unknownWarningToggle = document.getElementById('unknownWarningToggle');
    const unknownWarningHint = document.getElementById('unknownWarningHint');
    if (!unknownWarningSaving) {
      unknownWarningToggle.checked = unknownEnabled;
    }
    unknownWarningHint.textContent = unknownEnabled
      ? 'Đang bật · sẽ gửi Telegram khi đủ thời gian'
      : 'Đang tắt · không theo dõi, không gửi cảnh báo';

    // 4. Unknown Tracker (Theo dõi người lạ)
    const unknownBadge = document.getElementById('unknownBadge');
    const unknownProgressBar = document.getElementById('unknownProgressBar');
    const unknownElapsedText = document.getElementById('unknownElapsedText');
    const unknownTrackerBox = document.getElementById('unknownTrackerBox');

    const state = unk.state || 'IDLE';
    const elapsed = typeof unk.elapsed_seconds === 'number' ? unk.elapsed_seconds : 0.0;
    const pct = Math.min(100, Math.max(0, (elapsed / unknownAlertSeconds) * 100));

    if (!unknownEnabled || state === 'DISABLED') {
      unknownTrackerBox.classList.add('disabled');
      unknownBadge.className = 'unknown-state-badge';
      unknownBadge.textContent = 'ĐÃ TẮT';
      unknownProgressBar.className = 'unknown-progress-bar';
      unknownProgressBar.style.width = '0%';
      unknownElapsedText.textContent = 'Không theo dõi';
    } else if (state === 'ALERTED' || unk.alerted) {
      unknownTrackerBox.classList.remove('disabled');
      unknownBadge.className = 'unknown-state-badge alerted';
      unknownBadge.textContent = '🚨 CẢNH BÁO!';
      unknownProgressBar.className = 'unknown-progress-bar alerted';
      unknownElapsedText.innerHTML = `<strong class="text-red">${elapsed.toFixed(1)}s</strong> / ${unknownAlertSeconds}s (Đã cảnh báo)`;
    } else if (state === 'TRACKING_UNKNOWN') {
      unknownTrackerBox.classList.remove('disabled');
      unknownBadge.className = 'unknown-state-badge tracking';
      unknownBadge.textContent = 'THEO DÕI';
      unknownProgressBar.className = 'unknown-progress-bar tracking';
      unknownProgressBar.style.width = `${pct.toFixed(1)}%`;
      unknownElapsedText.textContent = `${elapsed.toFixed(1)}s / ${unknownAlertSeconds}s`;
    } else {
      unknownTrackerBox.classList.remove('disabled');
      unknownBadge.className = 'unknown-state-badge';
      unknownBadge.textContent = 'IDLE';
      unknownProgressBar.className = 'unknown-progress-bar';
      unknownProgressBar.style.width = '0%';
      unknownElapsedText.textContent = `0.0s / ${unknownAlertSeconds}s`;
    }
  } catch (err) {
    const cameraBadge = document.getElementById('cameraBadge');
    const overlayCameraStatus = document.getElementById('overlayCameraStatus');
    const overlayCameraText = document.getElementById('overlayCameraText');
    const cameraOfflineView = document.getElementById('cameraOfflineView');
    const cameraOfflineMsg = document.getElementById('cameraOfflineMsg');
    const cameraStatus = document.getElementById('cameraStatus');
    if (cameraBadge) {
      cameraBadge.className = 'badge badge-danger';
      cameraBadge.textContent = 'MẤT KẾT NỐI';
    }
    if (overlayCameraStatus) overlayCameraStatus.className = 'camera-status-pill offline';
    if (overlayCameraText) overlayCameraText.textContent = 'RECONNECTING';
    if (cameraOfflineView) cameraOfflineView.classList.add('show');
    if (cameraOfflineMsg) cameraOfflineMsg.textContent = 'Không thể lấy trạng thái hệ thống. Đang tự động kết nối lại…';
    if (cameraStatus) cameraStatus.innerHTML = `${renderStatusDot('offline')} Mất kết nối máy chủ`;
  }
}

async function setUnknownWarningEnabled() {
  const toggle = document.getElementById('unknownWarningToggle');
  const requested = toggle.checked;
  unknownWarningSaving = true;
  toggle.disabled = true;
  try {
    await api('/api/settings', {
      method: 'PUT',
      body: { unknown_warning_enabled: requested }
    });
    toast(
      requested
        ? 'Đã bật cảnh báo người chưa xác định lưu lại thời gian dài'
        : 'Đã tắt cảnh báo người chưa xác định',
      'ok'
    );
  } catch (err) {
    toggle.checked = !requested;
    toast(`Không thể cập nhật cảnh báo: ${err.message}`, 'error');
  } finally {
    unknownWarningSaving = false;
    toggle.disabled = false;
    refreshStatus();
  }
}

// Cập nhật danh sách Sự kiện gần đây
async function refreshEvents() {
  try {
    const data = await api('/api/events?limit=6');
    const items = data.items || [];
    const container = document.getElementById('events');
    if (!container) return;

    // Tránh re-render liên tục gây giật nếu dữ liệu không đổi
    const hash = JSON.stringify(items.map(e => [e.id, e.event_type, e.created_at, e.telegram_sent]));
    if (hash === lastEventsHash) return;
    lastEventsHash = hash;

    container.setAttribute('aria-busy', 'false');

    if (!items.length) {
      container.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">🔔</div>
          <div class="empty-state-title">Chưa có sự kiện nào</div>
          <p class="empty-state-desc">Các sự kiện bấm chuông, người lạ hoặc cảnh báo an ninh sẽ hiển thị ở đây.</p>
        </div>
      `;
      return;
    }

    container.innerHTML = items.map(e => {
      const typeClass = e.event_type === 'DOORBELL' ? 'doorbell' : (e.event_type === 'SPOOF_ATTEMPT' ? 'spoof' : 'unknown');
      const icon = e.event_type === 'DOORBELL' ? '🔔' : (e.event_type === 'SPOOF_ATTEMPT' ? '🚨' : '⚠️');
      const title = e.recognized_name ? `Khuôn mặt: ${escapeHtml(e.recognized_name)}` : EVENT_LABELS[e.event_type] || e.event_type;
      const tgBadge = e.telegram_sent 
        ? '<span class="badge badge-success">✓ Telegram</span>'
        : '<span class="badge badge-neutral">Lưu cục bộ</span>';

      const thumbHtml = e.image_path ? `
        <button type="button" class="event-thumb-btn" onclick="viewEventImage(${e.id})" title="Nhấn để xem ảnh phóng to" aria-label="Xem ảnh sự kiện ${e.id}">
          <img src="/api/events/${e.id}/image" alt="Ảnh sự kiện" class="event-thumb-img" loading="lazy">
        </button>
      ` : '';

      return `
        <article class="event-item">
          <div class="event-icon-box ${typeClass}">
            <span aria-hidden="true">${icon}</span>
          </div>
          <div class="event-body">
            <div class="event-title">${title}</div>
            <div class="event-meta">
              <span>${EVENT_LABELS[e.event_type] || e.event_type}</span>
              <span>·</span>
              <span class="font-mono">${formatDate(e.created_at)}</span>
            </div>
          </div>
          <div style="display: flex; align-items: center; gap: 10px;">
            ${tgBadge}
            ${thumbHtml}
          </div>
        </article>
      `;
    }).join('');
  } catch (err) {
    // Force the next successful response to render even when its data matches
    // the last pre-disconnect snapshot.
    lastEventsHash = '';
    const container = document.getElementById('events');
    if (container) {
      container.setAttribute('aria-busy', 'false');
      container.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-title text-red">Không thể tải sự kiện</div>
          <p class="empty-state-desc">${escapeHtml(err.message)} Hệ thống sẽ tự thử lại.</p>
        </div>
      `;
    }
  }
}

// Xử lý xem ảnh phóng to trong Modal Lightbox
window.viewEventImage = async function viewEventImage(eventId) {
  try {
    const event = await api(`/api/events/${eventId}`);
    if (!event) return;

    const metaItems = [
      { label: 'Mã sự kiện', value: `#${event.id}` },
      { label: 'Loại sự kiện', value: EVENT_LABELS[event.event_type] || event.event_type },
      { label: 'Thời gian ghi nhận', value: formatDate(event.created_at) },
      { label: 'Danh tính nhận diện', value: event.recognized_name || (event.recognition_status ? event.recognition_status.toUpperCase() : 'Không rõ') },
      { label: 'Trạng thái Liveness', value: LIVENESS_TEXTS[event.liveness_status] || event.liveness_status || '—' },
      { label: 'Độ tin cậy', value: event.confidence ? `${Math.round(event.confidence * 100)}%` : '—' },
      { 
        label: 'Thông báo Telegram', 
        value: event.telegram_sent ? 'Đã gửi ảnh & thông báo' : 'Chỉ lưu trong máy',
        className: event.telegram_sent ? 'text-emerald' : 'text-muted'
      }
    ];

    openLightbox({
      title: `Sự kiện #${event.id} · ${EVENT_LABELS[event.event_type] || event.event_type}`,
      subtitle: formatDate(event.created_at),
      imageUrl: `/api/events/${event.id}/image`,
      downloadUrl: `/api/events/${event.id}/image`,
      metaItems
    });
  } catch (err) {
    toast('Không thể tải thông tin ảnh sự kiện', 'error');
  }
};

// Đếm ngược cooldown nút chuông cửa
function startDoorbellCooldown(seconds) {
  clearInterval(doorbellCooldownTimer);
  const bellBtn = document.getElementById('bell');
  const bellText = document.getElementById('bellText');
  const bellHint = document.getElementById('bellHint');

  let remaining = Math.max(0.5, seconds);
  bellBtn.disabled = true;
  bellBtn.classList.add('cooldown');

  const update = () => {
    if (remaining <= 0.05) {
      clearInterval(doorbellCooldownTimer);
      bellBtn.disabled = false;
      bellBtn.classList.remove('cooldown');
      bellText.textContent = 'BẤM CHUÔNG CỬA';
      bellHint.textContent = 'Sẵn sàng nhận khách';
      return;
    }
    bellText.textContent = `VUI LÒNG CHỜ (${remaining.toFixed(1)}s)`;
    bellHint.textContent = `Đang kích hoạt thời gian giãn cách (${remaining.toFixed(1)}s còn lại)`;
    remaining -= 0.1;
  };

  update();
  doorbellCooldownTimer = setInterval(update, 100);
}

// Xử lý sự kiện Bấm Chuông Cửa (POST /api/doorbell)
document.getElementById('bell').addEventListener('click', async () => {
  const bellBtn = document.getElementById('bell');
  const bellText = document.getElementById('bellText');
  const bellHint = document.getElementById('bellHint');

  bellBtn.disabled = true;
  bellBtn.classList.add('ringing');
  bellText.textContent = 'ĐANG XỬ LÝ…';

  try {
    const res = await api('/api/doorbell', { method: 'POST' });

    // 202 Accepted
    if (res.telegram_queued) {
      toast('🔔 Đã lưu sự kiện chuông cửa và xếp hàng gửi Telegram!', 'ok');
      bellHint.textContent = 'Đã gửi thông báo đến chủ nhà qua Telegram';
    } else {
      toast('🔔 Đã ghi nhận sự kiện chuông cửa (Lưu cục bộ)', 'ok');
      bellHint.textContent = 'Đã lưu sự kiện cục bộ (Telegram tắt hoặc chưa cấu hình)';
    }

    refreshEvents();
    startDoorbellCooldown(res.cooldown_seconds || doorbellCooldownSeconds);
  } catch (err) {
    if (err.status === 429) {
      const retryAfter = err.body?.retry_after_seconds || 5.0;
      toast(`Chuông đang trong thời gian giãn cách. Vui lòng chờ ${retryAfter.toFixed(1)}s`, 'warn');
      startDoorbellCooldown(retryAfter);
    } else if (err.status === 503) {
      toast('Camera hiện không khả dụng. Không thể chụp ảnh chuông cửa.', 'error');
      bellHint.textContent = 'Không thể bấm chuông: Camera offline';
      bellBtn.disabled = false;
      bellBtn.classList.remove('ringing');
      bellText.textContent = 'BẤM CHUÔNG CỬA';
    } else {
      toast(err.message || 'Lỗi khi kích hoạt chuông cửa', 'error');
      bellBtn.disabled = false;
      bellBtn.classList.remove('ringing');
      bellText.textContent = 'BẤM CHUÔNG CỬA';
    }
  }
});

// Khởi chạy
(function initDashboard() {
  // Đồng hồ trang
  const clockEl = document.getElementById('clock');
  const updateClock = () => {
    if (clockEl) {
      clockEl.textContent = new Date().toLocaleTimeString('vi-VN');
    }
  };
  updateClock();
  setInterval(updateClock, 1000);

  document.getElementById('unknownWarningToggle')
    .addEventListener('change', setUnknownWarningEnabled);

  // Tải cài đặt ban đầu & telemetry
  loadSystemSettings();
  refreshStatus();
  refreshEvents();

  // Polling chu kỳ
  setInterval(refreshStatus, 1200);
  setInterval(refreshEvents, 4500);
})();
