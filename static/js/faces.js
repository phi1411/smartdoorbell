// Smart Doorbell AI - Faces Management Logic

let registeredPeople = [];

// Tải danh sách người đã đăng ký từ API
async function loadFaces() {
  const container = document.getElementById('faceList');
  const countBadge = document.getElementById('faceCount');
  const totalBadge = document.getElementById('totalRegisteredBadge');

  try {
    const res = await api('/api/faces');
    registeredPeople = res.items || [];

    const total = registeredPeople.length;
    if (countBadge) countBadge.textContent = `${total} người`;
    if (totalBadge) totalBadge.textContent = `${total} người đã đăng ký`;

    if (!registeredPeople.length) {
      container.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">👤</div>
          <div class="empty-state-title">Chưa có khuôn mặt nào</div>
          <p class="empty-state-desc">Hãy nhập họ tên ở biểu mẫu bên trái và nhìn vào camera để đăng ký người đầu tiên.</p>
        </div>
      `;
      return;
    }

    container.innerHTML = registeredPeople.map(p => {
      const initial = (p.name || '?').trim().slice(0, 1).toUpperCase();
      return `
        <article class="person-item" data-id="${p.id}">
          <div class="person-avatar" aria-hidden="true">${escapeHtml(initial)}</div>
          <div class="person-info">
            <h3 class="person-name">${escapeHtml(p.name)}</h3>
            <div class="person-meta">
              <span class="badge badge-neutral">ID #${p.id}</span>
              <span>·</span>
              <span class="text-emerald">✓ ${p.sample_count || 0} mẫu ảnh</span>
              <span>·</span>
              <span class="font-mono">${formatDate(p.created_at)}</span>
            </div>
          </div>
          <div class="person-actions">
            <button type="button" class="btn btn-secondary btn-sm" onclick="reenrollFace(${p.id})" title="Chụp lại 5 mẫu mới cho người này">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><polyline points="23 20 23 14 17 14"/><path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/></svg>
              <span>Quét lại</span>
            </button>
            <button type="button" class="btn btn-danger-outline btn-sm" onclick="deleteFace(${p.id})" title="Xóa người này khỏi hệ thống">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
              <span>Xóa</span>
            </button>
          </div>
        </article>
      `;
    }).join('');
  } catch (err) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-title text-red">Lỗi tải dữ liệu</div>
        <p class="empty-state-desc">${escapeHtml(err.message)}</p>
        <button type="button" class="btn btn-secondary btn-sm" onclick="loadFaces()">Thử lại</button>
      </div>
    `;
  }
}

// Xử lý gửi biểu mẫu đăng ký khuôn mặt mới (POST /api/faces/enroll)
document.getElementById('enrollForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const nameInput = document.getElementById('personName');
  const submitBtn = document.getElementById('enrollSubmitBtn');
  const btnText = document.getElementById('enrollBtnText');
  const radarSweep = document.getElementById('radarSweep');
  const scanningAlert = document.getElementById('scanningAlert');

  const rawName = nameInput.value.trim();
  if (!rawName) {
    nameInput.focus();
    toast('Vui lòng nhập họ và tên người đăng ký', 'warn');
    return;
  }

  // Khóa form và kích hoạt animation quét radar
  nameInput.disabled = true;
  submitBtn.disabled = true;
  btnText.textContent = 'Đang quét mẫu…';
  radarSweep.classList.add('scanning');
  scanningAlert.classList.add('show');

  try {
    const res = await api('/api/faces/enroll', {
      method: 'POST',
      body: { name: rawName, sample_count: 5 }
    });

    toast(`✅ Đã đăng ký thành công cho "${rawName}" (${res.sample_count || 5} mẫu nét)!`, 'ok');
    form.reset();
    loadFaces();
  } catch (err) {
    toast(`Lỗi đăng ký: ${err.message}`, 'error', 5000);
  } finally {
    nameInput.disabled = false;
    submitBtn.disabled = false;
    btnText.textContent = 'Bắt đầu quét khuôn mặt';
    radarSweep.classList.remove('scanning');
    scanningAlert.classList.remove('show');
    nameInput.focus();
  }
});

// Quét lại mẫu cho một người đã có (POST /api/faces/{id}/enroll)
window.reenrollFace = async function reenrollFace(personId) {
  const person = registeredPeople.find(p => p.id === personId);
  const name = person?.name || `ID #${personId}`;

  const confirmed = await confirmDialog({
    title: 'Xác nhận quét lại mẫu',
    message: `Bạn có muốn thu thập lại 5 mẫu khuôn mặt mới cho "${name}" không? Quá trình sẽ chạy trong khoảng 5–15 giây. Vui lòng nhìn thẳng vào webcam.`,
    confirmText: 'Bắt đầu quét lại',
    cancelText: 'Hủy'
  });

  if (!confirmed) return;

  const radarSweep = document.getElementById('radarSweep');
  if (radarSweep) radarSweep.classList.add('scanning');
  toast(`Đang quét lại cho "${name}"... Hãy nhìn vào camera.`, 'warn', 4000);

  try {
    const res = await api(`/api/faces/${personId}/enroll`, {
      method: 'POST',
      body: { sample_count: 5 }
    });
    toast(`✅ Đã cập nhật thành công ${res.sample_count || 5} mẫu mới cho "${name}"!`, 'ok');
    loadFaces();
  } catch (err) {
    toast(`Lỗi khi quét lại: ${err.message}`, 'error', 5000);
  } finally {
    if (radarSweep) radarSweep.classList.remove('scanning');
  }
};

// Xóa một người (DELETE /api/faces/{id})
window.deleteFace = async function deleteFace(personId) {
  const person = registeredPeople.find(p => p.id === personId);
  const name = person?.name || `ID #${personId}`;

  const confirmed = await confirmDialog({
    title: 'Xác nhận xóa khuôn mặt',
    message: `Bạn có chắc chắn muốn xóa dữ liệu của "${name}" khỏi hệ thống? Toàn bộ 5 mẫu nhận diện AI của người này sẽ bị xóa vĩnh viễn và bộ nhận diện sẽ tự động cập nhật lại. Lịch sử sự kiện trước đây vẫn được giữ nguyên.`,
    confirmText: 'Xác nhận xóa',
    cancelText: 'Hủy bỏ',
    danger: true
  });

  if (!confirmed) return;

  try {
    await api(`/api/faces/${personId}`, { method: 'DELETE' });
    toast(`Đã xóa thành công khuôn mặt của "${name}"`, 'ok');
    loadFaces();
  } catch (err) {
    toast(`Không thể xóa: ${err.message}`, 'error');
  }
};

// Khởi chạy khi tải trang
loadFaces();

