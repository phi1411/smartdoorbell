(() => {
  const chat = document.querySelector('[data-hermes-chat]');
  if (!chat || typeof fetch !== 'function') return;

  const transcript = chat.querySelector('[data-hermes-transcript]');
  const topics = chat.querySelector('[data-hermes-topics]');
  const form = chat.querySelector('[data-hermes-form]');
  const question = form.elements.namedItem('question');
  const submit = form.querySelector('button[type="submit"]');
  const feedback = chat.querySelector('[data-hermes-feedback]');
  let pending = false;

  function scrollToLatest() {
    transcript.scrollTop = transcript.scrollHeight;
  }

  function showFeedback(message) {
    feedback.hidden = !message;
    feedback.textContent = message;
  }

  function addTurn(author, message) {
    const turn = document.createElement('div');
    turn.className = 'hermes-turn';
    if (author === 'Bạn') {
      turn.classList.add('hermes-turn-user');
    } else {
      const mark = document.createElement('span');
      mark.className = 'hermes-mark hermes-mark-small';
      mark.setAttribute('aria-hidden', 'true');
      mark.textContent = 'H';
      turn.append(mark);
    }
    const bubble = document.createElement('div');
    bubble.className = 'hermes-message';
    const label = document.createElement('span');
    label.className = 'hermes-author';
    label.textContent = author;
    const body = document.createElement('p');
    body.textContent = message;
    bubble.append(label, body);
    turn.append(bubble);
    transcript.append(turn);
    return turn;
  }

  function resizeQuestion() {
    question.style.height = 'auto';
    question.style.height = `${question.scrollHeight}px`;
  }

  topics.hidden = false;
  topics.addEventListener('click', (event) => {
    const suggestion = event.target.closest('[data-hermes-prompt]');
    if (!suggestion) return;
    question.value = suggestion.dataset.hermesPrompt;
    resizeQuestion();
    question.focus();
  });
  question.addEventListener('input', resizeQuestion);
  question.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (!pending) form.requestSubmit();
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const message = question.value.trim();
    if (pending || !message) return;
    const data = new FormData(form);
    data.set('question', message);
    pending = submit.disabled = true;
    showFeedback('');
    question.value = '';
    resizeQuestion();
    const outgoing = addTurn('Bạn', message);
    const waiting = addTurn('Hermes', 'Đang tra cứu…');
    waiting.classList.add('hermes-turn-pending');
    scrollToLatest();

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: data, headers: { Accept: 'application/json' }, signal: controller.signal,
      });
      if (response.redirected || response.status === 401 || response.status === 403) {
        throw new Error('Tải lại trang để kiểm tra phiên đăng nhập, rồi gửi lại câu hỏi.');
      }
      if (response.status === 400) {
        throw new Error((await response.json()).error || 'Kiểm tra lại nội dung câu hỏi.');
      }
      if (!response.ok) throw new Error('');
      const { reply } = await response.json();
      if (typeof reply !== 'string' || !reply.trim()) throw new Error('');
      waiting.remove();
      addTurn('Hermes', reply);
    } catch (error) {
      outgoing.remove();
      if (!question.value) question.value = message;
      resizeQuestion();
      showFeedback(error.constructor === Error && error.message
        ? error.message : 'Chưa nhận được phản hồi. Bạn có thể gửi lại câu hỏi.');
    } finally {
      clearTimeout(timeout);
      waiting.remove();
      pending = submit.disabled = false;
      scrollToLatest();
    }
  });

  scrollToLatest();
})();
