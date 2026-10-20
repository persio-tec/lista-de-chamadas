const elements = {
  today: document.querySelector("#today-date"),
  presentCount: document.querySelector("#present-count"),
  presentFraction: document.querySelector("#present-fraction"),
  studentCount: document.querySelector("#student-count"),
  pendingCount: document.querySelector("#pending-count"),
  cameraBadge: document.querySelector("#camera-badge"),
  cameraToggle: document.querySelector("#camera-toggle"),
  cameraFeed: document.querySelector("#camera-feed"),
  cameraPlaceholder: document.querySelector("#camera-placeholder"),
  cameraMessage: document.querySelector("#camera-message"),
  enrollForm: document.querySelector("#enroll-form"),
  enrollButton: document.querySelector("#enroll-button"),
  enrollMessage: document.querySelector("#enroll-message"),
  name: document.querySelector("#person-name"),
  consent: document.querySelector("#consent"),
  roster: document.querySelector("#roster-body"),
  empty: document.querySelector("#empty-state"),
  search: document.querySelector("#search"),
  historyDate: document.querySelector("#history-date"),
  historyExport: document.querySelector("#history-export"),
  history: document.querySelector("#history-body"),
  historyEmpty: document.querySelector("#history-empty"),
  historyMessage: document.querySelector("#history-message"),
  historyMore: document.querySelector("#history-more"),
};

let state = { camera_running: false, students: [], date: "" };
let historyItems = [];
let historyHasMore = false;
let historyLoading = false;
let historyReloadRequested = false;
let lastCameraEventId = null;
let faceUpdateInProgress = false;
let faceUpdateMessageUntil = 0;

function showMessage(element, text, kind = "") {
  element.textContent = text;
  element.className = `inline-message${kind ? ` ${kind}` : ""}`;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const contentType = response.headers.get("Content-Type") || "";
  const data = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(data?.error || "Não foi possível concluir a ação.");
  return data;
}

function formattedTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
}

function initials(name) {
  return name.trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
}

function formattedDate(value) {
  if (!value) return "—";
  return new Date(`${value.slice(0, 10)}T12:00:00`).toLocaleDateString("pt-BR");
}

function renderHistory() {
  elements.history.replaceChildren();
  elements.historyEmpty.classList.toggle("visible", historyItems.length === 0);
  for (const item of historyItems) {
    const row = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = item.name;
    const dateCell = document.createElement("td");
    dateCell.textContent = formattedDate(item.occurred_at);
    const time = document.createElement("td");
    time.textContent = formattedTime(item.occurred_at);
    const method = document.createElement("td");
    method.textContent = item.method === "facial" ? "Reconhecimento facial" : "Manual";
    row.append(name, dateCell, time, method);
    elements.history.append(row);
  }
  elements.historyMore.hidden = !historyHasMore;
  elements.historyMore.disabled = historyLoading;
}

async function loadHistory(reset = true) {
  if (historyLoading) {
    if (reset) historyReloadRequested = true;
    return;
  }
  historyLoading = true;
  elements.historyMore.disabled = true;
  const offset = reset ? 0 : historyItems.length;
  const params = new URLSearchParams({ limit: "50", offset: String(offset) });
  if (elements.historyDate.value) params.set("date", elements.historyDate.value);
  const exportParams = new URLSearchParams();
  if (elements.historyDate.value) exportParams.set("date", elements.historyDate.value);
  elements.historyExport.href = `/api/history.csv${exportParams.size ? `?${exportParams}` : ""}`;
  try {
    const result = await request(`/api/history?${params}`);
    historyItems = reset ? result.items : [...historyItems, ...result.items];
    historyHasMore = result.has_more;
    elements.historyMessage.textContent = `${result.total} ${result.total === 1 ? "registro" : "registros"}`;
    elements.historyMessage.className = "inline-message";
    renderHistory();
  } catch (error) {
    showMessage(elements.historyMessage, error.message, "error");
  } finally {
    historyLoading = false;
    renderHistory();
    if (historyReloadRequested) {
      historyReloadRequested = false;
      void loadHistory();
    }
  }
}

function renderRoster() {
  const query = elements.search.value.trim().toLocaleLowerCase("pt-BR");
  const visible = state.students.filter((student) => student.name.toLocaleLowerCase("pt-BR").includes(query));
  elements.roster.replaceChildren();
  elements.empty.classList.toggle("visible", visible.length === 0);
  elements.empty.querySelector("strong").textContent = state.students.length === 0
    ? "A lista ainda está vazia"
    : "Nenhuma pessoa encontrada";
  elements.empty.querySelector("p").textContent = state.students.length === 0
    ? "Adicione uma pessoa acima para começar a chamada."
    : "Tente buscar por outro nome.";

  for (const student of visible) {
    const row = document.createElement("tr");
    const person = document.createElement("td");
    person.innerHTML = `<div class="person-cell"><span class="avatar"></span><span></span></div>`;
    person.querySelector(".avatar").textContent = initials(student.name);
    person.querySelector(".person-cell > span:last-child").textContent = student.name;

    const status = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `presence ${student.marked_at ? "present" : "absent"}`;
    badge.innerHTML = `<span class="presence-dot"></span>`;
    badge.append(document.createTextNode(student.marked_at ? "Presente" : "Pendente"));
    status.append(badge);

    const time = document.createElement("td");
    time.textContent = formattedTime(student.marked_at);

    const action = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    if (!student.marked_at) {
      const markButton = document.createElement("button");
      markButton.className = "action-button";
      markButton.type = "button";
      markButton.textContent = "Marcar";
      markButton.addEventListener("click", () => markManually(student.id));
      actions.append(markButton);
    }
    const deleteButton = document.createElement("button");
    deleteButton.className = "action-button delete";
    deleteButton.type = "button";
    deleteButton.textContent = "Excluir cadastro";
    deleteButton.addEventListener("click", () => removeStudent(student.id, student.name));
    const updateFaceButton = document.createElement("button");
    updateFaceButton.className = "action-button";
    updateFaceButton.type = "button";
    updateFaceButton.textContent = "Atualizar rosto";
    updateFaceButton.addEventListener("click", () => updateStudentFace(student.id, student.name));
    actions.append(updateFaceButton, deleteButton);
    action.append(actions);
    row.append(person, status, time, action);
    elements.roster.append(row);
  }
}

function render() {
  const present = state.students.filter((student) => student.marked_at).length;
  const total = state.students.length;
  elements.today.textContent = new Date(`${state.date || new Date().toISOString().slice(0, 10)}T12:00:00`)
    .toLocaleDateString("pt-BR", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  elements.presentCount.textContent = String(present);
  elements.presentFraction.textContent = `de ${total} ${total === 1 ? "pessoa" : "pessoas"}`;
  elements.studentCount.textContent = String(total);
  elements.pendingCount.textContent = String(total - present);

  const running = state.camera_running;
  elements.cameraBadge.className = `badge ${running ? "badge-running" : "badge-idle"}`;
  elements.cameraBadge.innerHTML = `<span class="badge-dot"></span> ${running ? "Ativa" : "Desativada"}`;
  elements.cameraToggle.innerHTML = running
    ? '<span aria-hidden="true">■</span> Pausar chamada'
    : '<span aria-hidden="true">◎</span> Ativar chamada';
  elements.cameraToggle.className = `button ${running ? "button-secondary" : "button-primary"}`;
  elements.cameraPlaceholder.hidden = running;
  elements.cameraFeed.hidden = !running;
  if (running && !elements.cameraFeed.hasAttribute("src")) elements.cameraFeed.src = `/video?${Date.now()}`;
  if (!running) elements.cameraFeed.removeAttribute("src");
  renderRoster();
}

async function refresh() {
  try {
    const status = await request("/api/status");
    const wasRunning = state.camera_running;
    state = status;
    render();
    const latestEventId = status.latest_attendance_event?.id;
    const newCameraEvent = latestEventId !== undefined && latestEventId !== lastCameraEventId;
    if (newCameraEvent) {
      lastCameraEventId = latestEventId;
      void loadHistory();
    }
    if (status.model_error) showMessage(elements.cameraMessage, status.model_error, "error");
    else if (status.camera_error) showMessage(elements.cameraMessage, status.camera_error, "error");
    else if (status.camera_running && status.last_match) {
      faceUpdateMessageUntil = 0;
      if (newCameraEvent) {
        showMessage(elements.cameraMessage, `${status.last_match.name} identificada. Nova passagem registrada no histórico.`, "success");
      } else {
        showMessage(elements.cameraMessage, `${status.last_match.name} reconhecida; esta passagem já foi registrada.`, "success");
      }
    } else if (faceUpdateInProgress || Date.now() < faceUpdateMessageUntil) {
      return;
    } else if (status.camera_running && status.recognition_status?.status === "unrecognized") {
      showMessage(elements.cameraMessage, "Rosto detectado, mas não corresponde ao cadastro atual. Se esta pessoa estiver na lista, escolha “Atualizar rosto” ao lado do nome.", "error");
    } else if (status.camera_running && status.recognition_status?.status === "multiple_faces") {
      showMessage(elements.cameraMessage, "Mostre apenas uma pessoa por vez para o reconhecimento facial.");
    } else if (status.camera_running) {
      showMessage(elements.cameraMessage, "Chamada automática ativa. Posicione uma pessoa cadastrada em frente à câmera.");
    } else if (wasRunning) {
      showMessage(elements.cameraMessage, "Câmera desligada.");
    }
  } catch (error) {
    showMessage(elements.cameraMessage, error.message, "error");
  }
}

async function toggleCamera() {
  elements.cameraToggle.disabled = true;
  try {
    if (state.camera_running) {
      await request("/api/camera/stop", { method: "POST", body: "{}" });
    } else {
      await request("/api/camera/start", { method: "POST", body: "{}" });
      showMessage(elements.cameraMessage, "Câmera iniciada. Posicione uma pessoa por vez.");
    }
    await refresh();
  } catch (error) {
    showMessage(elements.cameraMessage, error.message, "error");
  } finally {
    elements.cameraToggle.disabled = false;
  }
}

async function enroll(event) {
  event.preventDefault();
  elements.enrollButton.disabled = true;
  showMessage(elements.enrollMessage, "");
  try {
    await request("/api/enroll", {
      method: "POST",
      body: JSON.stringify({ name: elements.name.value, consent: elements.consent.checked }),
    });
    elements.enrollForm.reset();
    showMessage(elements.enrollMessage, "Cadastro facial salvo apenas neste computador.", "success");
    await refresh();
  } catch (error) {
    showMessage(elements.enrollMessage, error.message, "error");
  } finally {
    elements.enrollButton.disabled = false;
  }
}

async function markManually(studentId) {
  try {
    await request("/api/attendance/manual", { method: "POST", body: JSON.stringify({ student_id: studentId }) });
    await refresh();
    await loadHistory();
  } catch (error) {
    showMessage(elements.cameraMessage, error.message, "error");
  }
}

async function removeStudent(studentId, name) {
  if (!window.confirm(`Excluir o cadastro de ${name}, incluindo os dados faciais e registros de presença deste computador?`)) return;
  try {
    await request(`/api/students/${studentId}`, { method: "DELETE" });
    showMessage(elements.enrollMessage, "Cadastro e dados faciais removidos.", "success");
    await refresh();
    await loadHistory();
  } catch (error) {
    showMessage(elements.enrollMessage, error.message, "error");
  }
}

async function updateStudentFace(studentId, name) {
  if (!window.confirm(`Posicione ${name} diante da câmera e confirme que a pessoa consentiu com a captura de cinco novas amostras faciais.`)) return;
  faceUpdateInProgress = true;
  try {
    showMessage(elements.cameraMessage, `Capturando novas amostras faciais de ${name}. Mantenha apenas essa pessoa em frente à câmera.`);
    const result = await request(`/api/students/${studentId}/face`, {
      method: "PUT",
      body: JSON.stringify({ consent: true }),
    });
    faceUpdateInProgress = false;
    faceUpdateMessageUntil = Date.now() + 8000;
    await refresh();
    showMessage(elements.cameraMessage, `Cadastro facial de ${name} atualizado com ${result.samples} amostras. O reconhecimento automático está ativo.`, "success");
  } catch (error) {
    faceUpdateInProgress = false;
    faceUpdateMessageUntil = Date.now() + 8000;
    showMessage(elements.cameraMessage, error.message, "error");
  }
}

elements.cameraToggle.addEventListener("click", toggleCamera);
elements.enrollForm.addEventListener("submit", enroll);
elements.search.addEventListener("input", renderRoster);
elements.historyDate.addEventListener("change", () => loadHistory());
elements.historyMore.addEventListener("click", () => loadHistory(false));
window.addEventListener("pagehide", () => navigator.sendBeacon("/api/camera/stop"));

async function initialize() {
  await refresh();
  await loadHistory();
  if (!state.camera_running) await toggleCamera();
}

initialize();
window.setInterval(refresh, 1500);
