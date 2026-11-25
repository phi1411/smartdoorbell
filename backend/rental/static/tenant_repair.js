(() => {
  const panel = document.querySelector("#repair-assistant");
  if (!panel) return;

  const composer = document.querySelector("#repair-composer");
  const issue = document.querySelector("#repair-issue");
  const record = document.querySelector("#repair-record");
  const prepare = document.querySelector("#repair-prepare");
  const progress = document.querySelector("#repair-progress");
  const confirmation = document.querySelector("#repair-confirm");
  const confirmationToken = document.querySelector("#repair-confirmation");
  const category = document.querySelector("#repair-category");
  const subject = document.querySelector("#repair-subject");
  const description = document.querySelector("#repair-description");
  const supportedMedia = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"]
    .find((type) => window.MediaRecorder?.isTypeSupported?.(type));
  record.hidden = !supportedMedia || !navigator.mediaDevices?.getUserMedia;

  let recording;
  let microphone;
  let stopTimer;
  let leaving = false;
  let followUp = false;

  const setStatus = (message) => { progress.textContent = message; };

  function hideDraft() {
    confirmation.hidden = true;
    confirmationToken.value = "";
  }

  function setBusy(busy) {
    prepare.disabled = busy;
    record.disabled = busy;
  }

  async function post(url, type, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": type, "X-CSRF-Token": panel.dataset.csrf },
      body,
    });
    if (response.redirected) throw new Error("Phiên đăng nhập đã hết hạn. Bạn vui lòng đăng nhập lại.");
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.message || "Chưa xử lý được yêu cầu. Bạn có thể thử lại.");
    return result;
  }

  async function prepareDraft() {
    if (!issue.reportValidity() || !issue.value.trim()) return;
    hideDraft();
    setBusy(true);
    setStatus("Hermes đang soạn phiếu…");
    try {
      const result = await post(panel.dataset.draft, "application/json", JSON.stringify({ issue: issue.value }));
      followUp = Boolean(result.question);
      if (followUp) {
        setStatus(result.question);
        issue.focus();
        issue.setSelectionRange(issue.value.length, issue.value.length);
        return;
      }
      subject.value = result.subject;
      category.value = result.category;
      description.value = result.description;
      confirmationToken.value = result.confirmation;
      confirmation.hidden = false;
      setStatus(result.notice || "Xem lại bản nháp rồi xác nhận gửi phiếu.");
      subject.focus();
    } catch (error) {
      setStatus(error.message);
    } finally {
      setBusy(false);
    }
  }

  composer.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!prepare.disabled) void prepareDraft();
  });

  function releaseMicrophone() {
    clearTimeout(stopTimer);
    microphone?.getTracks().forEach((track) => track.stop());
    microphone = undefined;
  }

  record.addEventListener("click", async () => {
    if (recording?.state === "recording") {
      recording.stop();
      return;
    }
    setBusy(true);
    hideDraft();
    try {
      microphone = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (leaving) {
        releaseMicrophone();
        return;
      }
      const audioChunks = [];
      const current = new MediaRecorder(microphone, { mimeType: supportedMedia, audioBitsPerSecond: 48000 });
      recording = current;
      current.ondataavailable = (event) => {
        if (event.data.size) audioChunks.push(event.data);
      };
      current.onerror = () => {
        releaseMicrophone();
        recording = undefined;
        setBusy(false);
        record.textContent = "Thu âm";
        setStatus("Thu âm bị gián đoạn. Bạn có thể nhập mô tả sự cố.");
      };
      current.onstop = async () => {
        releaseMicrophone();
        record.textContent = "Thu âm";
        if (recording !== current) return;
        recording = undefined;
        if (leaving) {
          setBusy(false);
          return;
        }
        setBusy(true);
        try {
          const audio = new Blob(audioChunks, { type: current.mimeType });
          if (!audio.size || audio.size > Number(panel.dataset.audioLimit)) {
            throw new Error("Hãy thu lại một đoạn ngắn hoặc nhập mô tả.");
          }
          setStatus("Hermes đang nghe lại bản thu…");
          const result = await post(panel.dataset.transcribe, audio.type, audio);
          const text = followUp && issue.value.trim() ? issue.value.trim() + " " + result.text : result.text;
          issue.value = text.slice(0, issue.maxLength);
          await prepareDraft();
        } catch (error) {
          setBusy(false);
          setStatus(error.message);
        }
      };
      current.start();
      record.disabled = false;
      record.textContent = "Dừng và gửi bản thu";
      setStatus("Đang thu âm. Bạn có tối đa 20 giây.");
      stopTimer = setTimeout(() => {
        if (current.state === "recording") current.stop();
      }, 20000);
    } catch {
      releaseMicrophone();
      recording = undefined;
      setBusy(false);
      setStatus("Chưa mở được mic. Bạn có thể cấp quyền mic hoặc nhập mô tả.");
    }
  });

  window.addEventListener("pagehide", () => {
    leaving = true;
    if (recording?.state === "recording") recording.stop();
    releaseMicrophone();
  });
  window.addEventListener("pageshow", () => { leaving = false; });
})();
