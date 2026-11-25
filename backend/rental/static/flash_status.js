const labels = {
  confirmed: "ESP32 đã xác nhận bật đèn.",
  expired: "Hết hạn chờ ESP32 xác nhận.",
};

async function followFlash(element) {
  const deadline = Date.now() + 50000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(element.dataset.flashStatus, {
        headers: { Accept: "application/json" },
        redirect: "error",
        signal: AbortSignal.timeout(4000),
      });
      if (response.ok) {
        const { status } = await response.json();
        if (Object.hasOwn(labels, status)) {
          element.textContent = labels[status];
          return;
        }
      }
    } catch {
      // A brief network interruption can recover on the next check.
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  element.textContent = "Chưa nhận được xác nhận. Tải lại trang để xem trạng thái.";
}

document.querySelectorAll("[data-flash-status]").forEach(followFlash);

document.querySelectorAll("[data-flash-command]").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const feedback = form.querySelector("[role=status]");
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { Accept: "application/json" },
        body: new FormData(form),
        redirect: "error",
        signal: AbortSignal.timeout(5000),
      });
      if (response.status === 409) {
        feedback.textContent = "Lượt chuông đã hết hạn hoặc có lượt mới hơn.";
        return;
      }
      if (!response.ok) throw new Error();
      feedback.textContent = "Đang chờ ESP32 xác nhận.";
      feedback.dataset.flashStatus = form.action;
      await followFlash(feedback);
    } catch {
      feedback.textContent = "Chưa gửi được yêu cầu. Bấm lại để thử.";
      button.disabled = false;
    }
  });
});
