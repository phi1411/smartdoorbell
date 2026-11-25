(() => {
  const form = document.querySelector("#face-sample-form");
  if (!form || !navigator.mediaDevices?.getUserMedia || !window.DataTransfer || !window.File) return;

  const file = form.elements.namedItem("photo");
  const register = form.querySelector("button[type=submit]");
  const open = form.querySelector("#face-camera-open");
  const controls = form.querySelector("#face-camera-controls");
  const video = form.querySelector("#face-camera-video");
  const capture = form.querySelector("#face-camera-capture");
  const close = form.querySelector("#face-camera-close");
  const preview = form.querySelector("#face-camera-preview");
  const status = form.querySelector("#face-camera-status");
  const stopTracks = stream => stream?.getTracks().forEach(track => track.stop());
  let cameraRequest = 0;
  let previewUrl = "";

  function inform(message) {
    status.textContent = message;
    status.hidden = !message;
  }

  function closeCamera() {
    cameraRequest += 1;
    stopTracks(video.srcObject);
    video.srcObject = null;
    video.hidden = true;
    controls.hidden = true;
    open.hidden = false;
    capture.disabled = true;
    register.disabled = false;
  }

  function clearPhoto() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = "";
    preview.removeAttribute("src");
    preview.hidden = true;
    open.textContent = "Chụp bằng camera";
  }

  open.hidden = false;
  open.addEventListener("click", async () => {
    const request = ++cameraRequest;
    open.hidden = true;
    preview.hidden = true;
    controls.hidden = false;
    register.disabled = true;
    inform("Đang mở camera…");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: { ideal: "user" }, width: { ideal: 1280 }, height: { ideal: 720 } },
      });
      if (request !== cameraRequest) {
        stopTracks(stream);
        return;
      }
      video.srcObject = stream;
      video.hidden = false;
      await video.play();
      if (request !== cameraRequest) return;
      capture.disabled = false;
      inform("Nhìn thẳng vào camera ở nơi đủ sáng.");
    } catch (error) {
      if (request !== cameraRequest) return;
      closeCamera();
      preview.hidden = !previewUrl;
      inform(error.name === "NotAllowedError" ? "Hãy cấp quyền camera hoặc chọn ảnh từ tệp."
        : "Chưa mở được camera. Bạn có thể chọn ảnh từ tệp.");
    }
  });

  capture.addEventListener("click", () => {
    if (!video.videoWidth || !video.videoHeight) {
      inform("Camera đang chuẩn bị hình. Bạn thử lại nhé.");
      return;
    }
    const request = cameraRequest;
    const canvas = document.createElement("canvas");
    const scale = Math.min(1, 1600 / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    capture.disabled = true;
    try {
      canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(blob => {
        if (request !== cameraRequest) return;
        try {
          if (!blob?.size) throw new Error();
          const selection = new DataTransfer();
          selection.items.add(new File([blob], "mau-tu-camera.jpg", { type: "image/jpeg" }));
          file.files = selection.files;
          clearPhoto();
          previewUrl = URL.createObjectURL(blob);
          preview.src = previewUrl;
          preview.hidden = false;
          closeCamera();
          open.textContent = "Chụp lại";
          inform("Xem lại ảnh rồi bấm Đăng ký mẫu.");
        } catch {
          capture.disabled = false;
          inform("Chưa lưu được ảnh chụp. Bạn thử lại hoặc chọn ảnh từ tệp.");
        }
      }, "image/jpeg", 0.9);
    } catch {
      capture.disabled = false;
      inform("Chưa lấy được ảnh từ camera. Bạn thử lại nhé.");
    }
  });

  function cancelPreview() {
    if (controls.hidden) return;
    closeCamera();
    preview.hidden = !previewUrl;
    inform(previewUrl ? "Xem lại ảnh rồi bấm Đăng ký mẫu." : "");
  }

  close.addEventListener("click", cancelPreview);
  file.addEventListener("change", () => {
    cancelPreview();
    clearPhoto();
    inform("");
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) cancelPreview();
  });
  window.addEventListener("pagehide", cancelPreview);
})();
