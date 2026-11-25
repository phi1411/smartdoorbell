(() => {
  const panel = document.querySelector("#live-camera");
  const device = panel.querySelector("#camera-device");
  const start = panel.querySelector("#camera-start");
  const stop = panel.querySelector("#camera-stop");
  const status = panel.querySelector("#camera-status");
  const image = panel.querySelector("#camera-frame");
  const snapshot = panel.querySelector("#camera-snapshot");
  const shotStatus = panel.querySelector("#snapshot-status");
  const shotResult = panel.querySelector("#camera-snapshot-result");
  const shotImage = panel.querySelector("#camera-snapshot-frame");
  const shotCaption = panel.querySelector("#camera-snapshot-caption");
  const flash = panel.querySelector("#camera-flash");
  const flashStatus = panel.querySelector("#direct-flash-status");
  const maxFrameBytes = 96 * 1024;
  let current = null;
  let objectUrl = null;
  let snapshotUrl = null;

  function showStatus(message) {
    if (status.textContent !== message) status.textContent = message;
  }

  function clearImage() {
    image.hidden = true;
    image.removeAttribute("src");
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }

  function clearSnapshot() {
    shotResult.hidden = true;
    shotImage.removeAttribute("src");
    if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
    snapshotUrl = null;
  }

  function requestOptions(run, method = "GET") {
    return {
      method,
      cache: "no-store",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": panel.dataset.csrf, "X-Live-Viewer": run.token || "" },
    };
  }

  function release(run) {
    if (run.token) {
      fetch(run.routes.stop, { ...requestOptions(run, "POST"), keepalive: true }).catch(() => {});
    }
  }

  async function checkedFetch(run, url, options) {
    const timeout = setTimeout(() => run.controller.abort(), 6500);
    try {
      return await fetch(url, { ...options, signal: run.controller.signal });
    } finally {
      clearTimeout(timeout);
    }
  }

  function finish(message) {
    const run = current;
    current = null;
    if (run) {
      clearTimeout(run.renewTimer);
      run.controller.abort();
      release(run);
    }
    clearImage();
    showStatus(message);
    start.disabled = false;
    stop.disabled = true;
    device.disabled = false;
  }

  async function renew(run) {
    if (current !== run) return;
    try {
      const response = await checkedFetch(run, run.routes.renew, requestOptions(run, "POST"));
      if (response.status !== 204 || Date.now() - run.lastData >= 6500) throw new Error();
      if (current === run) run.renewTimer = setTimeout(() => renew(run), 3500);
    } catch {
      if (current === run) finish("Phiên xem đã gián đoạn. Bấm Bắt đầu xem để kết nối lại.");
    }
  }

  function showFrame(bytes) {
    const previous = objectUrl;
    objectUrl = URL.createObjectURL(new Blob([bytes], { type: "image/jpeg" }));
    image.src = objectUrl;
    image.hidden = false;
    if (previous) URL.revokeObjectURL(previous);
    showStatus("Đang xem trực tiếp");
  }

  async function watch(run) {
    try {
      const response = await checkedFetch(run, run.routes.stream, requestOptions(run));
      if (current !== run) return;
      if (response.status === 429) {
        finish("Đã đạt giới hạn phiên xem camera. Vui lòng thử lại sau.");
        return;
      }
      if (!response.ok || response.redirected || !response.body ||
          response.headers.get("Content-Type") !== "application/octet-stream") throw new Error();
      const reader = response.body.getReader();
      let pending = new Uint8Array(0);
      try {
        while (current === run) {
          const { value, done } = await reader.read();
          if (current !== run) return;
          if (done) throw new Error();
          run.lastData = Date.now();
          const bytes = new Uint8Array(pending.length + value.length);
          bytes.set(pending);
          bytes.set(value, pending.length);
          const sizes = new DataView(bytes.buffer);
          let offset = 0;
          let latest = null;
          while (bytes.length - offset >= 4) {
            const length = sizes.getUint32(offset);
            if (length > maxFrameBytes) throw new Error();
            const end = offset + 4 + length;
            if (end > bytes.length) break;
            if (length) latest = bytes.subarray(offset + 4, end);
            offset = end;
          }
          pending = bytes.slice(offset);
          if (latest) {
            showFrame(latest);
            run.frameTime = run.lastData;
          } else if (run.lastData - run.frameTime >= 5000) {
            clearImage();
            showStatus("Chưa nhận được hình. Kiểm tra camera, nguồn và Wi-Fi.");
          }
        }
      } finally {
        reader.releaseLock();
      }
    } catch {
      if (current === run) finish("Phiên xem đã gián đoạn. Bấm Bắt đầu xem để kết nối lại.");
    }
  }

  start.addEventListener("click", async () => {
    if (current) return;
    const run = {
      routes: device.selectedOptions[0].dataset,
      controller: new AbortController(),
      token: "",
      frameTime: Date.now(),
    };
    current = run;
    start.disabled = true;
    stop.disabled = false;
    device.disabled = true;
    showStatus("Đang chờ hình từ camera…");
    try {
      const response = await checkedFetch(run, run.routes.start, requestOptions(run, "POST"));
      if (response.status === 409 || response.status === 429) {
        if (current === run) finish("Camera đang được xem ở phiên khác. Vui lòng thử lại sau.");
        return;
      }
      if (!response.ok || response.redirected) throw new Error();
      run.token = (await response.json()).viewer;
      if (current !== run) {
        release(run);
        return;
      }
      run.lastData = Date.now();
      run.renewTimer = setTimeout(() => renew(run), 3500);
      watch(run);
    } catch {
      if (current === run) finish("Chưa mở được camera. Vui lòng đăng nhập lại hoặc thử sau.");
    }
  });

  stop.addEventListener("click", () => finish("Đã dừng xem."));
  window.addEventListener("pagehide", () => {
    finish("Đã dừng xem.");
    clearSnapshot();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && current) finish("Đã dừng xem.");
  });

  function directRequest(url, method, timeout) {
    return fetch(url, {
      ...requestOptions({}, method),
      redirect: "error",
      signal: AbortSignal.timeout(timeout),
    });
  }

  snapshot.addEventListener("click", async () => {
    const camera = device.selectedOptions[0];
    snapshot.disabled = true;
    clearSnapshot();
    shotStatus.textContent = "Đang chờ ảnh mới từ ESP32…";
    try {
      const response = await directRequest(camera.dataset.snapshot, "POST", 16000);
      if (response.status === 429) {
        shotStatus.textContent = "Camera đang xử lý yêu cầu khác. Thử lại sau vài giây.";
        return;
      }
      if (!response.ok || response.headers.get("Content-Type") !== "image/jpeg") throw new Error();
      const frame = await response.blob();
      if (!frame.size || frame.size > maxFrameBytes) throw new Error();
      snapshotUrl = URL.createObjectURL(frame);
      shotImage.src = snapshotUrl;
      shotCaption.textContent = `${camera.textContent.trim()} · ${new Date().toLocaleTimeString("vi-VN")}`;
      shotResult.hidden = false;
      shotStatus.textContent = "Đã nhận ảnh mới.";
    } catch {
      shotStatus.textContent = "Chưa nhận được ảnh mới. Kiểm tra nguồn và Wi-Fi của ESP32.";
    } finally {
      snapshot.disabled = false;
    }
  });

  flash.addEventListener("click", async () => {
    const camera = device.selectedOptions[0];
    flash.disabled = true;
    flashStatus.textContent = "Đang gửi lệnh tới ESP32…";
    try {
      const response = await directRequest(camera.dataset.flash, "POST", 6000);
      if (response.status !== 202) throw new Error();
      const result = await response.json();
      const statusUrl = new URL(result.status_url, location.href);
      if (statusUrl.origin !== location.origin) throw new Error();
      flashStatus.textContent = "Đang chờ ESP32 xác nhận bật đèn…";
      const deadline = Date.now() + 46000;
      while (Date.now() < deadline) {
        const state = await directRequest(statusUrl, "GET", 6000);
        if (!state.ok) break;
        const { status: flashState } = await state.json();
        if (flashState === "confirmed") {
          flashStatus.textContent = `${camera.value} đã xác nhận bật đèn mô phỏng trong 3 giây.`;
          return;
        }
        if (flashState === "expired") break;
        await new Promise(resolve => setTimeout(resolve, 1200));
      }
      flashStatus.textContent = "ESP32 chưa xác nhận bật đèn. Kiểm tra nguồn và Wi-Fi.";
    } catch {
      flashStatus.textContent = "Chưa xác minh được lệnh mở cửa mô phỏng.";
    } finally {
      flash.disabled = false;
    }
  });
})();
