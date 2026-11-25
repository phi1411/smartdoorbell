(() => {
  const panel = document.querySelector("#intercom-audio");
  const status = document.querySelector("#intercom-state");
  const feedback = document.querySelector("#intercom-feedback");
  const enable = document.querySelector("#intercom-enable");
  const mute = document.querySelector("#intercom-mute");
  const listen = document.querySelector("#intercom-listen");
  const retry = document.querySelector("#intercom-retry");
  const reconnect = document.querySelector("#intercom-reconnect");
  const end = document.querySelector("#intercom-end");
  const flash = document.querySelector("#intercom-flash");
  const player = document.querySelector("#intercom-player");
  const assistantBox = document.querySelector("#intercom-assistant");
  const assistantStatus = document.querySelector("#assistant-status");
  const assistantLog = document.querySelector("#assistant-log");
  const greetingPlayer = document.querySelector("#intercom-greeting");
  const greetingSource = greetingPlayer?.getAttribute("src");
  const assistantReplay = document.querySelector("#assistant-replay");
  const assistantReplayReply = document.querySelector("#assistant-replay-reply");
  const assistantSpeak = document.querySelector("#assistant-speak");
  const assistantForm = document.querySelector("#assistant-form");
  const assistantInput = document.querySelector("#assistant-input");
  const assistantSend = document.querySelector("#assistant-send");
  const station = panel.dataset.side === "station";
  const stationAudioKey = "rental-station-audio-enabled";
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const canRecord = !!(window.MediaRecorder && navigator.mediaDevices?.getUserMedia
    && (window.AudioContext || window.webkitAudioContext));
  const microphone = { audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false };
  const statusText = {
    idle: station ? "Đang chờ khách bấm chuông." : "Đang chờ đàm thoại.",
    ringing: station ? "Có khách bấm chuông. Vui lòng chờ chủ trọ." : "Có khách bấm chuông.",
    assistant: station ? "Trợ lý Hermes đang tiếp khách (chủ trọ chưa kịp nghe máy)." : "Trợ lý Hermes đang tiếp khách.",
    active: station ? "Chủ trọ đã trả lời." : "Đã trả lời chuông.",
    ended: "Đã kết thúc đàm thoại.",
  };

  let call = { status: "idle", visit_id: null };
  let client;
  let media;
  let peer;
  let pc;
  let description;
  let gathering;
  let settingRemote = false;
  let connectionTimer;
  let stateDeadline;
  let iceServers;
  let muted = false;
  let armed = false;
  let autoEnable = !station;
  let acquiring = false;
  let polling = false;
  let ended = false;
  let paused = false;
  let pollTimer;
  let audioContext;
  let lastChime;
  let wakeLock;
  let assistantVisit = null;
  let assistantBusy = false;
  let assistantDone = false;
  let assistantListening = false;
  let assistantSpeaking = false;
  let assistantAutoRetries = 0;
  let greetingStarted = false;
  let greetingNeedsAction = false;
  let stopGreeting;
  let stopReply;
  let assistantSpeechTimer;
  let assistantRetryTimer;
  let recognition;
  let recording;
  let assistantMedia;
  let preferRecording = canRecord;

  const newClient = () => Array.from(crypto.getRandomValues(new Uint8Array(16)),
    byte => byte.toString(16).padStart(2, "0")).join("");
  const active = () => call.status === "active" && !document.hidden && !ended;
  const stop = stream => stream?.getTracks().forEach(track => track.stop());

  function message(value) {
    if (feedback.textContent !== value) feedback.textContent = value;
  }

  const assistantActive = visitId =>
    station && !ended && !document.hidden && call.status === "assistant" && call.visit_id === visitId;

  function setAssistantStatus(value) {
    if (assistantStatus && assistantStatus.textContent !== value) assistantStatus.textContent = value;
  }

  function appendAssistantTurn(speaker, text) {
    if (!assistantLog) return;
    const item = document.createElement("div");
    item.className = "assistant-turn";
    item.dataset.speaker = speaker;
    const label = document.createElement("strong");
    label.textContent = speaker === "guest" ? "Khách: " : "Hermes: ";
    item.append(label, document.createTextNode(text));
    assistantLog.appendChild(item);
    assistantLog.scrollTop = assistantLog.scrollHeight;
  }

  function updateAssistantControls() {
    if (!assistantBox) return;
    const visible = station && call.status === "assistant" && !ended;
    assistantBox.hidden = !visible;
    if (!visible) return;
    if (assistantReplay) {
      assistantReplay.hidden = !greetingNeedsAction;
      assistantReplay.disabled = assistantBusy || assistantSpeaking || assistantDone;
    }
    if (assistantSpeak) {
      assistantSpeak.hidden = (!SpeechRecognition && !canRecord) || assistantDone;
      assistantSpeak.disabled = assistantBusy || assistantSpeaking || assistantListening;
      assistantSpeak.textContent = assistantListening ? "Đang nghe…" : "Nói với trợ lý";
    }
    if (assistantInput) assistantInput.disabled = assistantBusy || assistantDone;
    if (assistantSend) assistantSend.disabled = assistantBusy || assistantDone;
  }

  function stopRecognition() {
    clearTimeout(assistantRetryTimer);
    assistantRetryTimer = null;
    const current = recognition;
    recognition = null;
    assistantListening = false;
    if (recording) {
      const currentRecording = recording;
      recording = null;
      currentRecording.cancel?.();
    }
    if (current) {
      try {
        current.onresult = null;
        current.onerror = null;
        current.onend = null;
        current.abort();
      } catch {
        // Ignore already-stopped recognition instances.
      }
    }
  }

  function stopAssistant(clearVisit = true) {
    const hadRecognition = Boolean(recognition || assistantListening || assistantMedia);
    stopRecognition();
    stopGreeting?.();
    stopReply?.();
    stop(assistantMedia);
    assistantMedia = null;
    clearTimeout(assistantSpeechTimer);
    assistantSpeechTimer = null;
    assistantSpeaking = false;
    assistantBusy = false;
    if (station && window.speechSynthesis) {
      try {
        window.speechSynthesis.cancel();
      } catch {
        // Ignore speech synthesis cancel errors.
      }
    }
    if (clearVisit) {
      assistantVisit = null;
      assistantDone = false;
      assistantAutoRetries = 0;
      greetingStarted = false;
      greetingNeedsAction = false;
    }
    updateAssistantControls();
    return hadRecognition;
  }

  function playGreeting(visitId) {
    if (!greetingPlayer || !assistantActive(visitId) || assistantSpeaking || assistantBusy || assistantDone) return;
    greetingStarted = true;
    greetingNeedsAction = false;
    stopRecognition();
    assistantSpeaking = true;
    setAssistantStatus("Hermes đang chào…");
    updateAssistantControls();

    const finish = played => {
      if (stopGreeting !== cleanup) return;
      cleanup();
      if (!assistantActive(visitId)) return;
      greetingNeedsAction = !played;
      updateAssistantControls();
      if (played && armed && (SpeechRecognition || canRecord)) startListening(visitId);
      else setAssistantStatus(played ? "Bạn có thể nhập lời nhắn bên dưới."
        : "Bấm Nghe lời chào để thử lại hoặc nhập lời nhắn bên dưới.");
    };
    const ended = () => finish(true);
    const failed = () => finish(false);
    const timeout = setTimeout(failed, 12000);
    const cleanup = () => {
      stopGreeting = null;
      clearTimeout(timeout);
      greetingPlayer.removeEventListener("ended", ended);
      greetingPlayer.removeEventListener("error", failed);
      greetingPlayer.pause();
      assistantSpeaking = false;
      updateAssistantControls();
    };
    stopGreeting = cleanup;
    greetingPlayer.addEventListener("ended", ended, { once: true });
    greetingPlayer.addEventListener("error", failed, { once: true });
    try {
      greetingPlayer.currentTime = 0;
      greetingPlayer.play().catch(failed);
    } catch {
      failed();
    }
  }

  function pickVietnameseVoice() {
    const voices = window.speechSynthesis?.getVoices?.() || [];
    return voices.find(voice => /^vi\b/i.test(voice.lang)) || null;
  }

  function speakOnPhone(visitId, text, onDone) {
    clearTimeout(assistantSpeechTimer);
    if (!armed || !window.speechSynthesis || !text) {
      assistantSpeaking = false;
      updateAssistantControls();
      assistantSpeechTimer = setTimeout(() => {
        if (assistantActive(visitId)) onDone?.();
      }, 250);
      return;
    }
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      clearTimeout(assistantSpeechTimer);
      assistantSpeechTimer = null;
      assistantSpeaking = false;
      updateAssistantControls();
      if (assistantActive(visitId)) onDone?.();
    };
    try {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = "vi-VN";
      utterance.rate = 1.02;
      const voice = pickVietnameseVoice();
      if (voice) utterance.voice = voice;
      utterance.onend = finish;
      utterance.onerror = finish;
      assistantSpeaking = true;
      setAssistantStatus("Hermes đang nói…");
      updateAssistantControls();
      const safetyMs = Math.min(18000, Math.max(4500, text.length * 115));
      assistantSpeechTimer = setTimeout(finish, safetyMs);
      window.speechSynthesis.speak(utterance);
    } catch {
      finish();
    }
  }

  function speakAssistant(visitId, replyId, text, onDone) {
    if (!armed || !greetingPlayer || !replyId || !panel.dataset.assistantUrl) {
      speakOnPhone(visitId, text, onDone);
      return;
    }
    const controller = new AbortController();
    let clipUrl;
    let timeout = setTimeout(() => finish(false), 24000);
    const ended = () => finish(true);
    const failed = () => finish(false);
    const cleanup = () => {
      stopReply = null;
      clearTimeout(timeout);
      controller.abort();
      greetingPlayer.removeEventListener("ended", ended);
      greetingPlayer.removeEventListener("error", failed);
      if (clipUrl) {
        greetingPlayer.pause();
        URL.revokeObjectURL(clipUrl);
        greetingPlayer.src = greetingSource;
      }
      if (assistantReplayReply) {
        assistantReplayReply.hidden = true;
        assistantReplayReply.onclick = null;
      }
      assistantSpeaking = false;
      updateAssistantControls();
    };
    const finish = played => {
      if (stopReply !== cleanup) return;
      cleanup();
      if (!assistantActive(visitId)) return;
      if (played) onDone?.();
      else speakOnPhone(visitId, text, onDone);
    };
    const play = async () => {
      try {
        await greetingPlayer.play();
        if (stopReply !== cleanup) return;
        clearTimeout(timeout);
        timeout = setTimeout(ended, 34000);
        if (assistantReplayReply) assistantReplayReply.hidden = true;
        setAssistantStatus("Hermes đang nói…");
      } catch (error) {
        if (stopReply !== cleanup) return;
        if (error.name === "NotAllowedError" && assistantReplayReply) {
          clearTimeout(timeout);
          assistantReplayReply.hidden = false;
          assistantReplayReply.onclick = play;
          setAssistantStatus("Bấm Nghe câu trả lời để bật loa.");
        } else failed();
      }
    };
    stopReply = cleanup;
    assistantSpeaking = true;
    setAssistantStatus("Hermes đang chuẩn bị giọng nói…");
    updateAssistantControls();
    fetch(`${panel.dataset.assistantUrl}/speech/${replyId}`, {
      method: "POST",
      headers: { "X-CSRF-Token": panel.dataset.csrf, "X-Intercom-Visit": String(visitId) },
      signal: controller.signal,
      redirect: "error",
    }).then(async response => {
      if (!response.ok || !response.headers.get("Content-Type")?.startsWith("audio/wav")) throw new Error();
      const audio = await response.blob();
      if (stopReply !== cleanup || !assistantActive(visitId)) return;
      clipUrl = URL.createObjectURL(audio);
      greetingPlayer.src = clipUrl;
      greetingPlayer.addEventListener("ended", ended, { once: true });
      greetingPlayer.addEventListener("error", failed, { once: true });
      await play();
    }).catch(failed);
  }

  async function recordAssistantTurn(visitId) {
    stopRecognition();
    const attempt = {};
    recording = attempt;
    assistantListening = true;
    setAssistantStatus("Đang mở mic…");
    updateAssistantControls();
    try {
      const alive = assistantMedia?.getAudioTracks().some(track => track.readyState === "live");
      if (!alive) stop(assistantMedia);
      const stream = alive ? assistantMedia : await navigator.mediaDevices.getUserMedia(microphone);
      if (recording !== attempt || !assistantActive(visitId)) {
        if (!alive) stop(stream);
        return;
      }
      assistantMedia = stream;
      const enableTracks = enabled => stream.getAudioTracks().forEach(track => { track.enabled = enabled; });
      enableTracks(true);
      attempt.cancel = () => enableTracks(false);
      const Context = window.AudioContext || window.webkitAudioContext;
      audioContext ||= new Context();
      if (audioContext.state !== "running") {
        let activationTimer;
        try {
          await Promise.race([audioContext.resume(), new Promise((_, reject) => {
            activationTimer = setTimeout(reject, 2000);
          })]);
        } finally {
          clearTimeout(activationTimer);
        }
      }
      if (recording !== attempt || !assistantActive(visitId)) {
        return;
      }
      const mimeType = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"]
        .find(type => MediaRecorder.isTypeSupported(type));
      const recorder = new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 48000 });
      const actualType = (recorder.mimeType || mimeType || "").split(";")[0];
      const input = audioContext.createMediaStreamSource(stream);
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 1024;
      input.connect(analyser);
      const values = new Float32Array(analyser.fftSize);
      const chunks = [];
      let timer;
      let total = 0;
      let voicedSamples = 0;
      let lastVoiceAt = 0;
      const startedAt = performance.now();

      const cleanup = () => {
        clearInterval(timer);
        input.disconnect();
        enableTracks(false);
      };
      const finish = send => {
        if (recording !== attempt) return;
        recording = null;
        assistantListening = false;
        cleanup();
        if (!assistantActive(visitId)) return;
        if (send && total && total <= 256 * 1024 && voicedSamples >= 3) {
          sendAssistantMessage(visitId, new Blob(chunks, { type: actualType }));
        } else if (send && voicedSamples < 3 && armed) {
          startListening(visitId);
        } else {
          setAssistantStatus("Chưa nghe rõ. Bấm Nói với trợ lý hoặc nhập lời nhắn.");
          updateAssistantControls();
        }
      };
      attempt.cancel = () => {
        recorder.onstop = null;
        recorder.onerror = null;
        try {
          if (recorder.state !== "inactive") recorder.stop();
        } finally {
          cleanup();
        }
      };
      recorder.ondataavailable = event => {
        if (event.data.size) {
          chunks.push(event.data);
          total += event.data.size;
        }
      };
      recorder.onstop = () => finish(true);
      recorder.onerror = () => finish(false);
      recorder.start(500);
      setAssistantStatus("Đang nghe bạn nói…");
      timer = setInterval(() => {
        if (recorder.state !== "recording") return;
        analyser.getFloatTimeDomainData(values);
        const rms = Math.sqrt(values.reduce((sum, value) => sum + value * value, 0) / values.length);
        const now = performance.now();
        if (rms > 0.012) {
          voicedSamples += 1;
          lastVoiceAt = now;
        }
        if (now - startedAt >= 20000 || (voicedSamples >= 3 && now - lastVoiceAt >= 1100)
            || (voicedSamples < 3 && now - startedAt >= 8500) || total > 256 * 1024) {
          recorder.stop();
        }
      }, 80);
    } catch {
      if (recording !== attempt) return;
      stopRecognition();
      setAssistantStatus("Chưa mở được mic. Thử Nói với trợ lý hoặc nhập lời nhắn.");
      updateAssistantControls();
    }
  }

  function startListening(visitId, manual = false) {
    if (!assistantActive(visitId) || assistantBusy || assistantSpeaking || assistantDone) return;
    if (preferRecording && canRecord) {
      recordAssistantTurn(visitId);
      return;
    }
    if (!SpeechRecognition) {
      setAssistantStatus("Trình duyệt chưa hỗ trợ nhận giọng nói tự động. Bạn có thể nhập lời nhắn bên dưới.");
      updateAssistantControls();
      return;
    }
    stopRecognition();
    const rec = new SpeechRecognition();
    rec.lang = "vi-VN";
    rec.interimResults = false;
    rec.continuous = false;
    rec.maxAlternatives = 1;
    recognition = rec;
    assistantListening = true;
    setAssistantStatus("Đang nghe bạn nói…");
    updateAssistantControls();
    let gotResult = false;
    rec.onresult = event => {
      const transcript = Array.from(event.results || [])
        .map(item => item[0]?.transcript || "")
        .join(" ")
        .trim();
      if (!transcript) return;
      gotResult = true;
      assistantAutoRetries = 0;
      sendAssistantMessage(visitId, transcript);
    };
    rec.onerror = event => {
      if (recognition !== rec) return;
      assistantListening = false;
      recognition = null;
      const serviceError = ["not-allowed", "service-not-allowed", "network", "audio-capture"].includes(event.error);
      if (serviceError && canRecord) {
        preferRecording = true;
        recordAssistantTurn(visitId);
        return;
      }
      if (serviceError) {
        setAssistantStatus("Chưa có quyền nhận giọng nói. Bạn có thể nhập lời nhắn bên dưới.");
      } else if (!manual && event.error === "no-speech" && assistantAutoRetries < 2 && assistantActive(visitId)) {
        assistantAutoRetries += 1;
        setAssistantStatus("Chưa nghe rõ, đang lắng nghe lại…");
        assistantRetryTimer = setTimeout(() => startListening(visitId, false), 650);
      } else {
        setAssistantStatus("Bấm \"Nói với trợ lý\" để nói tiếp hoặc nhập lời nhắn bên dưới.");
      }
      updateAssistantControls();
    };
    rec.onend = () => {
      if (recognition !== rec) return;
      assistantListening = false;
      recognition = null;
      if (!gotResult && assistantActive(visitId) && !assistantBusy && !assistantSpeaking && !assistantDone) {
        if (!manual && assistantAutoRetries < 2) {
          assistantAutoRetries += 1;
          assistantRetryTimer = setTimeout(() => startListening(visitId, false), 650);
        } else {
          setAssistantStatus("Bấm \"Nói với trợ lý\" để nói tiếp hoặc nhập lời nhắn bên dưới.");
        }
      }
      updateAssistantControls();
    };
    try {
      rec.start();
    } catch {
      assistantListening = false;
      recognition = null;
      if (canRecord) {
        preferRecording = true;
        recordAssistantTurn(visitId);
        return;
      }
      setAssistantStatus("Bấm \"Nói với trợ lý\" để bắt đầu nói hoặc nhập lời nhắn bên dưới.");
      updateAssistantControls();
    }
  }

  async function sendAssistantMessage(visitId, input) {
    const audio = input instanceof Blob ? input : null;
    const text = audio ? "" : (input || "").trim();
    if ((!text && !audio?.size) || !assistantActive(visitId) || assistantBusy || assistantDone
        || !panel.dataset.assistantUrl) return;
    assistantBusy = true;
    stopRecognition();
    stopGreeting?.();
    stopReply?.();
    if (text) appendAssistantTurn("guest", text);
    setAssistantStatus("Đang chuyển lời tới trợ lý Hermes…");
    updateAssistantControls();
    try {
      const response = await fetch(panel.dataset.assistantUrl, {
        method: "POST",
        headers: {
          "Content-Type": audio ? audio.type : "application/json",
          "X-CSRF-Token": panel.dataset.csrf,
          "X-Intercom-Visit": String(visitId),
        },
        body: audio || JSON.stringify({ text }),
        signal: AbortSignal.timeout(audio ? 60000 : 30000),
        redirect: "error",
      });
      if (!assistantActive(visitId)) return;
      if (response.status === 409) {
        assistantBusy = false;
        clearTimeout(pollTimer);
        poll();
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (audio && data.transcription) {
        assistantBusy = false;
        if (data.transcription === "no_speech" && armed && assistantAutoRetries < 2) {
          assistantAutoRetries += 1;
          setAssistantStatus("Chưa nghe rõ, đang lắng nghe lại…");
          assistantRetryTimer = setTimeout(() => startListening(visitId), 450);
          updateAssistantControls();
          return;
        }
        setAssistantStatus(data.transcription === "no_speech"
          ? "Hermes chưa nghe rõ. Thử nói lại hoặc nhập lời nhắn."
          : "Tạm thời chưa chuyển được giọng nói. Thử nói lại hoặc nhập lời nhắn.");
        updateAssistantControls();
        return;
      }
      if (!response.ok && !data.done) throw new Error();
      if (data.transcript) appendAssistantTurn("guest", data.transcript);
      assistantAutoRetries = 0;
      const reply = data.reply || "Tôi đã ghi nhận lời nhắn của bạn và sẽ chuyển ngay cho chủ trọ.";
      assistantDone = Boolean(data.done) || response.status === 429;
      appendAssistantTurn("hermes", reply);
      assistantBusy = false;
      speakAssistant(visitId, data.reply_id, reply, () => {
        if (assistantDone) {
          stop(assistantMedia);
          assistantMedia = null;
          setAssistantStatus("Đã ghi nhận đầy đủ lời nhắn cho chủ trọ.");
        } else if (armed) {
          startListening(visitId, false);
        } else {
          setAssistantStatus("Bấm \"Nói với trợ lý\" hoặc nhập lời nhắn bên dưới.");
        }
      });
    } catch {
      if (!assistantActive(visitId)) return;
      assistantBusy = false;
      setAssistantStatus("Kết nối tạm gián đoạn. Bạn vui lòng nói lại hoặc nhập lời nhắn.");
      updateAssistantControls();
    }
  }

  function startAssistantFor(incoming) {
    if (!station || !assistantBox || !incoming.visit_id) return;
    if (assistantVisit === incoming.visit_id) {
      if (armed && !greetingStarted) playGreeting(incoming.visit_id);
      return;
    }
    stopAssistant(true);
    assistantVisit = incoming.visit_id;
    assistantDone = (incoming.assistant_turns || 0) >= (incoming.assistant_max_turns || 6);
    if (assistantLog) assistantLog.innerHTML = "";
    const greeting = incoming.assistant_greeting ||
      "Chào bạn, chủ trọ hiện chưa kịp nghe máy. Tôi là trợ lý nhà trọ, bạn cần gặp ai hay muốn để lại lời nhắn gì ạ?";
    appendAssistantTurn("hermes", greeting);
    updateAssistantControls();
    if (assistantDone) {
      setAssistantStatus("Đã ghi nhận lời nhắn cho chủ trọ.");
      return;
    }
    if (armed) playGreeting(incoming.visit_id);
    else setAssistantStatus("Bật mic và loa để nghe lời chào.");
  }

  function updateControls() {
    enable.hidden = armed || ended;
    enable.disabled = acquiring;
    mute.hidden = !media || !active();
    mute.textContent = muted ? "Bật lại mic" : "Tắt mic";
    mute.setAttribute("aria-pressed", String(muted));
    retry.hidden = !paused || !active();
    if (end) end.disabled = !active();
    if (flash) flash.hidden = !active();
  }

  async function sound() {
    try {
      await player.play();
      listen.hidden = true;
    } catch {
      if (player.srcObject) listen.hidden = false;
    }
  }

  async function keepStationAwake() {
    if (!station || !armed || ended || document.hidden || wakeLock || !navigator.wakeLock) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; }, { once: true });
    } catch {
      // The station can also be kept visible without a screen wake lock.
    }
  }

  async function restoreStationAudio() {
    if (!station) return;
    try {
      if (localStorage.getItem(stationAudioKey) !== "1") return;
      const permission = await navigator.permissions?.query({ name: "microphone" });
      if (permission?.state === "granted") {
        armed = true;
        updateControls();
        keepStationAwake();
      }
    } catch {
      // Some browsers require an explicit tap again after reload.
    }
  }

  function chime() {
    if (!station || !armed || audioContext?.state !== "running" ||
        call.status !== "ringing" || lastChime === call.visit_id) return;
    lastChime = call.visit_id;
    for (const [delay, frequency] of [[0, 660], [0.22, 880]]) {
      const tone = audioContext.createOscillator();
      const volume = audioContext.createGain();
      const at = audioContext.currentTime + delay;
      tone.frequency.value = frequency;
      volume.gain.setValueAtTime(0.085, at);
      volume.gain.exponentialRampToValueAtTime(0.001, at + 0.17);
      tone.connect(volume).connect(audioContext.destination);
      tone.start(at);
      tone.stop(at + 0.18);
    }
  }

  function signal(body, visitId, keepalive = false) {
    return fetch(panel.dataset.signalUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": panel.dataset.csrf,
        "X-Intercom-Visit": String(visitId),
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(4500),
      redirect: "error",
      keepalive,
    });
  }

  function clearPeer() {
    const previous = pc;
    pc = null;
    previous?.close();
    clearTimeout(connectionTimer);
    peer = null;
    description = null;
    gathering = null;
    settingRemote = false;
    player.srcObject = null;
    listen.hidden = true;
  }

  function release() {
    clearTimeout(stateDeadline);
    if (client && call.visit_id) signal({ client, leave: true }, call.visit_id, true).catch(() => {});
    client = null;
    iceServers = undefined;
    clearPeer();
    stop(media);
    media = null;
    muted = false;
  }

  function pauseCall(value) {
    release();
    paused = true;
    message(value);
    updateControls();
  }

  function waitForIce(connection) {
    if (connection.iceGatheringState === "complete") return Promise.resolve();
    return new Promise(resolve => {
      const done = () => {
        clearTimeout(timer);
        connection.removeEventListener("icegatheringstatechange", onChange);
        resolve();
      };
      const onChange = () => {
        if (connection.iceGatheringState === "complete") done();
      };
      const timer = setTimeout(done, 6500);
      connection.addEventListener("icegatheringstatechange", onChange);
    });
  }

  function connectTo(peerId) {
    const connection = new RTCPeerConnection({ iceServers: iceServers || [] });
    pc = connection;
    peer = peerId;
    for (const track of media.getAudioTracks()) connection.addTrack(track, media);
    connection.addEventListener("track", ({ streams, track }) => {
      if (connection === pc) {
        player.srcObject = streams[0] || new MediaStream([track]);
        sound();
      }
    });
    connection.addEventListener("connectionstatechange", () => {
      if (connection !== pc) return;
      if (connection.connectionState === "connected") {
        clearTimeout(connectionTimer);
        message("Đã kết nối âm thanh trực tiếp.");
      } else if (connection.connectionState === "failed") {
        pauseCall("Chưa nối được âm thanh. Bấm Thử kết nối lại.");
      } else if (connection.connectionState === "disconnected") {
        message("Âm thanh đang kết nối lại…");
        clearTimeout(connectionTimer);
        connectionTimer = setTimeout(() => {
          if (pc === connection && connection.connectionState !== "connected") {
            pauseCall("Kết nối âm thanh đã gián đoạn.");
          }
        }, 10000);
      }
    });
    connectionTimer = setTimeout(() => {
      if (pc === connection && connection.connectionState !== "connected") {
        pauseCall("Chưa nối được âm thanh. Bấm Thử kết nối lại.");
      }
    }, 25000);
    return connection;
  }

  async function publishDescription(connection) {
    try {
      const local = station ? await connection.createAnswer() : await connection.createOffer();
      if (pc !== connection) return;
      await connection.setLocalDescription(local);
      await waitForIce(connection);
      if (pc === connection) description = connection.localDescription.toJSON();
    } catch {
      if (pc === connection) pauseCall("Chưa thiết lập được âm thanh.");
    }
  }

  function acceptSignal(signalState) {
    if (peer !== signalState.peer) {
      clearPeer();
      if (!signalState.peer) {
        message(station ? "Đang chờ chủ trọ bật mic…" : "Đang chờ điện thoại ở cửa bật mic…");
        return;
      }
      connectTo(signalState.peer);
      message("Đang nối âm thanh…");
    }
    const connection = pc;
    if (!connection) return;
    if (!station && !gathering) gathering = publishDescription(connection);
    if (!signalState.description || connection.remoteDescription || settingRemote) return;
    if (station && gathering) return;
    const apply = async () => {
      settingRemote = true;
      try {
        await connection.setRemoteDescription(signalState.description);
        if (station && pc === connection) await publishDescription(connection);
      } catch {
        if (pc === connection) pauseCall("Chưa thiết lập được âm thanh.");
      } finally {
        if (pc === connection) settingRemote = false;
      }
    };
    if (station) gathering = apply();
    else apply();
  }

  async function acquire() {
    if (acquiring) return;
    acquiring = true;
    updateControls();
    try {
      const requestedVisit = active() ? call.visit_id : null;
      const stream = await navigator.mediaDevices.getUserMedia(microphone);
      if (!ended && !document.hidden) {
        armed = true;
        if (station) {
          try { localStorage.setItem(stationAudioKey, "1"); } catch { /* Storage is optional. */ }
        }
        if (requestedVisit && active() && call.visit_id === requestedVisit) {
          media = stream;
          for (const track of stream.getAudioTracks()) {
            track.addEventListener("ended", () => {
              if (media === stream) {
                armed = false;
                release();
                message("Mic đã ngắt. Bật mic lại để tiếp tục.");
                updateControls();
              }
            });
          }
          client = newClient();
          message("Đang nối âm thanh…");
        } else {
          stop(stream);
          message("Mic và loa đã sẵn sàng.");
        }
        keepStationAwake();
        chime();
      } else stop(stream);
    } catch {
      armed = false;
      message("Chưa dùng được mic. Kiểm tra quyền mic rồi thử lại.");
    } finally {
      acquiring = false;
      updateControls();
    }
  }

  async function poll() {
    if (polling || document.hidden || ended) return;
    polling = true;
    try {
      const url = new URL(panel.dataset.stateUrl, location.href);
      url.searchParams.set("ice", "1");
      if (station) url.searchParams.set("assistant", "1");
      const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(4500) });
      if ([401, 403, 404].includes(response.status) || response.redirected) {
        ended = true;
        stopAssistant(true);
        release();
        wakeLock?.release().catch(() => {});
        status.textContent = station ? "Điện thoại đã ngắt kết nối với chuông." : "Phiên đàm thoại không còn truy cập được.";
        reconnect.hidden = false;
        return;
      }
      if (!response.ok) throw new Error();
      const incoming = await response.json();
      if (document.hidden) return;
      let micCooldown = false;
      if (call.visit_id !== incoming.visit_id || (call.status === "active" && incoming.status !== "active")) {
        release();
        paused = false;
        message("");
      }
      if (incoming.status !== "assistant" && assistantVisit !== null) {
        micCooldown = stopAssistant(true);
      }
      call = incoming;
      status.dataset.state = call.status;
      let heading = statusText[call.status] || statusText.idle;
      if (station && call.status === "ringing" && typeof call.assistant_in === "number" && call.assistant_in > 0) {
        heading = `Có khách bấm chuông. Đang gọi chủ trọ… (trợ lý Hermes bật sau ${call.assistant_in} giây)`;
      }
      if (status.textContent !== heading) status.textContent = heading;
      chime();
      if (station && call.status === "assistant") {
        startAssistantFor(incoming);
        return;
      }
      if (!active() || paused) return;
      if (micCooldown) {
        await new Promise(resolve => setTimeout(resolve, 180));
        if (!active()) return;
      }
      if (!armed && autoEnable) {
        autoEnable = false;
        if (window.RTCPeerConnection && navigator.mediaDevices?.getUserMedia) await acquire();
      }
      if (!armed) return;
      clearTimeout(stateDeadline);
      stateDeadline = setTimeout(() => {
        release();
        message("Âm thanh đã tạm dừng do mất kết nối với web.");
        updateControls();
      }, 12000);
      if (!media) await acquire();
      if (!media || !active()) return;
      iceServers ??= call.ice_servers || [];
      const joined = client;
      const result = await signal({ client, target: peer, description }, call.visit_id);
      if (!active() || joined !== client) return;
      if (result.status === 409) {
        message("Đang chờ kết nối với phiên đàm thoại…");
      } else if (result.ok) {
        acceptSignal(await result.json());
      }
    } catch {
      if (!pc) message("Đang kết nối lại…");
    } finally {
      polling = false;
      updateControls();
      updateAssistantControls();
      if (!ended && !document.hidden) {
        const delay = call.status === "active" ? 650 : (call.status === "ringing" || call.status === "assistant") ? 1000 : 2000;
        pollTimer = setTimeout(poll, delay);
      }
    }
  }

  enable.addEventListener("click", async () => {
    if (!window.RTCPeerConnection || !navigator.mediaDevices?.getUserMedia) {
      message("Mở trang HTTPS bằng trình duyệt hỗ trợ đàm thoại.");
      return;
    }
    const Context = window.AudioContext || window.webkitAudioContext;
    if (Context) {
      audioContext ||= new Context();
      audioContext.resume().catch(() => {});
    }
    if (station && window.speechSynthesis) {
      try {
        window.speechSynthesis.cancel();
        const warmup = new SpeechSynthesisUtterance(" ");
        warmup.lang = "vi-VN";
        warmup.volume = 0;
        window.speechSynthesis.speak(warmup);
      } catch {
        // Ignore TTS warmup restrictions on unsupported browsers.
      }
    }
    await acquire();
    clearTimeout(pollTimer);
    poll();
  });
  assistantReplay?.addEventListener("click", () => playGreeting(call.visit_id));
  assistantSpeak?.addEventListener("click", () => {
    if (call.status === "assistant" && call.visit_id) {
      assistantAutoRetries = 0;
      startListening(call.visit_id, true);
    }
  });
  assistantForm?.addEventListener("submit", event => {
    event.preventDefault();
    const text = assistantInput?.value || "";
    if (!text.trim() || call.status !== "assistant" || !call.visit_id) return;
    assistantInput.value = "";
    sendAssistantMessage(call.visit_id, text);
  });
  mute.addEventListener("click", () => {
    muted = !muted;
    for (const track of media?.getAudioTracks() || []) track.enabled = !muted;
    updateControls();
  });
  listen.addEventListener("click", sound);
  retry.addEventListener("click", () => {
    paused = false;
    clearTimeout(pollTimer);
    poll();
  });
  function suspend() {
    clearTimeout(pollTimer);
    stopAssistant(false);
    release();
    wakeLock?.release().catch(() => {});
    updateControls();
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) suspend();
    else {
      keepStationAwake();
      poll();
    }
  });
  window.addEventListener("pagehide", suspend);
  window.addEventListener("pageshow", ({ persisted }) => {
    if (persisted) poll();
  });
  restoreStationAudio().finally(poll);
})();
