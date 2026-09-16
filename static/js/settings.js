// Smart Doorbell AI - Settings Logic

const BOOL_KEYS = [
  'unknown_warning_enabled',
  'anti_spoof_enabled',
  'telegram_enabled',
  'save_event_images'
];

const NUMERIC_RULES = {
  doorbell_cooldown_seconds: { min: 0.5, max: 300.0, label: 'Thời gian giãn cách chuông' },
  unknown_alert_seconds: { min: 1.0, max: 3600.0, label: 'Thời gian cảnh báo người lạ' },
  unknown_track_lost_tolerance_seconds: { min: 0.1, max: 30.0, label: 'Dung sai mất dấu' },
  face_match_threshold: { min: 1.0, max: 200.0, label: 'Ngưỡng LBPH' },
  face_min_size: { min: 30, max: 500, label: 'Kích thước khuôn mặt tối thiểu' },
  recognition_every_n_frames: { min: 1, max: 60, label: 'Tần suất phân tích frame' }
};

let initialSettings = {};

// Thu thập giá trị hiện tại từ form
function getCurrentFormValues() {
  const form = document.getElementById('settingsForm');
  const values = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (BOOL_KEYS.includes(el.name)) {
      values[el.name] = el.checked;
    } else if (NUMERIC_RULES[el.name]) {
      values[el.name] = Number(el.value);
    }
  }
  return values;
}

// Kiểm tra form có bị thay đổi (dirty-state) không
function checkDirtyState() {
  const current = getCurrentFormValues();
  let isDirty = false;

  for (const [key, val] of Object.entries(initialSettings)) {
    if (current[key] !== val) {
      isDirty = true;
      break;
    }
  }

  const indicator = document.getElementById('dirtyIndicator');
  if (indicator) {
    if (isDirty) {
      indicator.classList.add('show');
    } else {
      indicator.classList.remove('show');
    }
  }
}

// Tải cài đặt từ backend (GET /api/settings)
async function loadSettings() {
  try {
    const s = await api('/api/settings');
    initialSettings = { ...s };

    for (const [k, v] of Object.entries(s)) {
      const el = document.querySelector(`[name="${k}"]`);
      if (!el) continue;
      if (BOOL_KEYS.includes(k)) {
        el.checked = Boolean(v);
      } else {
        el.value = v;
      }
    }

    const indicator = document.getElementById('dirtyIndicator');
    if (indicator) indicator.classList.remove('show');
  } catch (err) {
    toast(`Không thể tải cài đặt: ${err.message}`, 'error');
  }
}

// Hủy thay đổi và khôi phục giá trị đã lưu
document.getElementById('cancelSettingsBtn').addEventListener('click', () => {
  for (const [k, v] of Object.entries(initialSettings)) {
    const el = document.querySelector(`[name="${k}"]`);
    if (!el) continue;
    if (BOOL_KEYS.includes(k)) {
      el.checked = Boolean(v);
    } else {
      el.value = v;
    }
  }
  const indicator = document.getElementById('dirtyIndicator');
  if (indicator) indicator.classList.remove('show');
  toast('Đã hoàn tác các thay đổi chưa lưu', 'ok');
});

// Lắng nghe sự kiện thay đổi trên form để kích hoạt dirty indicator
document.getElementById('settingsForm').addEventListener('input', checkDirtyState);
document.getElementById('settingsForm').addEventListener('change', checkDirtyState);

// Lưu cài đặt (PUT /api/settings)
document.getElementById('settingsForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const saveBtn = document.getElementById('saveSettingsBtn');
  const payload = getCurrentFormValues();

  // Kiểm tra client-side validation
  for (const [key, rule] of Object.entries(NUMERIC_RULES)) {
    const val = payload[key];
    if (typeof val !== 'number' || isNaN(val) || val < rule.min || val > rule.max) {
      toast(`${rule.label} phải nằm trong khoảng từ ${rule.min} đến ${rule.max}`, 'warn');
      const el = form.querySelector(`[name="${key}"]`);
      if (el) el.focus();
      return;
    }
  }

  saveBtn.disabled = true;
  saveBtn.textContent = 'Đang lưu…';

  try {
    const updated = await api('/api/settings', {
      method: 'PUT',
      body: payload
    });

    initialSettings = { ...updated };
    const indicator = document.getElementById('dirtyIndicator');
    if (indicator) indicator.classList.remove('show');

    toast('✅ Đã lưu thành công cấu hình hệ thống!', 'ok');
  } catch (err) {
    toast(`Lỗi lưu cài đặt: ${err.message}`, 'error');
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = 'Lưu cài đặt';
  }
});

// Khởi chạy khi tải trang
loadSettings();


